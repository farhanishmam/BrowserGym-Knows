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

# Authentication wiring. ``auto_login`` is the only supported mode: each
# Ray worker mints its own freshly-validated ``storage_state.json`` at
# launch via ``scripts/google_auto_login.py`` using the credentials in
# ``GOOGLE_USER_EMAIL`` / ``GOOGLE_USER_PASSWORD``. Two workers can never
# share a storage_state file -- the per-PID pool at
# ``.bg_storage_state_pool/worker_<pid>.json`` is the sole source of auth.
#
# The legacy ``snapshot`` and ``persistent_profile`` modes have been
# removed because both violated per-worker isolation under parallel
# execution. Setting ``BROWSERGYM_AUTH_MODE`` to anything other than
# ``auto_login`` is a hard error.
_STATE_POOL_DIR = _repo_root / ".bg_storage_state_pool"
_STATE_POOL_HELPER = _repo_root / "scripts" / "storage_state_pool.py"
_DESIRED_PARALLEL_JOBS = 5

_AUTH_MODE = os.environ.get("BROWSERGYM_AUTH_MODE", "auto_login").strip().lower()
if _AUTH_MODE != "auto_login":
    raise SystemExit(
        f"BROWSERGYM_AUTH_MODE={_AUTH_MODE!r} is not supported; "
        "per-worker auto_login is mandatory. Unset the env var or set "
        "it to 'auto_login'."
    )

_AUTO_LOGIN_READY = bool(
    os.environ.get("GOOGLE_USER_EMAIL", "").strip()
    and os.environ.get("GOOGLE_USER_PASSWORD", "")
)
if not _AUTO_LOGIN_READY:
    raise SystemExit(
        "GOOGLE_USER_EMAIL / GOOGLE_USER_PASSWORD must be set; "
        "auto_login cannot mint per-worker storage_state without them."
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


os.environ["BROWSERGYM_AUTO_LOGIN"] = "1"
os.environ.setdefault("BROWSERGYM_STATE_POOL_DIR", str(_STATE_POOL_DIR))
if _STATE_POOL_HELPER.is_file():
    os.environ.setdefault("BROWSERGYM_STATE_POOL_HELPER", str(_STATE_POOL_HELPER))
# The persistent-profile path is not supported anymore. Strip any leftover
# env vars so a stray export from a parent shell can never reach the
# Playwright launch path inside browsergym.core.env.
for _name in (
    "BROWSERGYM_PERSISTENT_PROFILE",
    "BROWSERGYM_PERSISTENT_PARALLEL",
    "BROWSERGYM_PERSISTENT_POOL_DIR",
    "BROWSERGYM_PERSISTENT_CHANNEL",
):
    os.environ.pop(_name, None)
_sweep_dead_workers(_STATE_POOL_DIR, file_suffix=".json")

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

# Authentication strategy: per-worker auto-login is the only supported
# path. Each Ray worker mints its own fresh storage_state via the stealth
# Playwright login flow inside browsergym.core.env, so we deliberately
# do NOT assign env_args.storage_state -- there is no shared snapshot
# fallback, and a failed mint must fail the task loudly (see env.py)
# instead of silently relaunching Chromium against a shared file.
for env_args in benchmark.env_args_list:
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

# Run the study. Auto-login mints per-worker storage_state files, so any
# number of parallel workers is safe -- we cap at _DESIRED_PARALLEL_JOBS,
# bounded by the number of tasks in the benchmark.
_n_jobs = min(_DESIRED_PARALLEL_JOBS, len(benchmark.env_args_list))

study.run(n_jobs=_n_jobs)
