"""Per-PID Playwright ``storage_state`` mint pool for the KNOWS benchmark.

Each Ray worker that runs a benchmark task needs its own, freshly-minted
Google session so we can saturate the configured parallelism without 5
workers fighting over a single rotating cookie set or a Chromium
SingletonLock. This module owns that per-PID lifecycle.

What it does
------------
- Resolves a directory ``<repo>/.bg_storage_state_pool/`` (configurable via
  ``BROWSERGYM_STATE_POOL_DIR``) and stores one
  ``worker_<pid>.json`` snapshot inside it per live worker process.
- :func:`mint_for_current_pid` returns the path to the current worker's
  snapshot, calling :mod:`scripts.google_auto_login` on demand if the file
  is missing or older than :data:`_STALE_AFTER_SECONDS`.
- Concurrent mints are serialized via a directory-wide ``mint.lock`` file
  so the 5 parallel workers end up logging in sequentially -- avoids the
  rate-limit / "too many sign-ins from this IP" failure mode without
  giving up parallelism for the rest of the benchmark.
- :func:`sweep_dead_workers` removes ``worker_<pid>.json`` snapshots whose
  owning PID is no longer alive, the same way ``benchmarks/_common.py``
  already prunes ``.bg_profile_pool/``.

Falling back gracefully
-----------------------
If auto-login fails (e.g. ``GOOGLE_USER_EMAIL`` / ``GOOGLE_USER_PASSWORD``
are not set, or Google fired a 2FA challenge), :func:`mint_for_current_pid`
returns ``None`` instead of raising. Callers can then fall back to the
legacy persistent-profile or storage_state.json snapshot path.

Refresh semantics
-----------------
A minted snapshot is considered fresh for ``BROWSERGYM_STATE_POOL_TTL``
seconds (default: 25 minutes -- comfortably under Google's ~30 min
``__Secure-1PSIDTS`` rotation window). Past that, the next call to
:func:`mint_for_current_pid` re-runs the login flow and overwrites the
file, so long benchmark runs that span multiple rotation windows can
top up their cookies without restarting the worker.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Resolve the pool directory from the env var or default. We do this at
# import time so callers can read ``DEFAULT_POOL_DIR`` if they want to use
# the same path for sweeps / debugging.
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POOL_DIR = _REPO_ROOT / ".bg_storage_state_pool"

# Snapshots older than this are considered stale and will be re-minted on
# the next ``mint_for_current_pid`` call. Configurable via env so users can
# tune for slower / faster Google rotation behavior without code changes.
_DEFAULT_STALE_AFTER_SECONDS = 25 * 60


def _pool_dir() -> Path:
    raw = os.environ.get("BROWSERGYM_STATE_POOL_DIR")
    return Path(raw) if raw else DEFAULT_POOL_DIR


def _stale_after_seconds() -> int:
    raw = os.environ.get("BROWSERGYM_STATE_POOL_TTL")
    if not raw:
        return _DEFAULT_STALE_AFTER_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_STALE_AFTER_SECONDS
    return max(60, value)  # never trust "refresh every 0 seconds"


def _state_path_for_pid(pid: int) -> Path:
    return _pool_dir() / f"worker_{pid}.json"


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def sweep_dead_workers(pool_dir: Optional[Path] = None) -> None:
    """Delete ``worker_<pid>.json`` files whose PID is no longer running.

    Live worker snapshots are left alone so concurrent runs don't yank
    each other's state out from under them. Best-effort: any I/O failure
    is logged at DEBUG level and ignored (the worst case is a stale file
    sticks around for one more cycle).
    """
    pool = pool_dir or _pool_dir()
    if not pool.is_dir():
        return
    for entry in pool.iterdir():
        if not entry.name.startswith("worker_") or not entry.name.endswith(".json"):
            continue
        try:
            pid_str = entry.stem.removeprefix("worker_")
            pid = int(pid_str)
        except ValueError:
            continue
        if _is_alive(pid):
            continue
        try:
            entry.unlink()
        except OSError as exc:  # pragma: no cover -- best-effort cleanup
            logger.debug("Could not delete stale worker state %s: %s", entry, exc)


def _acquire_mint_lock(pool: Path, *, timeout_s: float = 120.0) -> Optional[int]:
    """Best-effort cross-process lock for the auto-login subprocess.

    Returns a file descriptor (caller must :func:`os.close` it after the
    lock is no longer needed). Returns ``None`` if locking is unsupported
    on this platform; the caller should proceed without locking in that
    case (which is fine for single-worker setups).
    """
    pool.mkdir(parents=True, exist_ok=True)
    lock_path = pool / "mint.lock"

    try:
        import fcntl
    except ImportError:  # pragma: no cover -- non-POSIX platforms
        return None

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.time() + timeout_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.time() >= deadline:
                os.close(fd)
                return None
            time.sleep(1.0)


def _release_mint_lock(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001 -- best-effort
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _is_fresh(path: Path) -> bool:
    """Return True if *path* exists and is younger than the TTL."""
    if not path.is_file():
        return False
    age = time.time() - path.stat().st_mtime
    return age < _stale_after_seconds()


def _run_auto_login(output: Path, *, timeout_s: float = 180.0) -> bool:
    """Spawn ``google_auto_login.py`` as a subprocess and wait for it.

    We use a subprocess instead of calling ``perform_login`` in-process
    so the Playwright instance the worker later launches doesn't share
    state with the login flow. Returns True on success, False on any
    failure (the caller falls back to the legacy auth path).
    """
    script_path = Path(__file__).with_name("google_auto_login.py")
    cmd = [
        sys.executable,
        str(script_path),
        "--output",
        str(output),
        "--headless",
    ]
    logger.info("Running auto-login: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("Auto-login timed out after %.1fs: %s", timeout_s, exc)
        return False
    except Exception as exc:  # noqa: BLE001 -- last-resort
        logger.warning("Auto-login subprocess failed to start: %s", exc)
        return False

    if result.returncode != 0:
        logger.warning(
            "Auto-login subprocess exited with %d. stderr=%s",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False

    if not output.is_file():
        logger.warning("Auto-login claimed success but %s does not exist.", output)
        return False

    return True


def mint_for_current_pid(*, force: bool = False) -> Optional[Path]:
    """Return a path to a fresh storage_state.json for the current PID.

    The file lives at ``<pool>/worker_<pid>.json``. If a fresh snapshot
    already exists (younger than the TTL), its path is returned without
    re-running the login flow. Set ``force=True`` to always re-mint.

    Returns ``None`` (not a path) when the auto-login flow can't run for
    any reason -- callers must fall back to a different auth strategy
    rather than treating ``None`` as success.
    """
    pid = os.getpid()
    pool = _pool_dir()
    state_path = _state_path_for_pid(pid)

    # Fast path: already minted within TTL.
    if not force and _is_fresh(state_path):
        return state_path

    # Hold the directory-wide mint lock so 5 parallel workers don't all log
    # in simultaneously (Google rate-limits + flaky 2FA prompts in that
    # case). Workers re-check freshness *after* acquiring the lock so they
    # cooperatively share a single mint when contention is high.
    lock_fd = _acquire_mint_lock(pool)
    try:
        # Another worker may have refreshed the file while we were waiting
        # for the lock. Skip the redundant login if so.
        if not force and _is_fresh(state_path):
            return state_path

        sweep_dead_workers(pool)

        ok = _run_auto_login(state_path)
        if not ok:
            # Wipe any partial output so future calls don't think it's fresh.
            try:
                state_path.unlink()
            except FileNotFoundError:
                pass
            return None

        return state_path
    finally:
        _release_mint_lock(lock_fd)


__all__ = [
    "DEFAULT_POOL_DIR",
    "mint_for_current_pid",
    "sweep_dead_workers",
]
