# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A fork of **BrowserGym** + **AgentLab** wired to run the **Knows benchmark**: a suite of Google Workspace (Docs / Sheets / Slides) tasks for evaluating LLM browser agents. An agent is given a goal plus a freshly-created Google Workspace document, drives a real Chromium browser via Playwright to complete the task, then an evaluator grades the resulting document against per-checkpoint criteria using the Google Workspace APIs.

## Repository layout (the parts that matter)

- [browsergym/](browsergym/) — a monorepo of editable sub-packages (`core`, `experiments`, `knows`, `miniwob`, `webarena`, `webarenalite`, `visualwebarena`, `assistantbench`). The Knows-specific code lives in `core` (browser auth) and `experiments` (benchmark registration), plus the `knows` package below.
- [browsergym/knows/](browsergym/knows/) — **git submodule** (`alexgill321/Agent-Benchmark`). The actual benchmark: task definitions, evaluators, gold data, and Google API integration. **Has its own [CLAUDE.md](browsergym/knows/CLAUDE.md)** — read it before touching evaluators or task data.
- [AgentLab-Knows/](AgentLab-Knows/) — **git submodule** fork of AgentLab. Provides the `GenericAgent` and the `AGENT_*` config constants (one per model × observation-mode) imported by the runner scripts.
- [benchmark.py](benchmark.py) — top-level single-run entry; hard-codes one `AGENT` constant and one benchmark.
- [benchmarks/](benchmarks/) — one thin script per (model, observation-mode), e.g. [opus47_axt.py](benchmarks/opus47_axt.py). Each just imports an `AGENT_*` and calls `run_knows_benchmark` from [benchmarks/_common.py](benchmarks/_common.py).
- [scripts/](scripts/) — operational helpers: auth minting, evaluator runners, regraders. [scripts/_run_common.sh](scripts/_run_common.sh) is the shared bash bootstrap sourced by all `run_*.sh` shells.
- `run*.sh` — orchestration shells (`run.sh` full sweep, `run_one.sh` single combo, `run_selective.sh` skip-if-complete, `run_sequential.sh`). Many ad-hoc `run_<split>_*.sh` scripts at the root are one-off batch launchers — disposable, not part of the core flow.

## How a run is wired (read this before changing the run path)

1. **Benchmark registration** lives in [browsergym/experiments/src/browsergym/experiments/benchmark/configs.py](browsergym/experiments/src/browsergym/experiments/benchmark/configs.py). `KNOWS_SPLITS` + `_make_knows_benchmark` register one `Benchmark` per split named `knows_<family>_<num>` (e.g. `knows_docs_1`, `knows_sheets_38`, `knows_slides_42`) into `DEFAULT_BENCHMARKS`. Each split = 5 instances, `max_steps = KNOWS_MAX_STEPS` (80), action subset `"knows"`, backend `"knows"`.
2. **Gym task registration** lives in [browsergym/knows/src/browsergym/knows/__init__.py](browsergym/knows/src/browsergym/knows/__init__.py). `_register_task_family` registers gym ids `knows.<family>.{1..N}` from task classes.
3. **Task lifecycle** is in [browsergym/knows/src/browsergym/knows/task.py](browsergym/knows/src/browsergym/knows/task.py). `KnowsBenchTask` → `KnowsWorkspaceTask` is the base; each family subclass sets `TASK_FAMILY_FOLDER`, `TASK_ID_PREFIX`, and `AVAILABLE_INSTANCES`. `setup()` creates a fresh per-trial Google doc named `<agent_name>_<family>_instance_<n>`; `validate()` runs the evaluator at DONE/finalize.
4. **Evaluators** are loaded dynamically by `task.py` from the submodule at `browsergym/knows/.../eval/tasks/<family>/instance_<n>/evaluator.py` (paired with `checkpoints.md`). `task.py` rewrites the evaluator module's path constants and points Google auth at `auth-data/` before exec. Scoring types (`Result`/`Checkpoint`/`EvaluationStep`) live in `eval/eval_utils/scoring.py`.

The `_common.py` and `benchmark.py` PYTHONPATH preamble (inserting `browsergym/{core,experiments,knows}/src` ahead of site-packages) is load-bearing: without it Ray worker subprocesses fall back to pip-installed wheels and fail with `Unknown high-level action subspace: knows`. Don't remove it.

### Output tree routing

Results auto-route to `<obs>/<model>/<split>/` where:
- obs: `final_axt` (accessibility tree only) / `final_ss` (screenshot only) / `final_axt_ss` (both) — derived from the agent's obs flags / script suffix (`*_axt.py`, `*_screenshot.py`, `*_axt_screenshot.py`).
- model: `gpt` / `claude` / `gemini` / `deepseek` — derived from the model name / script prefix (`gpt55_*`, `opus47_*`, `gemini31_*`, `deepseek_v4_*`).
- split: `<num>_<family>` (e.g. `38_sheets`). Note some legacy folders use `<family>_<num>`; the selective-mode completeness check accepts both.

Override with `AGENTLAB_EXP_ROOT`.

## Commands

Python interpreter is the conda env `knows` (Python 3.10): `/opt/miniconda3/envs/knows/bin/python`. `run_one.sh` defaults to it via `$PYBIN`.

