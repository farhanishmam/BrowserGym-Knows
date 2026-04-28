import os
import shutil
import sys
from pathlib import Path

# Prefer this repo's local browsergym sub-packages (e.g. the "knows" action
# subset and the knows_docs_1 benchmark) over the older pip-installed wheels.
# We set PYTHONPATH (in addition to sys.path) so that Ray worker subprocesses
# also pick up the local sources; otherwise they fall back to site-packages
# and fail with `Unknown high-level action subspace: knows`.
_repo_root = Path(__file__).resolve().parent
_bg_local_srcs = [
    _repo_root / "browsergym" / "core" / "src",
    _repo_root / "browsergym" / "experiments" / "src",
    _repo_root / "browsergym" / "knows" / "src",
]
_bg_local_srcs = [str(p) for p in _bg_local_srcs if p.is_dir()]
# Repo root is added so Ray worker subprocesses can import ``scripts.*``
# (notably ``scripts.storage_state_pool`` for the per-worker auto-login mint).
_pythonpath_entries = _bg_local_srcs + [str(_repo_root)]
for _p in reversed(_pythonpath_entries):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_existing_pp = os.environ.get("PYTHONPATH", "")
_pp_parts = _existing_pp.split(os.pathsep) if _existing_pp else []
_new_pp = _pythonpath_entries + [p for p in _pp_parts if p not in _pythonpath_entries]
os.environ["PYTHONPATH"] = os.pathsep.join(_new_pp)

# Authentication wiring. Two modes are supported (selected via
# ``BROWSERGYM_AUTH_MODE``, default ``auto_login``):
#
#   - ``auto_login``: each Ray worker mints its own freshly-validated
#     ``storage_state.json`` at launch via ``scripts/google_auto_login.py``,
#     using the credentials in ``GOOGLE_USER_EMAIL`` / ``GOOGLE_USER_PASSWORD``.
#     This eliminates the manual ``extract_auth_state.py`` step and gives
#     each worker its own Google session (no cookie-rotation collisions).
#
#   - ``persistent_profile``: legacy behavior that reuses (or clones) a
#     persistent Chromium profile at ``playwright_chrome_profile/``. Useful
#     for single-worker debugging or when you don't want to spend an
#     automated login on a quick run.
#
# All env vars wired here are inherited by Ray worker processes spawned
# below; ``browsergym.core.env`` reads them at browser-launch time.
_PROFILE_DIR = _repo_root / "playwright_chrome_profile"
_PROFILE_POOL_DIR = _repo_root / ".bg_profile_pool"
_STATE_POOL_DIR = _repo_root / ".bg_storage_state_pool"
_STATE_POOL_HELPER = _repo_root / "scripts" / "storage_state_pool.py"
_DESIRED_PARALLEL_JOBS = 5

