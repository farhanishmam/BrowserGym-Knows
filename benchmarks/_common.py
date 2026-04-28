"""Shared boilerplate for the per-model benchmark scripts in this folder.

Each script in `benchmarks/` does roughly the same thing: pick a single
`GenericAgentArgs` config, attach a `knows_*` benchmark, and dispatch a
parallel run via `agentlab.experiments.study.make_study`.

The benchmark is configurable: callers can either pass it explicitly (via
the `benchmark_name=` kwarg) or, more commonly, set the `KNOWS_BENCHMARK`
env var before launching the script. This is what `run.sh` does so the
same per-model script can be reused across `knows_docs_1`, `knows_sheets_2`,
`knows_docs_5`, `knows_sheets_6`, `knows_slides_17`, `knows_slides_20`, and
`knows_sheets_38` without duplicating files per split.

Authentication strategy (mirrors the top-level `benchmark.py`):
  - Default ("auto_login"): each Ray worker runs `scripts/google_auto_login.py`
    at startup to mint its own freshly-validated `storage_state.json` snapshot
    via a stealth Playwright email + password flow. Per-worker minting solves
    the cookie-rotation collisions that plague any approach where 5 parallel
    workers share a single source session, and removes the manual
    `extract_auth_state.py` step entirely. Credentials come from
    `GOOGLE_USER_EMAIL` / `GOOGLE_USER_PASSWORD` in `.env`.
  - Legacy fallback ("persistent_profile"): if `playwright_chrome_profile/`
    exists at the repo root, we expose it via `BROWSERGYM_PERSISTENT_PROFILE`
    so each Playwright launch reuses (or clones) the on-disk Chrome profile.
    Useful for single-worker debugging when you don't want to burn an
    automated login on a quick test.
  - Last-resort fallback: `storage_state.json` snapshot at the repo root,
    which is a one-shot snapshot and goes stale within hours-to-days.

Mode is selected by the `BROWSERGYM_AUTH_MODE` env var (default
``"auto_login"``). Parallel mode (`BROWSERGYM_PERSISTENT_PARALLEL=1`) is
still supported for the persistent-profile path: it clones the source
profile to a per-PID directory under `<profile>/../.bg_profile_pool`. The
auto-login path doesn't need that escape hatch because each worker gets
its own freshly-minted snapshot from
`<repo>/.bg_storage_state_pool/worker_<pid>.json`.

The only thing that differs between the 9 per-model scripts is the agent
(model x observation-mode combination), so we factor the rest out here.
"""

from __future__ import annotations

import os
import shutil
import sys
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing agentlab at module load
    from agentlab.agents.generic_agent import GenericAgentArgs


# The repo root is the parent of this `benchmarks/` directory; we resolve it
# from __file__ so the scripts work regardless of the cwd they're launched in.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Prefer this repo's local browsergym sub-packages (e.g. the "knows" action
# subset and the knows_docs_1 benchmark) over the older pip-installed wheels.
# We set PYTHONPATH (in addition to sys.path) so that Ray worker subprocesses
# also pick up the local sources; otherwise they fall back to site-packages
# and fail with `Unknown high-level action subspace: knows`.
_BG_LOCAL_SRCS = [
    _REPO_ROOT / "browsergym" / "core" / "src",
    _REPO_ROOT / "browsergym" / "experiments" / "src",
    _REPO_ROOT / "browsergym" / "knows" / "src",
]
_BG_LOCAL_SRCS = [str(p) for p in _BG_LOCAL_SRCS if p.is_dir()]

# Repo root is added so Ray worker subprocesses can import ``scripts.*``
# (notably ``scripts.storage_state_pool`` for the per-worker auto-login mint).
_REPO_ROOT_STR = str(_REPO_ROOT)
_PYTHONPATH_ENTRIES = _BG_LOCAL_SRCS + [_REPO_ROOT_STR]

