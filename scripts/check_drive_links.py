#!/usr/bin/env python3
"""Check that Drive links embedded in Knows task goals are publicly accessible.

Probes every Google Drive / Docs URL found in ``eval/tasks/<family>/
instance_<n>/task.md`` with an unauthenticated HTTP GET — the same
experience the agent's browser has — and reports PUBLIC / PRIVATE /
MISSING / ERROR per link. Placeholder tokens left by per-run setup
scripts (e.g. sheets_10) are reported as SKIP.

Usage
-----
    python scripts/check_drive_links.py                       # sweep all families
    python scripts/check_drive_links.py --split docs_1        # one family (by split name)
    python scripts/check_drive_links.py --family docs_1_formal_letter --instance 2
    python scripts/check_drive_links.py --url https://drive.google.com/drive/folders/<id>
    python scripts/check_drive_links.py --api                 # add service-account permission check
    python scripts/check_drive_links.py --json                # machine-readable output

Exits non-zero when any link is PRIVATE, MISSING or ERROR.
"""

from __future__ import annotations

import argparse
import json
import os
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


def _load_dotenv_files() -> None:
    """Load repo env files (SERVICE_ACCOUNT_PATH may live there)."""
    for env_path in (_REPO / ".env", _REPO / "browsergym" / "knows" / ".env"):
        if not env_path.is_file():
            continue
        try:
            with open(env_path) as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[len("export ") :]
                    if "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
        except Exception as exc:  # noqa: BLE001 - best-effort env loading
            print(f"Warning: failed to read {env_path}: {exc}", file=sys.stderr)


_load_dotenv_files()

from browsergym.knows.eval.eval_utils import drive_link_check as dlc  # noqa: E402

EVAL_TASKS_DIR = _REPO / "browsergym" / "knows" / "src" / "browsergym" / "knows" / "eval" / "tasks"

_FAILURE_STATUSES = (dlc.STATUS_PRIVATE, dlc.STATUS_MISSING, dlc.STATUS_ERROR)


def _resolve_family(split_or_family: str) -> Path:
    """Map 'docs_1' or a full family name to its task-family directory."""
    direct = EVAL_TASKS_DIR / split_or_family
    if direct.is_dir():
        return direct
    matches = [
        d
        for d in EVAL_TASKS_DIR.iterdir()
        if d.is_dir() and d.name.startswith(split_or_family + "_")
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"error: no task family matches {split_or_family!r} under {EVAL_TASKS_DIR}")
    sys.exit(
        f"error: ambiguous split {split_or_family!r}: " + ", ".join(m.name for m in matches)
    )


def _build_drive_service():
    """Build a Drive v3 service from the evaluator service account."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    sa_path = os.environ.get(
        "SERVICE_ACCOUNT_PATH",
        str(_REPO / "browsergym" / "knows" / "auth-data" / "service-account.json"),
    )
    if not Path(sa_path).is_file():
        sys.exit(f"error: --api needs a service account key; not found at {sa_path}")
    creds = Credentials.from_service_account_file(
        sa_path, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _print_record(record: dict, drive_service=None) -> None:
    link, result = record["link"], record["result"]
    where = f"{record['family']}/{record['instance']}" if record.get("family") else "(url)"
    file_id = link.file_id or "-"
    line = (
        f"{where:<45} {link.kind:<12} {file_id:<36} {result.status:<8} "
        f"{result.detail}"
    )
    if drive_service is not None and link.file_id:
        is_public, detail = dlc.check_link_via_api(drive_service, link.file_id)
        line += f"  [api: {'anyone' if is_public else 'NO anyone'} — {detail}]"
    print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--all", action="store_true", help="sweep every family (default)")
    target.add_argument("--family", help="one task family directory name")
    target.add_argument("--split", help="short split name, e.g. docs_1")
    target.add_argument("--url", help="check a single URL and exit")
    parser.add_argument("--instance", type=int, help="restrict to one instance number")
    parser.add_argument("--api", action="store_true", help="add service-account permission check")
    parser.add_argument("--json", action="store_true", dest="as_json", help="JSON output")
    parser.add_argument("--timeout", type=float, default=15.0, help="per-request timeout (s)")
    parser.add_argument("--retries", type=int, default=3, help="attempts per link")
    args = parser.parse_args()

    drive_service = _build_drive_service() if args.api else None

    if args.url:
        link = dlc.DriveLink(
            url=args.url,
            kind=dlc._classify_kind(args.url),
            file_id=dlc._extract_file_id(args.url),
        )
        result = dlc.check_link_public(link, timeout=args.timeout, retries=args.retries)
        records = [{"family": None, "instance": None, "link": link, "result": result}]
    elif args.family or args.split:
        family_dir = _resolve_family(args.family or args.split)
        records = []
        for record in dlc.sweep_all_tasks(family_dir.parent, args.timeout, args.retries):
            if record["family"] != family_dir.name:
                continue
            if args.instance and record["instance"] != f"instance_{args.instance}":
                continue
            records.append(record)
    else:
        if not EVAL_TASKS_DIR.is_dir():
            sys.exit(f"error: tasks directory not found: {EVAL_TASKS_DIR}")
        records = dlc.sweep_all_tasks(EVAL_TASKS_DIR, args.timeout, args.retries)

    if args.as_json:
        print(
            json.dumps(
                [
                    {
                        "family": r["family"],
                        "instance": r["instance"],
                        "url": r["link"].url,
                        "kind": r["link"].kind,
                        "file_id": r["link"].file_id,
                        "status": r["result"].status,
                        "http_code": r["result"].http_code,
                        "final_url": r["result"].final_url,
                        "detail": r["result"].detail,
                    }
                    for r in records
                ],
                indent=2,
            )
        )
    else:
        if not records:
            print("no Drive links found in the selected task.md files")
        for record in records:
            _print_record(record, drive_service)
        counts: dict = {}
        for record in records:
            counts[record["result"].status] = counts.get(record["result"].status, 0) + 1
        if records:
            print(
                "-- "
                + "  ".join(f"{status}: {n}" for status, n in sorted(counts.items()))
            )

    failed = [r for r in records if r["result"].status in _FAILURE_STATUSES]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