_AUTH_MODE = os.environ.get("BROWSERGYM_AUTH_MODE", "snapshot").strip().lower()
_AUTO_LOGIN_READY = bool(
    os.environ.get("GOOGLE_USER_EMAIL", "").strip()
    and os.environ.get("GOOGLE_USER_PASSWORD", "")
)
_RUN_EVALUATORS = os.environ.get("KNOWS_RUN_EVALUATORS", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def _sweep_dead_workers(pool_dir: Path, *, file_suffix: str = "") -> None:
    """Remove ``worker_<pid>`` entries (dirs or files) whose PID is dead."""
    if not pool_dir.is_dir():
        return
    for _entry in pool_dir.iterdir():
        if not _entry.name.startswith("worker_"):
            continue
        try:
            stem = _entry.stem if file_suffix else _entry.name
            _pid = int(stem.removeprefix("worker_"))
        except ValueError:
            continue
        try:
            os.kill(_pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                if _entry.is_dir():
                    shutil.rmtree(_entry, ignore_errors=True)
                else:
                    _entry.unlink()
            except OSError:
                pass


if _AUTH_MODE == "snapshot":
    # Snapshot mode: every worker loads the same on-disk storage_state.json
    # (refreshed by run.sh's extract_auth_state.py preflight). Make sure no
    # legacy mode env vars stick around to override the fallback path.
    for _name in (
        "BROWSERGYM_AUTO_LOGIN",
        "BROWSERGYM_PERSISTENT_PROFILE",
        "BROWSERGYM_PERSISTENT_PARALLEL",
        "BROWSERGYM_PERSISTENT_POOL_DIR",
        "BROWSERGYM_PERSISTENT_CHANNEL",
    ):
        os.environ.pop(_name, None)
elif _AUTH_MODE == "auto_login" and _AUTO_LOGIN_READY:
    os.environ["BROWSERGYM_AUTO_LOGIN"] = "1"
    os.environ.setdefault("BROWSERGYM_STATE_POOL_DIR", str(_STATE_POOL_DIR))
    if _STATE_POOL_HELPER.is_file():
        os.environ.setdefault("BROWSERGYM_STATE_POOL_HELPER", str(_STATE_POOL_HELPER))
    # Make sure the legacy persistent-profile path doesn't also fire.
    for _name in (
        "BROWSERGYM_PERSISTENT_PROFILE",
        "BROWSERGYM_PERSISTENT_PARALLEL",
        "BROWSERGYM_PERSISTENT_POOL_DIR",
        "BROWSERGYM_PERSISTENT_CHANNEL",
    ):
        os.environ.pop(_name, None)
    _sweep_dead_workers(_STATE_POOL_DIR, file_suffix=".json")
elif _PROFILE_DIR.is_dir():
    os.environ["BROWSERGYM_PERSISTENT_PROFILE"] = str(_PROFILE_DIR)
    os.environ.setdefault("BROWSERGYM_PERSISTENT_CHANNEL", "chrome")
    os.environ["BROWSERGYM_PERSISTENT_POOL_DIR"] = str(_PROFILE_POOL_DIR)
    if _DESIRED_PARALLEL_JOBS > 1:
        os.environ["BROWSERGYM_PERSISTENT_PARALLEL"] = "1"

    # Sweep clones whose owning PID is no longer alive so the pool doesn't
    # grow without bound across runs. Live worker dirs (typically none at
    # this point, since Ray hasn't started yet) are left alone.
    _sweep_dead_workers(_PROFILE_POOL_DIR)

from agentlab.agents.generic_agent import AGENT_4o_MINI
from agentlab.agents.generic_agent import AGENT_4o
from agentlab.agents.generic_agent import AGENT_GPT55
from agentlab.agents.generic_agent import AGENT_OPUS_47
from agentlab.agents.generic_agent import AGENT_GEMINI_31_PRO
from agentlab.experiments.study import make_study
from agentlab.experiments.study import Study

from browsergym.experiments.benchmark import DEFAULT_BENCHMARKS

# Pick the agent first so we can stamp its name onto each task's kwargs.
# Swap to AGENT_OPUS_47 (Anthropic Claude Opus 4.7) or AGENT_GEMINI_31_PRO
# (Google Gemini 3.1 Pro Preview via Vertex AI) to compare frontier models.
AGENT = AGENT_GEMINI_31_PRO_AXT

# Pick the benchmark split. Kept as a named constant so the output-tree
# routing below can derive its `<split>/` subdir without re-parsing the
# benchmark object. Override via the `KNOWS_BENCHMARK` env var to swap
# splits without editing this file (mirrors `benchmarks/_common.py`).
BENCHMARK_NAME = os.environ.get("KNOWS_BENCHMARK", "").strip() or "knows_docs_1"

# Load the benchmark configuration (knows_docs_1 covers all 5 docs_1_formal_letter instances).
benchmark = DEFAULT_BENCHMARKS[BENCHMARK_NAME]()

# Authentication strategy:
#  - If BROWSERGYM_AUTO_LOGIN=1 is set (above), each Ray worker mints its
#    own fresh storage_state via the stealth Playwright login flow. We
#    skip env_args.storage_state because it would just be overwritten by
#    the per-worker mint inside browsergym.core.env.
#  - Else if BROWSERGYM_PERSISTENT_PROFILE is set, each Playwright launch
#    reuses (or clones) the on-disk Chrome profile, which keeps Google
#    session cookies fresh automatically. storage_state.json is unused.
#  - Otherwise we fall back to the snapshot in storage_state.json, which
#    goes stale within hours-to-days because Google rotates SIDTS/SIDCC.
STORAGE_STATE_FILE = "storage_state.json"
_use_auto_login = os.environ.get("BROWSERGYM_AUTO_LOGIN", "").lower() in (
    "1",
    "true",
    "yes",
)
_use_persistent_profile = bool(os.environ.get("BROWSERGYM_PERSISTENT_PROFILE"))
_use_persistent_parallel = (
    os.environ.get("BROWSERGYM_PERSISTENT_PARALLEL", "").lower() in ("1", "true", "yes")
)

# In auto-login mode, the legacy snapshot is the *fallback* path: when the
# per-worker mint inside ``browsergym.core.env`` succeeds it overrides this
# with the fresh per-worker file; when mint fails we still want Chromium to
# launch with *some* auth rather than the sign-in page (which silently
# breaks every task). The persistent-profile mode rejects storage_state, so
# we leave it unset in that branch.
_fallback_storage_state = STORAGE_STATE_FILE if Path(STORAGE_STATE_FILE).is_file() else None

for env_args in benchmark.env_args_list:
    if not _use_persistent_profile and _fallback_storage_state:
        env_args.storage_state = _fallback_storage_state
    # Pass the agent name through so the task's setup() can pre-create a
    # uniquely-named Google Doc (e.g. "<agent_name>_docs_1_formal_letter_instance_1").
    # Evaluators stay off for benchmark runs unless KNOWS_RUN_EVALUATORS is set.
    env_args.task_kwargs = {
        "agent_name": AGENT.agent_name,
        "run_evaluator": _RUN_EVALUATORS,
    }

study = make_study(
    benchmark=benchmark,
    agent_args=[AGENT],
    comment="Knows Benchmark with Google Auth",
)


def _results_root_for_agent(agent_args) -> str:
    """Pick the top-level output tree based on the agent's obs flags.

    Mirrors the routing baked into `run.sh`:
      - axt only            -> `final_axt/`
      - screenshot only     -> `final_ss/`
      - axt + screenshot    -> `final_axt_ss/`
    """
    obs = agent_args.flags.obs
    use_axt = bool(getattr(obs, "use_ax_tree", False))
    use_screenshot = bool(getattr(obs, "use_screenshot", False))
    if use_axt and use_screenshot:
        return "final_axt_ss"
    if use_screenshot:
        return "final_ss"
    return "final_axt"


def _model_subdir_for_agent(agent_args) -> str:
    """Pick the `<model>/` subdir from the agent's chat model name.

    Matches the `gpt` / `claude` / `gemini` folders that `run.sh` writes
    under each `final_*` tree (see `model_subdir_for_script`).
    """
    model_id = (agent_args.chat_model_args.model_name or "").lower()
    if "gpt" in model_id:
        return "gpt"
    if "claude" in model_id or "opus" in model_id:
        return "claude"
    if "gemini" in model_id:
        return "gemini"
    raise ValueError(f"Cannot infer model subdir from model_name={model_id!r}")


def _split_subdir_for_benchmark(benchmark_name: str) -> str:
    """Map `knows_<family>_<num>` -> `<num>_<family>` (e.g. `2_sheets`).

    Matches the layout produced by `run.sh:split_subdir_for_benchmark`.
    """
    parts = benchmark_name.split("_")
    if len(parts) != 3 or parts[0] != "knows":
        raise ValueError(
            f"Cannot infer split subdir from benchmark={benchmark_name!r}; "
            "expected 'knows_<family>_<num>' (e.g. 'knows_sheets_2')."
        )
    _, family, num = parts
    return f"{num}_{family}"


# Honor `AGENTLAB_EXP_ROOT` as an explicit override (so `run.sh` and other
# orchestration scripts can keep redirecting outputs without touching this
# file). Otherwise auto-route into `<final_*>/<model>/<split>/` so direct
# `python benchmark.py` runs land alongside the per-model scripts under
# `benchmarks/`.
_env_root = os.environ.get("AGENTLAB_EXP_ROOT", "").strip()
if _env_root:
    study.dir = Path(_env_root)
else:
    study.dir = (
        _repo_root
        / _results_root_for_agent(AGENT)
        / _model_subdir_for_agent(AGENT)
        / _split_subdir_for_benchmark(BENCHMARK_NAME)
    )
study.dir.mkdir(parents=True, exist_ok=True)

# Run the study.
#  - auto-login: per-worker minted snapshots, so any number of parallel
#    workers is fine. Cap at _DESIRED_PARALLEL_JOBS.
#  - persistent + parallel: each Ray worker gets its own profile clone, so we
#    can saturate up to _DESIRED_PARALLEL_JOBS workers.
#  - persistent + serial: a single profile dir can only be opened by one
#    Chromium process at a time (SingletonLock), so n_jobs must be 1.
#  - no persistent profile, no auto-login: original snapshot-based behavior.
if _use_auto_login:
    _n_jobs = min(_DESIRED_PARALLEL_JOBS, len(benchmark.env_args_list))
elif _use_persistent_profile and _use_persistent_parallel:
    _n_jobs = min(_DESIRED_PARALLEL_JOBS, len(benchmark.env_args_list))
elif _use_persistent_profile:
    _n_jobs = 1
else:
    _n_jobs = min(5, len(benchmark.env_args_list))

study.run(n_jobs=_n_jobs)