for _p in reversed(_PYTHONPATH_ENTRIES):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_existing_pp = os.environ.get("PYTHONPATH", "")
_pp_parts = _existing_pp.split(os.pathsep) if _existing_pp else []
_new_pp = _PYTHONPATH_ENTRIES + [p for p in _pp_parts if p not in _PYTHONPATH_ENTRIES]
os.environ["PYTHONPATH"] = os.pathsep.join(_new_pp)


# Storage state file produced by `extract_auth_state.py` at the repo root.
# Only used as a fallback when no on-disk Chrome profile is present and
# auto-login is unavailable.
STORAGE_STATE_FILE = str(_REPO_ROOT / "storage_state.json")

# Persistent Chrome profile + per-PID clone pool. The clone pool lives next to
# the profile (configurable via BROWSERGYM_PERSISTENT_POOL_DIR) and is shared
# across all per-model scripts; each Ray worker grabs its own `worker_<pid>/`
# clone under it. Only consulted in legacy "persistent_profile" mode.
_PROFILE_DIR = _REPO_ROOT / "playwright_chrome_profile"
_PROFILE_POOL_DIR = _REPO_ROOT / ".bg_profile_pool"

# Where ``scripts/storage_state_pool.py`` writes its per-PID minted Google
# session snapshots. Pinned in env so dynamic-path-based imports inside
# ``browsergym.core.env`` can locate the helper module even when the
# repo root is missing from PYTHONPATH for some reason.
_STATE_POOL_DIR = _REPO_ROOT / ".bg_storage_state_pool"
_STATE_POOL_HELPER = _REPO_ROOT / "scripts" / "storage_state_pool.py"

# Default cap on parallel workers per study. Override per-script via the
# `n_jobs=` argument to `run_knows_benchmark`, or globally (e.g. from
# `run.sh`) via the `BROWSERGYM_N_JOBS` env var.
DEFAULT_PARALLEL_JOBS = 5

# Auth modes selectable via ``BROWSERGYM_AUTH_MODE``:
#
# - ``snapshot`` *(default, recommended for production)*: every worker
#   loads the same on-disk ``storage_state.json``. Combined with the
#   ``extract_auth_state.py`` preflight in ``run.sh`` (which refreshes
#   the snapshot before every benchmark), this gives recoverable auth
#   between splits without per-worker contention on the persistent
#   profile.
#
# - ``auto_login``: each Ray worker mints its own storage_state at
#   launch time by opening the persistent Chromium profile via the
#   ``scripts/storage_state_pool`` helper. Useful when you want every
#   worker to have a totally independent cookie set, but the per-PID
#   profile-extract dance is fragile under heavy parallelism.
#
# - ``persistent_profile``: legacy mode where each worker launches a
#   ``persistent_context`` directly off the on-disk profile (with a
#   per-PID clone pool). Kept as a debugging fallback.
AUTH_MODE_SNAPSHOT = "snapshot"
AUTH_MODE_AUTO_LOGIN = "auto_login"
AUTH_MODE_PERSISTENT_PROFILE = "persistent_profile"
DEFAULT_AUTH_MODE = AUTH_MODE_SNAPSHOT


