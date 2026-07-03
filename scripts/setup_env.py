#!/usr/bin/env python3
"""One-stop environment setup / validation for the Knows benchmark.

Checks everything a fresh machine needs before ``run_one.sh`` /
``run.sh`` can work, in dependency order:

1. interpreter        — running under the ``knows`` conda env (warn only)
2. .env               — exists (bootstrapped from .env.example if not)
3. credentials        — GOOGLE_USER_EMAIL / GOOGLE_USER_PASSWORD filled in;
                        model API keys reported per benchmark script family
4. playwright         — chromium browser installed
5. auth mint          — real headless login via scripts/google_auto_login.py
                        (pass --headed the first time to clear Google's
                        "Verify it's you" challenge)
6. service account    — evaluator credential parses and the Drive API
                        accepts it
7. drive links        — every Drive link embedded in task goals is publicly
                        accessible (the agent's account has no per-file grants)

Each step prints ``[setup] PASS/WARN/FAIL``; the script exits non-zero if
any step FAILs. Re-running is always safe: no step destroys state and an
existing .env is never overwritten.

Usage
-----
    python scripts/setup_env.py                # full check
    python scripts/setup_env.py --headed       # first run on a new machine/IP
    python scripts/setup_env.py --skip-mint --skip-link-check   # fast subset
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Sys-path / PYTHONPATH wiring — mirrors benchmark.py so every sub-import
# resolves correctly regardless of the user's current working directory.
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent.parent
_LOCAL_PATHS = [
    _REPO / "browsergym" / "core" / "src",
    _REPO / "browsergym" / "experiments" / "src",
    _REPO / "browsergym" / "knows",
    _REPO / "browsergym" / "knows" / "src",
    _REPO,
]
for _p in _LOCAL_PATHS:
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_KNOWS_PYTHON = "/opt/miniconda3/envs/knows/bin/python"

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

# (env var, benchmark script family) — WARN when missing, never FAIL.
_MODEL_KEYS = [
    ("OPENAI_API_KEY", "gpt55_*"),
    ("ANTHROPIC_API_KEY", "opus47_*"),
    ("DEEPSEEK_API_KEY", "deepseek_v4_*"),
    ("GEMINI_API_KEY", "gemini31_*"),
]

_PLACEHOLDER_VALUES = {"you@gmail.com", "your-password", "changeme", "..."}


class _Report:
    """Collects step outcomes and renders the final summary."""

    def __init__(self):
        self.rows = []

    def add(self, status: str, step: str, detail: str) -> None:
        self.rows.append((status, step, detail))
        print(f"[setup] {status:<4} {step}: {detail}")

    @property
    def failed(self) -> bool:
        return any(status == FAIL for status, _, _ in self.rows)

    def summary(self) -> None:
        print("\n" + "=" * 72)
        print("Setup summary")
        print("=" * 72)
        for status, step, detail in self.rows:
            print(f"  {status:<4} {step:<18} {detail}")
        print("=" * 72)
        if self.failed:
            print("Result: NOT READY — fix the FAIL lines above and re-run.")
        else:
            print("Result: environment ready. Try:")
            print("  ./run_one.sh opus47_axt.py knows_docs_1")


def _parse_env_file(path: Path) -> dict:
    """Parse KEY=VALUE lines (with optional 'export ' prefix) from *path*."""
    values = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def check_interpreter(report: _Report) -> None:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.executable == _KNOWS_PYTHON:
        report.add(PASS, "interpreter", f"{sys.executable} (python {version})")
    else:
        report.add(
            WARN,
            "interpreter",
            f"running {sys.executable} (python {version}); benchmark runs "
            f"expect the knows conda env at {_KNOWS_PYTHON}",
        )


def check_env_file(report: _Report) -> dict:
    env_path = _REPO / ".env"
    example_path = _REPO / ".env.example"
    if not env_path.is_file():
        if example_path.is_file():
            shutil.copy(example_path, env_path)
            report.add(
                FAIL,
                ".env",
                f"created {env_path} from .env.example — fill in "
                "GOOGLE_USER_EMAIL / GOOGLE_USER_PASSWORD and re-run",
            )
        else:
            report.add(FAIL, ".env", f"missing (and no .env.example to copy at {example_path})")
        return {}
    values = _parse_env_file(env_path)
    report.add(PASS, ".env", f"found {env_path} ({len(values)} keys)")
    return values


def check_credentials(report: _Report, env_values: dict) -> bool:
    ok = True
    for key in ("GOOGLE_USER_EMAIL", "GOOGLE_USER_PASSWORD"):
        value = env_values.get(key, "")
        if not value or value in _PLACEHOLDER_VALUES:
            report.add(FAIL, "credentials", f"{key} is empty or still the placeholder")
            ok = False
    email = env_values.get("GOOGLE_USER_EMAIL", "")
    if ok and "@" not in email:
        report.add(FAIL, "credentials", f"GOOGLE_USER_EMAIL={email!r} is not an email address")
        ok = False
    if ok:
        report.add(PASS, "credentials", f"Google account {email}")
    for key, scripts in _MODEL_KEYS:
        if not env_values.get(key):
            report.add(WARN, "model keys", f"{key} not set — {scripts} scripts will not run")
    return ok


def check_playwright(report: _Report) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        report.add(FAIL, "playwright", "not importable — run `make install`")
        return False
    try:
        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
            if exe.exists():
                report.add(PASS, "playwright", f"chromium at {exe}")
                return True
    except Exception as exc:  # noqa: BLE001 - report any startup failure
        report.add(FAIL, "playwright", f"failed to start ({exc})")
        return False
    report.add(FAIL, "playwright", "chromium missing — run `playwright install chromium`")
    return False


def check_auth_mint(report: _Report, env_values: dict, headed: bool) -> None:
    env = os.environ.copy()
    for key, value in env_values.items():
        env.setdefault(key, value)
    command = [
        sys.executable,
        str(_REPO / "scripts" / "google_auto_login.py"),
        "--output",
        str(_REPO / "storage_state.json"),
        "--verbose",
    ]
    if headed:
        command.append("--headed")
    print(f"[setup] .... auth mint: running {' '.join(command)}")
    proc = subprocess.run(command, cwd=str(_REPO), env=env)
    if proc.returncode == 0:
        report.add(PASS, "auth mint", "storage_state.json minted (headless login works)")
    else:
        report.add(
            FAIL,
            "auth mint",
            "google_auto_login.py failed — see output above and the debug "
            f"artifacts under {_REPO / '.bg_storage_state_pool' / 'debug'}; "
            "on a new machine/IP re-run with --headed to clear Google's "
            "'Verify it's you' challenge",
        )


def check_service_account(report: _Report) -> None:
    sa_path = Path(
        os.environ.get(
            "SERVICE_ACCOUNT_PATH",
            str(_REPO / "browsergym" / "knows" / "auth-data" / "service-account.json"),
        )
    )
    if not sa_path.is_file():
        report.add(
            FAIL,
            "service account",
            f"not found at {sa_path} — evaluators cannot grade without it "
            "(see SETUP_AUTH.md; use --skip-service-account to bypass)",
        )
        return
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_service_account_file(
            str(sa_path), scopes=["https://www.googleapis.com/auth/drive"]
        )
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        about = drive.about().get(fields="user(emailAddress)").execute()
        sa_email = about.get("user", {}).get("emailAddress", "<unknown>")
        report.add(PASS, "service account", f"Drive API live as {sa_email}")
    except Exception as exc:  # noqa: BLE001 - any API/parse failure is a FAIL
        report.add(FAIL, "service account", f"{sa_path} rejected by the Drive API: {exc}")


def check_drive_links(report: _Report) -> None:
    try:
        from browsergym.knows.eval.eval_utils import drive_link_check as dlc
    except ImportError as exc:
        report.add(FAIL, "drive links", f"checker not importable ({exc})")
        return
    tasks_dir = (
        _REPO / "browsergym" / "knows" / "src" / "browsergym" / "knows" / "eval" / "tasks"
    )
    if not tasks_dir.is_dir():
        report.add(FAIL, "drive links", f"tasks directory missing: {tasks_dir} — run "
                   "`git submodule update --init --recursive`")
        return
    records = dlc.sweep_all_tasks(tasks_dir, timeout=15.0, retries=2)
    bad = [r for r in records if r["result"].status in (dlc.STATUS_PRIVATE, dlc.STATUS_MISSING, dlc.STATUS_ERROR)]
    counts: dict = {}
    for r in records:
        counts[r["result"].status] = counts.get(r["result"].status, 0) + 1
    tally = "  ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    if bad:
        families = sorted({f"{r['family']}/{r['instance']}" for r in bad})
        report.add(
            FAIL,
            "drive links",
            f"{len(bad)} non-public link(s) [{tally}] in: {', '.join(families)} — "
            "details via `python scripts/check_drive_links.py`; affected tasks "
            "will refuse to start (KNOWS_SKIP_LINK_CHECK=1 to override)",
        )
    else:
        report.add(PASS, "drive links", f"all task links public [{tally or 'no links found'}]")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--headed", action="store_true",
                        help="run the auth mint with a visible browser (first run)")
    parser.add_argument("--skip-mint", action="store_true", help="skip the login test")
    parser.add_argument("--skip-link-check", action="store_true", help="skip the Drive-link sweep")
    parser.add_argument("--skip-service-account", action="store_true",
                        help="skip the evaluator credential check")
    args = parser.parse_args()

    report = _Report()
    check_interpreter(report)

    env_values = check_env_file(report)
    creds_ok = bool(env_values) and check_credentials(report, env_values)
    playwright_ok = check_playwright(report)

    if args.skip_mint:
        report.add(SKIP, "auth mint", "--skip-mint")
    elif not (creds_ok and playwright_ok):
        report.add(SKIP, "auth mint", "blocked by earlier failure")
    else:
        check_auth_mint(report, env_values, headed=args.headed)

    if args.skip_service_account:
        report.add(SKIP, "service account", "--skip-service-account")
    else:
        check_service_account(report)

    if args.skip_link_check:
        report.add(SKIP, "drive links", "--skip-link-check")
    else:
        check_drive_links(report)

    report.summary()
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
