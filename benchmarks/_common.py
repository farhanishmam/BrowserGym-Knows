"""Shared boilerplate for the per-model benchmark scripts in this folder.

Each script in `benchmarks/` does roughly the same thing: pick a single
`GenericAgentArgs` config, attach a `knows_*` benchmark, and dispatch a
parallel run via `agentlab.experiments.study.make_study`.

The benchmark is configurable: callers can either pass it explicitly (via
the `benchmark_name=` kwarg) or, more commonly, set the `KNOWS_BENCHMARK`
env var before launching the script. This is what `run.sh` does so the
same per-model script can be reused across `knows_docs_1`, `knows_sheets_2`,
`knows_docs_5`, `knows_sheets_6`, `knows_sheets_10`, `knows_slides_17`,
`knows_slides_20`, `knows_sheets_25`, `knows_sheets_38`,
`knows_slides_39`, and `knows_sheets_55` without duplicating files per split.

Authentication strategy (mirrors the top-level `benchmark.py`):
  ``auto_login`` is the only supported mode. Each Ray worker runs
  `scripts/google_auto_login.py` at startup to mint its own
  freshly-validated `storage_state.json` snapshot via a stealth Playwright
  email + password flow. The snapshot lives at
  `<repo>/.bg_storage_state_pool/worker_<pid>.json` -- two workers can
  never share a storage_state file. Credentials come from
  `GOOGLE_USER_EMAIL` / `GOOGLE_USER_PASSWORD` in `.env`.

  Setting `BROWSERGYM_AUTH_MODE` to anything other than ``"auto_login"``
  is a hard error. The legacy ``snapshot`` (every worker reads the same
  storage_state.json) and ``persistent_profile`` (every worker shares an
  on-disk Chromium profile) modes have been removed because both
  violated per-worker isolation under parallel execution.

The only thing that differs between the 9 per-model scripts is the agent
(model x observation-mode combination), so we factor the rest out here.
"""

from __future__ import annotations

import os
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


# Where ``scripts/storage_state_pool.py`` writes its per-PID minted Google
# session snapshots. Pinned in env so dynamic-path-based imports inside
# ``browsergym.core.env`` can locate the helper module even when the
# repo root is missing from PYTHONPATH for some reason.
_STATE_POOL_DIR = _REPO_ROOT / ".bg_storage_state_pool"
_STATE_POOL_HELPER = _REPO_ROOT / "scripts" / "storage_state_pool.py"

# Default cap on parallel workers per study. Keep this conservative because
# Google can invalidate same-account sessions when too many workers run at once.
# Override per-script via the `n_jobs=` argument to `run_knows_benchmark`, or
# globally (e.g. from `run.sh`) via the `BROWSERGYM_N_JOBS` env var.
DEFAULT_PARALLEL_JOBS = 2

# ``auto_login`` is the only mode the harness will accept. The constant
# names are kept (rather than removed) so external callers and the smoke
# test can keep referencing them, but ``DEFAULT_AUTH_MODE`` and
# ``_resolve_auth_mode`` only ever return ``AUTH_MODE_AUTO_LOGIN``.
AUTH_MODE_AUTO_LOGIN = "auto_login"
DEFAULT_AUTH_MODE = AUTH_MODE_AUTO_LOGIN


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


def _auto_login_credentials_present() -> bool:
    """Return True iff the auto-login flow has the credentials it needs."""
    return bool(
        os.environ.get("GOOGLE_USER_EMAIL", "").strip()
        and os.environ.get("GOOGLE_USER_PASSWORD", "")
    )


def _configure_auto_login() -> None:
    """Wire up the per-worker auto-login mint for ``browsergym.core.env``.

    Auto-login is the only supported auth path, so this is unconditional:
    it sets ``BROWSERGYM_AUTO_LOGIN=1`` plus the helper paths, strips any
    leftover persistent-profile env vars (a stray export from a parent
    shell would otherwise reach Playwright), and prunes dead-PID
    snapshots from the pool dir.

    Raises ``SystemExit`` if the Google credentials are missing -- there
    is no fallback, so a misconfigured run must not silently continue.
    """
    if not _auto_login_credentials_present():
        raise SystemExit(
            "GOOGLE_USER_EMAIL / GOOGLE_USER_PASSWORD must be set; "
            "auto_login cannot mint per-worker storage_state without them."
        )

    os.environ["BROWSERGYM_AUTO_LOGIN"] = "1"
    # Pin the helper paths so ``browsergym.core.env._mint_per_worker_storage_state``
    # can locate them even if PYTHONPATH gets mangled by a downstream tool.
    os.environ.setdefault("BROWSERGYM_STATE_POOL_DIR", str(_STATE_POOL_DIR))
    if _STATE_POOL_HELPER.is_file():
        os.environ.setdefault("BROWSERGYM_STATE_POOL_HELPER", str(_STATE_POOL_HELPER))

    for name in (
        "BROWSERGYM_PERSISTENT_PROFILE",
        "BROWSERGYM_PERSISTENT_PARALLEL",
        "BROWSERGYM_PERSISTENT_POOL_DIR",
        "BROWSERGYM_PERSISTENT_CHANNEL",
    ):
        os.environ.pop(name, None)

    # Sweep stale per-PID snapshots so the pool dir doesn't grow without
    # bound across runs.
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


