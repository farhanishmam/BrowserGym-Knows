#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Load API keys from .env into the current shell so the python scripts see them.
set -a
source .env
set +a

REPO_ROOT="$(pwd)"

# Prefer the in-repo AgentLab checkout so the benchmark entrypoints work even
# when the current shell has not activated an editable install.
export PYTHONPATH="$REPO_ROOT/AgentLab-Knows/src${PYTHONPATH:+:$PYTHONPATH}"

# -----------------------------------------------------------------------------
# Authentication (auto_login is the ONLY supported mode)
# -----------------------------------------------------------------------------
# Each Ray worker mints its own freshly-validated storage_state.json at
# launch via scripts/google_auto_login.py, keyed on its PID. No two
# workers ever share a storage_state file -- the per-worker mint pool at
# .bg_storage_state_pool/worker_<pid>.json is the sole source of auth.
#
# The legacy "snapshot" (every worker reads the same storage_state.json)
# and "persistent_profile" (every worker shares an on-disk Chromium
# profile) modes have been removed because both violated per-worker
# isolation under parallel execution.
export BROWSERGYM_AUTH_MODE="${BROWSERGYM_AUTH_MODE:-auto_login}"

if [[ "$BROWSERGYM_AUTH_MODE" != "auto_login" ]]; then
    echo "ERROR: only BROWSERGYM_AUTH_MODE=auto_login is supported (got '$BROWSERGYM_AUTH_MODE')." >&2
    echo "       Per-worker storage_state is mandatory; snapshot/persistent_profile modes have been removed." >&2
    exit 1
fi

if [[ -z "${GOOGLE_USER_EMAIL:-}" || -z "${GOOGLE_USER_PASSWORD:-}" ]]; then
    echo "ERROR: GOOGLE_USER_EMAIL / GOOGLE_USER_PASSWORD must be set in .env." >&2
    echo "       The per-worker auto-login flow needs them to mint storage_state." >&2
    exit 1
fi

# -----------------------------------------------------------------------------
# Parallelism (5 task instances per benchmark script)
# -----------------------------------------------------------------------------
# Auto-login mode mints a fresh per-PID storage_state for each worker, so any
# parallelism level is safe. Persistent-profile mode also supports parallelism
# via per-PID profile clones. _common.py picks up BROWSERGYM_N_JOBS to size
# the Ray worker pool (see run_knows_benchmark).
#
# Default to 5 workers to maximize throughput across benchmark scripts.
# Override via `BROWSERGYM_N_JOBS=N ./run.sh` for one-off experiments.
export BROWSERGYM_N_JOBS="${BROWSERGYM_N_JOBS:-5}"

# -----------------------------------------------------------------------------
# Extra goal instructions
# -----------------------------------------------------------------------------
# Appended to every task's goal text by KnowsBenchTask.setup() when set. Use
# this to inject run-time guidance (sign-in fallbacks, etc.) without editing
# each per-instance task.md. The agent sees this text after the task
# description and any benchmark-injected hints.
#
# The credentials are restated here so the agent itself can recover from a
# sign-in page when our refresh_auth() preflight wasn't enough (e.g. the
# session got bumped mid-task and the snapshot loaded into the worker's
# Chromium has expired). The harness preflight is the primary defense; this
# is the in-task safety net. The Apps Script ban keeps every benchmarked
# model inside the normal Docs/Sheets/Slides UI instead of automating edits.
export BROWSERGYM_EXTRA_GOAL_INSTRUCTIONS="If at any point you find yourself signed out of Google (e.g. redirected to https://accounts.google.com/ServiceLogin, https://accounts.google.com/AccountChooser, or shown a 'Sign in' / 'Choose an account' / 'Verify it's you' page), sign back in as agentbenchmark@gmail.com using password Universityofutah. If a 'Verify it's you' interstitial appears with just a Next button, click Next and continue. Then resume the task on the document URL provided above. You are strictly prohibited from using Google Apps Script, script.google.com, or any Apps Script editor/API to complete this task."

# Run a single benchmark script with AGENTLAB_EXP_ROOT pointing at the
# requested final results tree under the repo root. _common.py honors this
# env var when run_knows_benchmark() is called without an explicit results_dir.
#
# An optional 3rd arg selects which knows split to run; it's exposed to the
# python script via the KNOWS_BENCHMARK env var that run_knows_benchmark()
# reads. When omitted, the script falls back to its historical default
# (knows_docs_1), so existing call sites keep working unchanged.
model_subdir_for_script() {
    local script="$1"
    case "$script" in
        gemini31_*) echo "gemini" ;;
        gpt55_*) echo "gpt" ;;
        opus47_*) echo "claude" ;;
        *)
            echo "ERROR: cannot infer final results model folder for script: $script" >&2
            return 1
            ;;
    esac
}