def _env_flag(name: str, *, default: bool = False) -> bool:
    """Read a bool-like environment variable."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_n_jobs() -> int | None:
    """Read `BROWSERGYM_N_JOBS` from the environment if set to a positive int."""
    raw = os.environ.get("BROWSERGYM_N_JOBS")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _configure_persistent_profile(parallel: bool) -> bool:
    """Wire up the persistent-profile env vars if the profile dir exists.

    Returns True if a persistent profile is available (callers should then
    skip `storage_state` injection and let Chromium reuse the on-disk
    cookies). Returns False otherwise so callers can fall back to the
    storage_state snapshot.
    """

    if not _PROFILE_DIR.is_dir():
        return False

    os.environ["BROWSERGYM_PERSISTENT_PROFILE"] = str(_PROFILE_DIR)
    os.environ.setdefault("BROWSERGYM_PERSISTENT_CHANNEL", "chrome")
    os.environ["BROWSERGYM_PERSISTENT_POOL_DIR"] = str(_PROFILE_POOL_DIR)
    if parallel:
        os.environ["BROWSERGYM_PERSISTENT_PARALLEL"] = "1"
    else:
        os.environ.pop("BROWSERGYM_PERSISTENT_PARALLEL", None)

    # Sweep clones whose owning PID is no longer alive so the pool doesn't
    # grow without bound across runs. Live worker dirs are left alone so
    # other concurrently-running scripts don't have their clones yanked.
    if _PROFILE_POOL_DIR.is_dir():
        for entry in _PROFILE_POOL_DIR.iterdir():
            if not entry.name.startswith("worker_"):
                continue
            try:
                pid = int(entry.name.removeprefix("worker_"))
            except ValueError:
                continue
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError, OSError):
                shutil.rmtree(entry, ignore_errors=True)

    return True


def _auto_login_credentials_present() -> bool:
    """Return True iff the auto-login flow has the credentials it needs.

    Falls through to a False return without raising so callers can decide
    whether to log a warning, fall back to the persistent-profile path,
    or both.
    """
    return bool(
        os.environ.get("GOOGLE_USER_EMAIL", "").strip()
        and os.environ.get("GOOGLE_USER_PASSWORD", "")
    )


def _configure_auto_login() -> bool:
    """Wire up the per-worker auto-login mint for ``browsergym.core.env``.

    Returns True if auto-login was successfully configured (callers must
    then NOT also configure the persistent-profile env vars, since the two
    auth paths would otherwise fight). Returns False when credentials are
    missing so callers can fall back to the legacy auth strategies.
    """
    if not _auto_login_credentials_present():
        return False

    os.environ["BROWSERGYM_AUTO_LOGIN"] = "1"
    # Pin the helper paths so ``browsergym.core.env._mint_per_worker_storage_state``
    # can locate them even if PYTHONPATH gets mangled by a downstream tool.
    os.environ.setdefault("BROWSERGYM_STATE_POOL_DIR", str(_STATE_POOL_DIR))
    if _STATE_POOL_HELPER.is_file():
        os.environ.setdefault("BROWSERGYM_STATE_POOL_HELPER", str(_STATE_POOL_HELPER))

    # Auto-login owns the parallelism story; clear the persistent-profile
    # env vars so the legacy clone-pool path doesn't also fire.
    for name in (
        "BROWSERGYM_PERSISTENT_PROFILE",
        "BROWSERGYM_PERSISTENT_PARALLEL",
        "BROWSERGYM_PERSISTENT_POOL_DIR",
        "BROWSERGYM_PERSISTENT_CHANNEL",
    ):
        os.environ.pop(name, None)

    # Sweep stale per-PID snapshots so the pool dir doesn't grow without
    # bound across runs. Mirrors the persistent-profile sweeper above.
    if _STATE_POOL_DIR.is_dir():
        for entry in _STATE_POOL_DIR.iterdir():
            if not (entry.name.startswith("worker_") and entry.name.endswith(".json")):
                continue
            try:
                pid = int(entry.stem.removeprefix("worker_"))
            except ValueError:
                continue
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    entry.unlink()
                except OSError:
                    pass

    return True


def _resolve_auth_mode() -> str:
    """Return the auth mode requested for this run.

    Honors ``BROWSERGYM_AUTH_MODE`` if set to a known value; otherwise
    falls back to :data:`DEFAULT_AUTH_MODE`. Unknown values are treated as
    the default with a warning emitted via ``print`` so misconfigured
    runs are visible without failing.
    """
    raw = os.environ.get("BROWSERGYM_AUTH_MODE", "").strip().lower()
    if not raw:
        return DEFAULT_AUTH_MODE
    if raw in (
        AUTH_MODE_SNAPSHOT,
        AUTH_MODE_AUTO_LOGIN,
        AUTH_MODE_PERSISTENT_PROFILE,
    ):
        return raw
    print(
        f"[_common] WARNING: unknown BROWSERGYM_AUTH_MODE={raw!r}; "
        f"falling back to {DEFAULT_AUTH_MODE!r}."
    )
    return DEFAULT_AUTH_MODE


def _configure_snapshot() -> None:
    """Strip per-worker auth env vars so workers fall through to the snapshot.

    ``snapshot`` mode relies on every worker loading the same
    ``storage_state.json`` (kept fresh by ``extract_auth_state.py`` in
    ``run.sh``). We have to actively unset the auto-login / persistent-
    profile vars in case a previous mode left them in the environment.
    """
    for name in (
        "BROWSERGYM_AUTO_LOGIN",
        "BROWSERGYM_PERSISTENT_PROFILE",
        "BROWSERGYM_PERSISTENT_PARALLEL",
        "BROWSERGYM_PERSISTENT_POOL_DIR",
        "BROWSERGYM_PERSISTENT_CHANNEL",
    ):
        os.environ.pop(name, None)


# Default benchmark when neither a `benchmark_name=` kwarg nor a
# `KNOWS_BENCHMARK` env var is provided. Picked to preserve the historical
# behavior of `run_knows_docs_1` (which always ran the docs_1 split).
_DEFAULT_BENCHMARK_NAME = "knows_docs_1"


def _resolve_benchmark_name(explicit: str | None) -> str:
    """Pick the benchmark name to run. Precedence: explicit kwarg >
    `KNOWS_BENCHMARK` env var > `_DEFAULT_BENCHMARK_NAME`."""
    if explicit:
        return explicit
    env_value = os.environ.get("KNOWS_BENCHMARK", "").strip()
    if env_value:
        return env_value
    return _DEFAULT_BENCHMARK_NAME


def _split_task_names(value: str) -> list[str]:
    return [part.strip() for part in value.replace(",", " ").split() if part.strip()]


def _resolve_task_names(explicit: Sequence[str] | str | None) -> list[str] | None:
    """Resolve an optional per-task filter from args or ``KNOWS_TASKS``."""
    if explicit is None:
        env_value = os.environ.get("KNOWS_TASKS", "").strip()
        if not env_value:
            return None
        return _split_task_names(env_value) or None
    if isinstance(explicit, str):
        return _split_task_names(explicit) or None
    return list(explicit) or None


def _resolve_existing_doc_ids() -> dict[str, str]:
    """Read optional task-name -> workspace document ID mappings from env.

    Preferred format is JSON, e.g.
    ``{"knows.docs_1_formal_letter.4": "doc_id"}``. For shell convenience,
    ``task=doc_id,task=doc_id`` is also accepted.
    """

    raw = os.environ.get("KNOWS_EXISTING_DOC_IDS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {}
        for item in raw.replace(",", " ").split():
            if "=" not in item:
                raise ValueError(
                    "KNOWS_EXISTING_DOC_IDS must be JSON or task=doc_id pairs; "
                    f"could not parse {item!r}."
                )
            task_name, doc_id = item.split("=", 1)
            parsed[task_name.strip()] = doc_id.strip()
    if not isinstance(parsed, dict):
        raise ValueError("KNOWS_EXISTING_DOC_IDS must map task names to document IDs.")
    return {
        str(task_name).strip(): str(doc_id).strip()
        for task_name, doc_id in parsed.items()
        if str(task_name).strip() and str(doc_id).strip()
    }


def run_knows_benchmark(
    agent: "GenericAgentArgs",
    *,
    benchmark_name: str | None = None,
    task_names: Sequence[str] | str | None = None,
    comment: str | None = None,
    n_jobs: int | None = None,
    results_dir: Path | None = None,
) -> None:
    """Run a `knows_*` benchmark with a single agent config.

    Args:
        agent: The pre-built `GenericAgentArgs` to run (e.g. AGENT_GPT55_AXT).
        benchmark_name: Which split to run, e.g. ``"knows_docs_1"`` or
            ``"knows_sheets_2"``. Defaults to the value of the
            ``KNOWS_BENCHMARK`` env var, falling back to ``"knows_docs_1"``.
        task_names: Optional task ids to run from the selected benchmark. When
            omitted, the ``KNOWS_TASKS`` env var can provide a comma/space
            separated list, e.g. ``"knows.sheets_2_personal_recipe.5"``.
        comment: Optional study comment; defaults to the agent's own name.
        n_jobs: Optional override for parallel jobs (capped by task count).
            Defaults to `DEFAULT_PARALLEL_JOBS` when the persistent profile is
            available, falling back to `min(5, task_count)` otherwise.
        results_dir: Optional override for where the study writes outputs.

    Evaluators are disabled by default; set ``KNOWS_RUN_EVALUATORS=1`` to
    grade each task at DONE/finalize time.
    """

    benchmark_name = _resolve_benchmark_name(benchmark_name)
    resolved_task_names = _resolve_task_names(task_names)
    existing_doc_ids = _resolve_existing_doc_ids()

    # Decide on parallelism *before* configuring the auth path, since whether
    # to enable parallel-clone mode depends on whether we'll launch multiple
    # workers. We can't know the final n_jobs until we've loaded the
    # benchmark, but we can make the right env-var decision based on the
    # caller's (or env var's) request. Precedence: explicit kwarg >
    # `BROWSERGYM_N_JOBS` env var > `DEFAULT_PARALLEL_JOBS`.
    if n_jobs is None:
        n_jobs = _env_n_jobs()
    use_parallel = (n_jobs is None and DEFAULT_PARALLEL_JOBS > 1) or (
        n_jobs is not None and n_jobs > 1
    )

    # Resolve auth mode and wire up the corresponding env vars. ``snapshot``
    # is the default: every worker loads the same on-disk
    # ``storage_state.json`` and ``run.sh`` refreshes that file via
    # ``extract_auth_state.py`` between splits. ``auto_login`` and
    # ``persistent_profile`` remain available for debugging.
    auth_mode = _resolve_auth_mode()
    use_auto_login = False
    use_persistent_profile = False
    if auth_mode == AUTH_MODE_SNAPSHOT:
        _configure_snapshot()
    elif auth_mode == AUTH_MODE_AUTO_LOGIN:
        use_auto_login = _configure_auto_login()
        if not use_auto_login:
            print(
                "[_common] auto-login requested but GOOGLE_USER_EMAIL / "
                "GOOGLE_USER_PASSWORD are not set; falling back to the "
                "persistent-profile path."
            )
            use_persistent_profile = _configure_persistent_profile(parallel=use_parallel)
    else:
        use_persistent_profile = _configure_persistent_profile(parallel=use_parallel)

    # These imports rely on the sys.path/PYTHONPATH adjustments above and on
    # the persistent-profile env vars, so we defer them until here.
    from agentlab.experiments.study import make_study
    from browsergym.experiments.benchmark import DEFAULT_BENCHMARKS

    if benchmark_name not in DEFAULT_BENCHMARKS:
        available = sorted(n for n in DEFAULT_BENCHMARKS if n.startswith("knows_"))
        raise ValueError(
            f"Unknown benchmark {benchmark_name!r}; expected one of "
            f"{available} (set via the KNOWS_BENCHMARK env var or the "
            "benchmark_name= kwarg)."
        )

    if use_auto_login:
        resolved_mode_label = AUTH_MODE_AUTO_LOGIN
    elif use_persistent_profile:
        resolved_mode_label = AUTH_MODE_PERSISTENT_PROFILE
    else:
        resolved_mode_label = AUTH_MODE_SNAPSHOT
    benchmark = DEFAULT_BENCHMARKS[benchmark_name]()
    if resolved_task_names:
        benchmark = benchmark.subset_from_list(
            resolved_task_names,
            benchmark_name_suffix="selected",
        )
    run_evaluators = _env_flag("KNOWS_RUN_EVALUATORS", default=False)

    print(
        f"[_common] running benchmark={benchmark.name} agent={agent.agent_name} "
        f"auth_mode={resolved_mode_label} run_evaluators={run_evaluators}"
    )
    if resolved_task_names:
        print(f"[_common] selected tasks: {', '.join(resolved_task_names)}")
    if existing_doc_ids:
        print(
            "[_common] continuing existing workspace docs for: "
            + ", ".join(sorted(existing_doc_ids))
        )

    # Persistent-profile mode supplies its own user-data-dir and rejects
    # ``storage_state``, so we leave ``env_args.storage_state`` unset in
    # that branch. In every other branch we point ``env_args.storage_state``
    # at the legacy on-disk snapshot when one exists. In auto-login mode
    # this is the *fallback* path: when ``_mint_per_worker_storage_state``
    # in ``browsergym.core.env`` succeeds it overrides this with the fresh
    # per-worker file; when it fails (creds missing, 2FA challenge, etc.)
    # we still launch Chromium with the (possibly stale) snapshot rather
    # than with no auth at all -- which previously caused workers to
    # silently time out trying to create a Sheet on the sign-in page.
    fallback_storage_state = (
        STORAGE_STATE_FILE if Path(STORAGE_STATE_FILE).is_file() else None
    )

    for env_args in benchmark.env_args_list:
        if not use_persistent_profile and fallback_storage_state:
            env_args.storage_state = fallback_storage_state
        # Pass the agent name through so the task's setup() can pre-create a
        # uniquely-named Google workspace file (e.g.
        # "<agent_name>_sheets_2_personal_recipe_instance_1"). Evaluators stay
        # off for benchmark runs unless KNOWS_RUN_EVALUATORS is explicitly set.
        env_args.task_kwargs = {
            "agent_name": agent.agent_name,
            "run_evaluator": run_evaluators,
        }
        existing_doc_id = existing_doc_ids.get(env_args.task_name)
        if existing_doc_id:
            env_args.task_kwargs["existing_doc_id"] = existing_doc_id

    study = make_study(
        benchmark=benchmark,
        agent_args=[agent],
        comment=comment or f"Knows Benchmark ({benchmark.name}) - {agent.agent_name}",
    )

    if results_dir is not None:
        study.dir = Path(results_dir)
    else:
        # Honor agentlab's standard env var so callers (e.g. run.sh) can redirect
        # outputs without having to pass results_dir through every script.
        env_root = os.environ.get("AGENTLAB_EXP_ROOT")
        study.dir = Path(env_root) if env_root else _REPO_ROOT / "results"

    # Resolve final n_jobs:
    #  - auto-login: per-worker minted snapshots, so any number of parallel
    #    workers is fine. Cap at caller / DEFAULT_PARALLEL_JOBS, bounded by
    #    task count.
    #  - persistent + parallel: cap at DEFAULT_PARALLEL_JOBS or caller's value,
    #    bounded by task count.
    #  - persistent + serial: a single profile dir can only be opened by one
    #    Chromium process at a time (SingletonLock), so n_jobs must be 1.
    #  - storage_state snapshot only: original parallel behavior.
    task_count = len(benchmark.env_args_list)
    if use_auto_login:
        resolved_n_jobs = min(n_jobs or DEFAULT_PARALLEL_JOBS, task_count)
    elif use_persistent_profile and use_parallel:
        resolved_n_jobs = min(n_jobs or DEFAULT_PARALLEL_JOBS, task_count)
    elif use_persistent_profile:
        resolved_n_jobs = 1
    else:
        resolved_n_jobs = min(n_jobs if n_jobs is not None else 5, task_count)

    parallel_backend = os.environ.get("BROWSERGYM_PARALLEL_BACKEND", "ray").strip() or "ray"
    study.run(n_jobs=resolved_n_jobs, parallel_backend=parallel_backend)


def run_knows_docs_1(
    agent: "GenericAgentArgs",
    *,
    comment: str | None = None,
    n_jobs: int | None = None,
    results_dir: Path | None = None,
) -> None:
    """Backwards-compatible wrapper: always runs ``knows_docs_1``.

    Prefer :func:`run_knows_benchmark` in new code so the benchmark can be
    swapped via the ``KNOWS_BENCHMARK`` env var without touching the script.
    """
    run_knows_benchmark(
        agent,
        benchmark_name="knows_docs_1",
        comment=comment,
        n_jobs=n_jobs,
        results_dir=results_dir,
    )