def _resolve_auth_mode() -> str:
    """Return the auth mode requested for this run.

    ``auto_login`` is the only accepted value. An unset env var defaults
    to it; any other value is rejected outright (per-worker storage_state
    is mandatory).
    """
    raw = os.environ.get("BROWSERGYM_AUTH_MODE", "").strip().lower()
    if not raw:
        return DEFAULT_AUTH_MODE
    if raw == AUTH_MODE_AUTO_LOGIN:
        return raw
    raise SystemExit(
        f"BROWSERGYM_AUTH_MODE={raw!r} is not supported; "
        "per-worker auto_login is mandatory. Unset the env var or set "
        "it to 'auto_login'."
    )


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
            Defaults to `DEFAULT_PARALLEL_JOBS`. Per-worker auto-login
            mints isolate every worker's storage_state, so any value is
            safe.
        results_dir: Optional override for where the study writes outputs.

    Evaluators are disabled by default; set ``KNOWS_RUN_EVALUATORS=1`` to
    grade each task at DONE/finalize time.
    """

    benchmark_name = _resolve_benchmark_name(benchmark_name)
    resolved_task_names = _resolve_task_names(task_names)
    existing_doc_ids = _resolve_existing_doc_ids()

    # Caller / env var override for n_jobs. Precedence: explicit kwarg >
    # `BROWSERGYM_N_JOBS` env var > `DEFAULT_PARALLEL_JOBS`.
    if n_jobs is None:
        n_jobs = _env_n_jobs()

    # Auto-login is the only mode the harness supports; ``_resolve_auth_mode``
    # rejects anything else. ``_configure_auto_login`` raises if the
    # Google credentials are missing.
    _resolve_auth_mode()
    _configure_auto_login()

    # These imports rely on the sys.path/PYTHONPATH adjustments above, so
    # we defer them until here.
    from agentlab.experiments.study import make_study
    from browsergym.experiments.benchmark import DEFAULT_BENCHMARKS

    if benchmark_name not in DEFAULT_BENCHMARKS:
        available = sorted(n for n in DEFAULT_BENCHMARKS if n.startswith("knows_"))
        raise ValueError(
            f"Unknown benchmark {benchmark_name!r}; expected one of "
            f"{available} (set via the KNOWS_BENCHMARK env var or the "
            "benchmark_name= kwarg)."
        )

    benchmark = DEFAULT_BENCHMARKS[benchmark_name]()
    if resolved_task_names:
        benchmark = benchmark.subset_from_list(
            resolved_task_names,
            benchmark_name_suffix="selected",
        )
    run_evaluators = _env_flag("KNOWS_RUN_EVALUATORS", default=False)

    print(
        f"[_common] running benchmark={benchmark.name} agent={agent.agent_name} "
        f"auth_mode={AUTH_MODE_AUTO_LOGIN} run_evaluators={run_evaluators}"
    )
    if resolved_task_names:
        print(f"[_common] selected tasks: {', '.join(resolved_task_names)}")
    if existing_doc_ids:
        print(
            "[_common] continuing existing workspace docs for: "
            + ", ".join(sorted(existing_doc_ids))
        )

    # Auto-login is the sole auth path: each Ray worker mints its own
    # storage_state inside ``browsergym.core.env`` at launch time. We
    # deliberately leave ``env_args.storage_state`` unset -- there is no
    # shared snapshot fallback, and a failed mint must fail the task
    # loudly (see env.py) rather than silently relaunching Chromium
    # against a shared file.
    for env_args in benchmark.env_args_list:
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

    # Auto-login mints per-worker snapshots, so any number of parallel
    # workers is safe. Cap at caller's n_jobs (falling through to
    # DEFAULT_PARALLEL_JOBS), bounded by the number of tasks.
    task_count = len(benchmark.env_args_list)
    resolved_n_jobs = min(n_jobs or DEFAULT_PARALLEL_JOBS, task_count)

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
