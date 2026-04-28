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
# Authentication (snapshot mode)
# -----------------------------------------------------------------------------
# We refresh storage_state.json from the persistent Chromium profile via
# extract_auth_state.py (see refresh_auth() below) BEFORE every run_bench
# call. All five Ray workers in a benchmark then share that snapshot.
#
# This is the only approach that survives multi-hour runs: the persistent
# profile eventually loses its session as Google rotates __Secure-1PSIDTS
# under five concurrent workers, and there's no automated recovery once
# that happens. Re-running extract_auth_state.py between splits forces a
# clean re-auth using the credentials in .env.
#
# To switch to per-worker auto-mint (each worker opens the persistent
# profile at task launch), set BROWSERGYM_AUTH_MODE=auto_login before
# invoking this script. The legacy persistent_profile mode is still
# selectable too.
export BROWSERGYM_AUTH_MODE="${BROWSERGYM_AUTH_MODE:-snapshot}"

PROFILE_DIR="$REPO_ROOT/playwright_chrome_profile"
STORAGE_STATE_FILE="$REPO_ROOT/storage_state.json"

if [[ -z "${GOOGLE_USER_EMAIL:-}" || -z "${GOOGLE_USER_PASSWORD:-}" ]]; then
    echo "ERROR: GOOGLE_USER_EMAIL / GOOGLE_USER_PASSWORD must be set in .env." >&2
    echo "       extract_auth_state.py needs them to recover from a signed-out" >&2
    echo "       persistent profile." >&2
    exit 1
fi

if [[ ! -d "$PROFILE_DIR" ]]; then
    echo "ERROR: persistent Chrome profile not found at $PROFILE_DIR" >&2
    echo "       Bootstrap it once with:" >&2
    echo "         python extract_auth_state.py --headed" >&2
    exit 1
fi

if [[ "$BROWSERGYM_AUTH_MODE" == "persistent_profile" ]]; then
    export BROWSERGYM_PERSISTENT_PROFILE="$PROFILE_DIR"
    export BROWSERGYM_PERSISTENT_CHANNEL="chrome"
    export BROWSERGYM_PERSISTENT_POOL_DIR="$REPO_ROOT/.bg_profile_pool"
    export BROWSERGYM_PERSISTENT_PARALLEL=1
fi

# Refresh storage_state.json by re-extracting from the persistent profile.
# Used as a preflight before every run_bench so the snapshot can never go
# more than one benchmark stale. Returns 0 on success, non-zero on failure
# -- callers check the return code so they can warn but not abort the run.
refresh_auth() {
    echo "[run.sh] Refreshing auth state via extract_auth_state.py..."
    if python "$REPO_ROOT/extract_auth_state.py" \
        --output "$STORAGE_STATE_FILE" \
        --profile-dir "$PROFILE_DIR" \
        --headless \
        --verbose 2>&1 | sed 's/^/  /'; then
        echo "[run.sh] Auth refresh OK ($(date '+%H:%M:%S'))."
        return 0
    fi
    echo "[run.sh] WARNING: auth refresh failed; benchmark will run on the" >&2
    echo "         existing storage_state.json (possibly stale)." >&2
    return 1
}

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

# Alternate (legacy) order, e.g. "slides_17" instead of "17_slides". Some
# pre-existing result folders use this layout, so the completeness check
# below considers both.
split_subdir_alt_for_benchmark() {
    local benchmark="$1"
    if [[ "$benchmark" =~ ^knows_([^_]+)_([0-9]+)$ ]]; then
        echo "${BASH_REMATCH[1]}_${BASH_REMATCH[2]}"
        return 0
    fi
    return 1
}

# Each per-instance trial directory is named "<...>.<idx>_<seed>" (e.g.
# .1_28, .2_14, ...). A split with 5 unique trailing ".<idx>_<seed>"
# suffixes among its immediate subdirectories is treated as a fully-run
# split and skipped on subsequent invocations.
SELECTIVE_REQUIRED_INSTANCES="${SELECTIVE_REQUIRED_INSTANCES:-5}"

count_unique_instances() {
    local dir="$1"
    [[ -d "$dir" ]] || { echo 0; return 0; }
    # Step the pipeline manually so a no-match grep (exit 1) does not
    # trip set -euo pipefail in callers.
    local entries matches
    entries="$(ls -1 "$dir" 2>/dev/null || true)"
    if [[ -z "$entries" ]]; then
        echo 0
        return 0
    fi
    matches="$(printf '%s\n' "$entries" | grep -oE '\.[0-9]+_[0-9]+$' || true)"
    if [[ -z "$matches" ]]; then
        echo 0
        return 0
    fi
    printf '%s\n' "$matches" | sort -u | wc -l | tr -d ' '
}