### Install (see [instructions.md](instructions.md) for the canonical order)
```bash
conda create -n knows python=3.10
pip install -r requirements.txt
make install                         # editable-installs every browsergym/* sub-package + playwright chromium
(cd AgentLab-Knows && pip install -e .)
# IMPORTANT: AgentLab pulls in its own browsergym wheels; uninstall those and keep the editable
# in-repo browsergym-core / experiments / webarena / knows, or the "knows" action subset won't resolve.
```
Submodules: `git submodule update --init --recursive` (pulls `AgentLab-Knows` and `browsergym/knows`).

### Validate the environment
```bash
./setup.sh                    # .env creds, headless login mint, service account, Drive-link sweep
./setup.sh --headed           # first run on a new machine/IP ("Verify it's you")
python scripts/check_drive_links.py [--split docs_1 | --url <drive-url>]   # link sweep alone
```

### Run benchmarks
```bash
./run_one.sh <script> <benchmark> [n_jobs]     # one combo, fully bootstrapped
./run_one.sh opus47_axt.py knows_docs_1
./run_one.sh gpt55_axt.py  knows_slides_39 2

./run.sh                                        # full sweep; edit the run_bench lines to pick combos
KNOWS_BENCHMARK=knows_sheets_2 python benchmark.py   # benchmark.py hard-codes the AGENT; edit it to swap models
```

### Run / debug an evaluator standalone
```bash
python scripts/run_evaluator.py --split docs_1 --instance 1 --id <google_file_id>
# --no-share if the service account already has access; --debug keeps generated artifacts
```

### Tests
```bash
make test-core                                  # pytest -n auto ./tests/core
pytest tests/core/test_task.py::test_name       # single test
pytest -m "not slow"                            # markers: slow, serial (see pyproject.toml)
```

### Formatting
`black` with line-length 100 (configured in [pyproject.toml](pyproject.toml)); a [.pre-commit-config.yaml](.pre-commit-config.yaml) is present.

## Authentication (the part most likely to bite you)

`auto_login` is the **only** supported auth mode — `BROWSERGYM_AUTH_MODE` set to anything else is a hard error, and the legacy `snapshot` / `persistent_profile` modes were removed because they violated per-worker isolation. Full details in [SETUP_AUTH.md](SETUP_AUTH.md). Key facts:

- Set `GOOGLE_USER_EMAIL` / `GOOGLE_USER_PASSWORD` in `.env` (gitignored). Each Ray worker mints its own `storage_state.json` at launch via [scripts/google_auto_login.py](scripts/google_auto_login.py) into `.bg_storage_state_pool/worker_<pid>.json`. A failed mint fails the task loudly — there is no shared-snapshot fallback by design.
- **Parallelism is pinned to `BROWSERGYM_N_JOBS=1`** because all workers authenticate as the same Google account; Google's `__Secure-1PSIDTS` rotation invalidates every concurrent session but one (observed: 4-of-5 trials dying as `auth_lost_mid_episode`). Only raise it after wiring a multi-account auth pool.
- First run from a new machine/IP may hit a "Verify it's you" challenge: clear it once with `python scripts/google_auto_login.py --headed --output storage_state.json`; trust persists.
- `auth-data/service-account.json` is **unrelated** to browser login — it's the evaluator's Drive/Docs/Sheets/Slides API credential for grading. Don't conflate the two.

## Key environment variables

| Var | Purpose |
| --- | --- |
| `KNOWS_BENCHMARK` | Which split to run (`knows_<family>_<num>`); overrides the script default. |
| `KNOWS_TASKS` | Comma/space list of specific task ids to subset (e.g. `knows.sheets_2_personal_recipe.5`). |
| `KNOWS_RUN_EVALUATORS` | Run inline grading at DONE. Default **on** via `_common.py`; `benchmark.py` leaves it **off** unless set. |
| `KNOWS_EXISTING_DOC_IDS` | JSON or `task=doc_id` pairs to continue against an existing workspace doc instead of creating fresh. |
| `KNOWS_SKIP_LINK_CHECK` | Bypass the pre-task Drive-link public-accessibility check (`task.py` fails the episode when a goal-embedded link isn't shared as "Anyone with the link"). Sweep manually with `scripts/check_drive_links.py`. |
| `AGENTLAB_EXP_ROOT` | Override the results output directory (skips auto-routing). |
| `BROWSERGYM_EXTRA_GOAL_INSTRUCTIONS` | Appended to every goal — sign-in recovery text + the **Apps Script ban** (agents may not use Apps Script / `script.google.com` to complete tasks). Set in `_run_common.sh`. |

## Conventions / gotchas

- The Knows submodule (`browsergym/knows/`) has its **own [CLAUDE.md](browsergym/knows/CLAUDE.md)** covering task/evaluator structure, the LLM-judge message format, and the QA Workspace add-on. Defer to it for anything inside that package. **Critical evaluator rule from it:** each criterion in `checkpoints.md` must map to exactly one checkpoint step in `evaluator.py`.
- Adding a new split is a two-file change: register the task family in `browsergym/knows/.../__init__.py` and add the split name to `KNOWS_SPLITS` in `experiments/.../benchmark/configs.py` (and to the `KNOWS_NEW_SPLITS` list in `run.sh` if it should be in the sweep).
- Submodules are pinned commits — coordinate changes to `browsergym/knows` and `AgentLab-Knows` with their upstream repos rather than editing in place expecting them to persist.
- The root holds many transient artifacts (`tmp_*.json`, `eval_dump/`, `final_axt*/`, `results/`, one-off `run_*.sh`). Treat these as run output, not source.