split_subdir_for_benchmark() {
    local benchmark="$1"
    if [[ "$benchmark" =~ ^knows_([^_]+)_([0-9]+)$ ]]; then
        echo "${BASH_REMATCH[2]}_${BASH_REMATCH[1]}"
        return 0
    fi

    echo "ERROR: cannot infer final results split folder for benchmark: $benchmark" >&2
    return 1
}

run_bench() {
    local results_root="$1"
    local script="$2"
    local benchmark="${3:-knows_docs_1}"
    if [[ "${BROWSERGYM_SKIP_GEMINI:-0}" == "1" && "$script" == gemini31_* ]]; then
        echo "[run.sh] skipping $script for benchmark=$benchmark (BROWSERGYM_SKIP_GEMINI=1)"
        return 0
    fi

    # No global auth preflight: each Ray worker mints its own
    # storage_state at launch via scripts/google_auto_login.py.

    local model_subdir
    model_subdir="$(model_subdir_for_script "$script")"
    local split_subdir
    split_subdir="$(split_subdir_for_benchmark "$benchmark")"
    local out_dir="$REPO_ROOT/$results_root/$model_subdir/$split_subdir"
    mkdir -p "$out_dir"
    AGENTLAB_EXP_ROOT="$out_dir" KNOWS_BENCHMARK="$benchmark" \
        python "$REPO_ROOT/benchmarks/$script"
}

# -----------------------------------------------------------------------------
# Default split: knows_docs_1
# -----------------------------------------------------------------------------
# These call run_bench without a 3rd arg, so KNOWS_BENCHMARK falls through to
# its default value of "knows_docs_1" inside _common.run_knows_benchmark.

# Accessibility-tree-only runs -> final_axt/<model>/<split>/
# run_bench final_axt gemini31_axt.py
# # run_bench final_axt gpt55_axt.py
# run_bench final_axt opus47_axt.py

# # Screenshot-only runs -> final_ss/<model>/<split>/
# run_bench final_ss gemini31_screenshot.py
# run_bench final_ss gpt55_screenshot.py
# run_bench final_ss opus47_screenshot.py

# # Accessibility-tree + screenshot runs -> final_axt_ss/<model>/<split>/
# # run_bench final_axt_ss gemini31_axt_screenshot.py
# run_bench final_axt_ss gpt55_axt_screenshot.py
# run_bench final_axt_ss opus47_axt_screenshot.py

# -----------------------------------------------------------------------------
# Newly-registered knows splits (gemini 3.1, gpt-5.5, opus-4.7)
# -----------------------------------------------------------------------------
# Each split has 5 instances and is wired through DEFAULT_BENCHMARKS in
# browsergym.experiments. The 3rd run_bench arg sets KNOWS_BENCHMARK so the
# per-model script reuses its existing agent config against a different
# task family (sheets / docs / slides). All three observation modes
# (axt, screenshot, axt+screenshot) are run for each model.
KNOWS_NEW_SPLITS=(
    knows_docs_1
    knows_sheets_2
    knows_docs_5
    knows_sheets_6
    knows_slides_17
    knows_slides_20
    knows_sheets_25
    knows_sheets_38
    knows_slides_39
)

for split in "${KNOWS_NEW_SPLITS[@]}"; do
    # Accessibility-tree-only runs -> final_axt/<model>/<split>/
    # run_bench final_axt    gemini31_axt.py             "$split"
    # run_bench final_axt    gpt55_axt.py                "$split"
    run_bench final_axt    opus47_axt.py               "$split"

    # Screenshot-only runs -> final_ss/<model>/<split>/
    # run_bench final_ss     gemini31_screenshot.py      "$split"
    # run_bench final_ss     gpt55_screenshot.py         "$split"
    # run_bench final_ss     opus47_screenshot.py        "$split"

    # Accessibility-tree + screenshot runs -> final_axt_ss/<model>/<split>/
    # run_bench final_axt_ss gemini31_axt_screenshot.py  "$split"
    # run_bench final_axt_ss gpt55_axt_screenshot.py      "$split"
    # run_bench final_axt_ss opus47_axt_screenshot.py     "$split"
done
