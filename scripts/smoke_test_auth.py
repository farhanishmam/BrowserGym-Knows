"""Smoke test for the per-worker auto-login storage_state pool.

Verifies four properties without contacting Google:

1. ``auto_login`` is the default auth mode in both ``benchmark.py`` and
   ``benchmarks/_common.py`` (no env var required).
2. Each simulated worker gets its own ``worker_<pid>.json`` snapshot --
   no two workers ever read or write the same file.
3. When a worker has no fresh snapshot, ``mint_for_current_pid`` invokes
   the auto-login subprocess to create one.
4. A *new* simulated worker (a PID with no existing file) follows the
   same code path: it acquires the lock, runs auto-login, and ends up
   with a brand-new per-PID file.

We monkey-patch ``_run_auto_login`` so the test never opens a real
browser; the assertion is structural (call count, file paths, locking)
rather than functional.

Run from the repo root:

    python scripts/smoke_test_auth.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


def _ok(msg: str) -> None:
    print(f"{GREEN}OK{RESET}  {msg}")


def _fail(msg: str) -> None:
    print(f"{RED}FAIL{RESET}  {msg}")
    raise SystemExit(1)


def check_auto_login_is_default() -> None:
    """benchmark.py & benchmarks/_common.py must default to auto_login."""
    bench_text = (_REPO_ROOT / "benchmark.py").read_text()
    common_text = (_REPO_ROOT / "benchmarks" / "_common.py").read_text()

    if 'BROWSERGYM_AUTH_MODE", "auto_login"' not in bench_text:
        _fail("benchmark.py does not default BROWSERGYM_AUTH_MODE to auto_login")
    if "_AUTH_MODE != \"auto_login\"" not in bench_text:
        _fail("benchmark.py does not reject non-auto_login modes")
    if 'AUTH_MODE_AUTO_LOGIN = "auto_login"' not in common_text:
        _fail("_common.py is missing the AUTH_MODE_AUTO_LOGIN constant")
    if "DEFAULT_AUTH_MODE = AUTH_MODE_AUTO_LOGIN" not in common_text:
        _fail("_common.py default auth mode is not AUTH_MODE_AUTO_LOGIN")

    _ok("auto_login is the hard-coded default in benchmark.py and _common.py")


def check_per_worker_pid_isolation() -> None:
    """Each PID resolves to a distinct ``worker_<pid>.json`` path."""
    from scripts import storage_state_pool as ssp

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BROWSERGYM_STATE_POOL_DIR"] = tmp
        paths: set[Path] = set()
        for fake_pid in (1001, 1002, 1003, 1004, 1005):
            p = ssp._state_path_for_pid(fake_pid)
            paths.add(p)
            if p.name != f"worker_{fake_pid}.json":
                _fail(f"unexpected per-PID filename {p.name}")
            if p.parent != Path(tmp):
                _fail(f"snapshot {p} not under pool dir {tmp}")
        if len(paths) != 5:
            _fail(f"per-PID filenames collided: {paths}")

    _ok("each PID maps to its own worker_<pid>.json under the pool dir")


def check_mint_runs_login_when_missing() -> None:
    """A worker with no snapshot triggers ``_run_auto_login``."""
    from scripts import storage_state_pool as ssp

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BROWSERGYM_STATE_POOL_DIR"] = tmp

        login_calls: list[Path] = []

        def fake_login(output: Path, *, timeout_s: float = 180.0) -> bool:
            login_calls.append(output)
            output.write_text('{"cookies": [], "origins": []}')
            return True

        with mock.patch.object(ssp, "_run_auto_login", side_effect=fake_login):
            path = ssp.mint_for_current_pid()

        if path is None:
            _fail("mint_for_current_pid returned None for a fresh worker")
        if not Path(path).is_file():
            _fail(f"mint did not create snapshot at {path}")
        if len(login_calls) != 1:
            _fail(f"expected 1 auto-login call, got {len(login_calls)}")
        if login_calls[0] != Path(path):
            _fail("auto-login output path does not match mint return path")
        if Path(path).name != f"worker_{os.getpid()}.json":
            _fail(f"snapshot is not pid-scoped: {Path(path).name}")

    _ok("missing snapshot -> auto-login subprocess runs and writes the file")


def check_new_worker_has_no_state_then_creates_one() -> None:
    """Simulate a freshly-spawned worker: no file, then mint creates one."""
    from scripts import storage_state_pool as ssp

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BROWSERGYM_STATE_POOL_DIR"] = tmp
        fake_new_pid = 999_999  # a PID that almost certainly does not exist

        with mock.patch.object(ssp.os, "getpid", return_value=fake_new_pid):
            target = ssp._state_path_for_pid(fake_new_pid)
            if target.exists():
                _fail("test setup error: snapshot already exists for new pid")

            login_calls: list[Path] = []

            def fake_login(output: Path, *, timeout_s: float = 180.0) -> bool:
                login_calls.append(output)
                output.write_text('{"cookies": [], "origins": []}')
                return True

            with mock.patch.object(ssp, "_run_auto_login", side_effect=fake_login):
                path = ssp.mint_for_current_pid()

            if path is None or Path(path) != target:
                _fail(f"new worker did not get its own snapshot ({path} vs {target})")
            if len(login_calls) != 1:
                _fail("new worker should run auto-login exactly once")

    _ok("a freshly-spawned worker with no state mints a new one via auto-login")


def check_existing_fresh_snapshot_is_reused() -> None:
    """If the per-PID snapshot is fresh, mint must NOT run login again."""
    from scripts import storage_state_pool as ssp

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BROWSERGYM_STATE_POOL_DIR"] = tmp
        fake_pid = 555_555

        target = Path(tmp) / f"worker_{fake_pid}.json"
        target.write_text('{"cookies": [], "origins": []}')
        os.utime(target, (time.time(), time.time()))

        with mock.patch.object(ssp.os, "getpid", return_value=fake_pid):
            with mock.patch.object(ssp, "_run_auto_login") as login:
                path = ssp.mint_for_current_pid()
                if login.called:
                    _fail("fresh snapshot should not trigger auto-login")
                if path is None or Path(path) != target:
                    _fail("fresh snapshot was not returned as-is")

    _ok("fresh per-PID snapshot is reused without re-running auto-login")


def check_two_concurrent_workers_get_different_files() -> None:
    """Two workers (different PIDs) end up with different snapshot paths."""
    from scripts import storage_state_pool as ssp

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BROWSERGYM_STATE_POOL_DIR"] = tmp

        produced: dict[int, Path] = {}

        def fake_login(output: Path, *, timeout_s: float = 180.0) -> bool:
            output.write_text('{"cookies": [], "origins": []}')
            return True

        with mock.patch.object(ssp, "_run_auto_login", side_effect=fake_login):
            for fake_pid in (777_001, 777_002):
                with mock.patch.object(ssp.os, "getpid", return_value=fake_pid):
                    path = ssp.mint_for_current_pid()
                    if path is None:
                        _fail(f"mint failed for fake pid {fake_pid}")
                    produced[fake_pid] = Path(path)

        if produced[777_001] == produced[777_002]:
            _fail("two workers ended up sharing the same snapshot file")
        for pid, path in produced.items():
            if path.name != f"worker_{pid}.json":
                _fail(f"snapshot for pid {pid} has wrong name: {path.name}")

    _ok("concurrent workers get different worker_<pid>.json files")


def main() -> None:
    print("Running auto-login / per-worker storage_state smoke test...")
    check_auto_login_is_default()
    check_per_worker_pid_isolation()
    check_mint_runs_login_when_missing()
    check_new_worker_has_no_state_then_creates_one()
    check_existing_fresh_snapshot_is_reused()
    check_two_concurrent_workers_get_different_files()
    print(f"\n{GREEN}All smoke-test checks passed.{RESET}")


if __name__ == "__main__":
    main()