# split_is_complete <results_root> <model_subdir> <benchmark>
#
# Returns 0 if the (results_root, model, split) combination already has
# SELECTIVE_REQUIRED_INSTANCES unique per-instance trial directories under
# either of the two split-folder naming conventions we have on disk.
# Returns 1 otherwise. Echoes the chosen folder path on stdout when complete.
split_is_complete() {
    local results_root="$1"
    local model_subdir="$2"
    local benchmark="$3"

    local primary alt
    primary="$(split_subdir_for_benchmark "$benchmark")"
    alt="$(split_subdir_alt_for_benchmark "$benchmark" 2>/dev/null || true)"

    local candidate count
    for candidate in "$primary" "$alt"; do
        [[ -n "$candidate" ]] || continue
        local dir="$REPO_ROOT/$results_root/$model_subdir/$candidate"
        count="$(count_unique_instances "$dir")"
        if [[ "$count" -ge "$SELECTIVE_REQUIRED_INSTANCES" ]]; then
            echo "$dir"
            return 0
        fi
    done
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

    local model_subdir
    model_subdir="$(model_subdir_for_script "$script")"
    local split_subdir
    split_subdir="$(split_subdir_for_benchmark "$benchmark")"
    local out_dir="$REPO_ROOT/$results_root/$model_subdir/$split_subdir"

    # Selective mode: skip any (results_root, model, split) combination
    # that already has SELECTIVE_REQUIRED_INSTANCES unique per-instance
    # trial directories. This covers both the canonical "<num>_<family>"
    # layout produced by current runs and the legacy "<family>_<num>"
    # layout some splits were saved under.
    local complete_dir
    if complete_dir="$(split_is_complete "$results_root" "$model_subdir" "$benchmark")"; then
        echo "[run.sh] SKIP  $results_root/$model_subdir/$split_subdir ($script, $benchmark) -- already $SELECTIVE_REQUIRED_INSTANCES instances at $complete_dir"
        return 0
    fi
    echo "[run.sh] RUN   $results_root/$model_subdir/$split_subdir ($script, $benchmark)"

    # Recover from any signed-out / stale state the previous benchmark
    # may have left in the persistent profile. Failure is non-fatal: the
    # benchmark will run on whatever storage_state.json is on disk.
    refresh_auth || true

    mkdir -p "$out_dir"
    AGENTLAB_EXP_ROOT="$out_dir" KNOWS_BENCHMARK="$benchmark" \
        python "$REPO_ROOT/benchmarks/$script"
}

# Print a one-shot "skip plan" so we know up front which combinations
# will be exercised before any benchmark fires.
print_selective_plan() {
    local splits=("$@")
    echo "[run.sh] selective-mode plan (required instances per split = $SELECTIVE_REQUIRED_INSTANCES):"
    printf "  %-14s %-8s %-15s %-6s\n" RESULTS MODEL SPLIT_DIR STATUS
    local results_root split model_subdir split_subdir status detail
    for results_root in final_axt final_axt_ss; do
        for model_subdir in gpt; do
            for split in "${splits[@]}"; do
                split_subdir="$(split_subdir_for_benchmark "$split")"
                if detail="$(split_is_complete "$results_root" "$model_subdir" "$split")"; then
                    status="SKIP ($(count_unique_instances "$detail")/$SELECTIVE_REQUIRED_INSTANCES @ $(basename "$detail"))"
                else
                    local primary_count
                    primary_count="$(count_unique_instances "$REPO_ROOT/$results_root/$model_subdir/$split_subdir")"
                    status="RUN ($primary_count/$SELECTIVE_REQUIRED_INSTANCES)"
                fi
                printf "  %-14s %-8s %-15s %s\n" "$results_root" "$model_subdir" "$split_subdir" "$status"
            done
        done
    done
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
    knows_sheets_38
)

# In selective mode we only target gpt (gpt55_*) on the axt and axt+ss
# observation modes. Claude (opus47_*), gemini, and the screenshot-only
# (final_ss) lane are intentionally commented out for this run -- claude
# is paused for now and gemini's splits are already fully populated.
print_selective_plan "${KNOWS_NEW_SPLITS[@]}"

for split in "${KNOWS_NEW_SPLITS[@]}"; do
    # Accessibility-tree-only runs -> final_axt/<model>/<split>/
    # run_bench final_axt    gemini31_axt.py             "$split"
    run_bench final_axt    gpt55_axt.py                "$split"
    # run_bench final_axt    opus47_axt.py               "$split"

    # Screenshot-only runs -> final_ss/<model>/<split>/
    # run_bench final_ss     gemini31_screenshot.py      "$split"
    # run_bench final_ss     gpt55_screenshot.py         "$split"
    # run_bench final_ss     opus47_screenshot.py        "$split"

    # Accessibility-tree + screenshot runs -> final_axt_ss/<model>/<split>/
    # run_bench final_axt_ss gemini31_axt_screenshot.py  "$split"
    run_bench final_axt_ss gpt55_axt_screenshot.py     "$split"
    # run_bench final_axt_ss opus47_axt_screenshot.py    "$split"
done
