#!/usr/bin/env bash
# scripts/_run_common.sh
#
# Shared bash helpers for the top-level benchmark runner shells:
#   - run.sh                full sweep (every uncommented model x obs x split)
#   - run_selective.sh      same sweep, but skips already-complete splits
#   - run_one.sh            one-shot single (script, benchmark) launcher
#
# This file is meant to be `source`d, not executed directly. It does not
# `set -euo pipefail` itself (that's the caller's call) and it does not
# `cd`; the caller picks the working directory.
#
# Public functions exposed (all under the run_common:: namespace):
#   run_common::bootstrap_env             # source .env, set BROWSERGYM_* + extra-goal env
#   run_common::model_subdir_for_script   # gemini/gpt/claude/deepseek
#   run_common::obs_subdir_for_script     # final_axt / final_ss / final_axt_ss
#   run_common::split_subdir_for_benchmark         # "<num>_<family>" canonical
#   run_common::split_subdir_alt_for_benchmark     # "<family>_<num>" legacy
#   run_common::run_bench <results_root> <script> <benchmark>
#   run_common::run_bench_skip_if_complete <results_root> <script> <benchmark>
#   run_common::print_selective_plan <model_subdir> <results_root...> -- <split...>
#
# Required globals (set by the caller before sourcing or before calling
# the run_bench functions):
#   REPO_ROOT      absolute path to the repo root
#
# Optional env knobs honored by run_bench:
#   BROWSERGYM_SKIP_GEMINI=1            skip gemini31_* scripts
#   SELECTIVE_REQUIRED_INSTANCES=N      override the "split is complete"
#                                       threshold (default: 5)

# -----------------------------------------------------------------------------
# Authentication + parallelism + extra-goal env setup
# -----------------------------------------------------------------------------
run_common::bootstrap_env() {
    if [[ -z "${REPO_ROOT:-}" ]]; then
        echo "ERROR: run_common::bootstrap_env: REPO_ROOT must be set." >&2
        return 1
    fi

    # Load API keys + Google credentials from .env into the current shell.
    if [[ ! -f "$REPO_ROOT/.env" ]]; then
        echo "ERROR: $REPO_ROOT/.env not found." >&2
        return 1
    fi
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +a

    # Prefer the in-repo AgentLab checkout so the benchmark entrypoints work
    # even when the current shell has not activated an editable install.
    export PYTHONPATH="$REPO_ROOT/AgentLab-Knows/src${PYTHONPATH:+:$PYTHONPATH}"

    # auto_login is the only supported auth mode (per-worker storage_state).
    export BROWSERGYM_AUTH_MODE="${BROWSERGYM_AUTH_MODE:-auto_login}"
    if [[ "$BROWSERGYM_AUTH_MODE" != "auto_login" ]]; then
        echo "ERROR: only BROWSERGYM_AUTH_MODE=auto_login is supported (got '$BROWSERGYM_AUTH_MODE')." >&2
        echo "       Per-worker storage_state is mandatory; snapshot/persistent_profile modes have been removed." >&2
        return 1
    fi

    if [[ -z "${GOOGLE_USER_EMAIL:-}" || -z "${GOOGLE_USER_PASSWORD:-}" ]]; then
        echo "ERROR: GOOGLE_USER_EMAIL / GOOGLE_USER_PASSWORD must be set in .env." >&2
        echo "       The per-worker auto-login flow needs them to mint storage_state." >&2
        return 1
    fi

    # Default to 2 workers to reduce same-account Google session churn.
    export BROWSERGYM_N_JOBS="${BROWSERGYM_N_JOBS:-2}"

    # Extra goal instructions appended to every task's goal text by
    # KnowsBenchTask.setup(). Restating the credentials here lets the agent
    # itself recover from a sign-out page when our refresh_auth() preflight
    # wasn't enough. The Apps Script ban keeps every benchmarked model
    # inside the normal Docs/Sheets/Slides UI instead of automating edits.
    export BROWSERGYM_EXTRA_GOAL_INSTRUCTIONS="If at any point you find yourself signed out of Google (e.g. redirected to https://accounts.google.com/ServiceLogin, https://accounts.google.com/AccountChooser, or shown a 'Sign in' / 'Choose an account' / 'Verify it is you' page), sign back in as ${GOOGLE_USER_EMAIL} using password ${GOOGLE_USER_PASSWORD}. If a 'Verify it is you' interstitial appears with just a Next button, click Next and continue. Then resume the task on the document URL provided above. You are strictly prohibited from using Google Apps Script, script.google.com, or any Apps Script editor/API to complete this task."
}

# -----------------------------------------------------------------------------
# Subdir routing helpers (script -> model, script -> obs, benchmark -> split)
# -----------------------------------------------------------------------------
run_common::model_subdir_for_script() {
    case "$1" in
        gemini31_*)     echo "gemini" ;;
        gpt55_*)        echo "gpt" ;;
        opus47_*)       echo "claude" ;;
        deepseek_v4_*)  echo "deepseek" ;;
        *)
            echo "ERROR: cannot infer final results model folder for script: $1" >&2
            return 1
            ;;
    esac
}

run_common::obs_subdir_for_script() {
    case "$1" in
        *_axt_screenshot.py) echo "final_axt_ss" ;;
        *_axt.py)            echo "final_axt" ;;
        *_screenshot.py)     echo "final_ss" ;;
        *)
            echo "ERROR: cannot infer obs folder for script: $1" >&2
            return 1
            ;;
    esac
}

run_common::split_subdir_for_benchmark() {
    if [[ "$1" =~ ^knows_([^_]+)_([0-9]+)$ ]]; then
        echo "${BASH_REMATCH[2]}_${BASH_REMATCH[1]}"
        return 0
    fi
    echo "ERROR: cannot infer final results split folder for benchmark: $1" >&2
    return 1
}

# Alternate (legacy) order, e.g. "slides_17" instead of "17_slides". Some
# pre-existing result folders use this layout, so the completeness check
# below considers both.
run_common::split_subdir_alt_for_benchmark() {
    if [[ "$1" =~ ^knows_([^_]+)_([0-9]+)$ ]]; then
        echo "${BASH_REMATCH[1]}_${BASH_REMATCH[2]}"
        return 0
    fi
    return 1
}

# -----------------------------------------------------------------------------
# Selective-mode helpers ("skip if already complete")
# -----------------------------------------------------------------------------
# Each per-instance trial directory is named "<...>.<idx>_<seed>" (e.g.
# .1_28, .2_14, ...). A split with SELECTIVE_REQUIRED_INSTANCES unique
# trailing ".<idx>_<seed>" suffixes is treated as fully-run.
: "${SELECTIVE_REQUIRED_INSTANCES:=5}"
export SELECTIVE_REQUIRED_INSTANCES

run_common::count_unique_instances() {
    local dir="$1"
    [[ -d "$dir" ]] || { echo 0; return 0; }
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
# echoes the matching folder path on stdout and returns 0 when complete.
run_common::split_is_complete() {
    local results_root="$1"
    local model_subdir="$2"
    local benchmark="$3"

    local primary alt
    primary="$(run_common::split_subdir_for_benchmark "$benchmark")"
    alt="$(run_common::split_subdir_alt_for_benchmark "$benchmark" 2>/dev/null || true)"

    local candidate count
    for candidate in "$primary" "$alt"; do
        [[ -n "$candidate" ]] || continue
        local dir="$REPO_ROOT/$results_root/$model_subdir/$candidate"
        count="$(run_common::count_unique_instances "$dir")"
        if [[ "$count" -ge "$SELECTIVE_REQUIRED_INSTANCES" ]]; then
            echo "$dir"
            return 0
        fi
    done
    return 1
}

# -----------------------------------------------------------------------------
# Core launcher: invoke a per-model benchmark script with the right env
# -----------------------------------------------------------------------------
# run_bench <results_root> <script> [benchmark]
#
# Sets AGENTLAB_EXP_ROOT to <repo>/<results_root>/<model>/<split>/ so the
# study lands in the canonical tree, then exec-substitutes the python
# script under benchmarks/.
run_common::run_bench() {
    local results_root="$1"
    local script="$2"
    local benchmark="${3:-knows_docs_1}"

    if [[ "${BROWSERGYM_SKIP_GEMINI:-0}" == "1" && "$script" == gemini31_* ]]; then
        echo "[run_common] skipping $script for benchmark=$benchmark (BROWSERGYM_SKIP_GEMINI=1)"
        return 0
    fi

    local model_subdir split_subdir out_dir
    model_subdir="$(run_common::model_subdir_for_script "$script")"
    split_subdir="$(run_common::split_subdir_for_benchmark "$benchmark")"
    out_dir="$REPO_ROOT/$results_root/$model_subdir/$split_subdir"
    mkdir -p "$out_dir"

    echo "[run_common] RUN   $results_root/$model_subdir/$split_subdir ($script, $benchmark)"
    AGENTLAB_EXP_ROOT="$out_dir" KNOWS_BENCHMARK="$benchmark" \
        python "$REPO_ROOT/benchmarks/$script"
}

# run_bench_skip_if_complete <results_root> <script> [benchmark]
#
# Same as run_bench, but short-circuits when the (results_root, model,
# split) triple already has SELECTIVE_REQUIRED_INSTANCES unique instance
# folders -- under either the canonical "<num>_<family>" layout or the
# legacy "<family>_<num>" layout some splits were saved under.
run_common::run_bench_skip_if_complete() {
    local results_root="$1"
    local script="$2"
    local benchmark="${3:-knows_docs_1}"

    if [[ "${BROWSERGYM_SKIP_GEMINI:-0}" == "1" && "$script" == gemini31_* ]]; then
        echo "[run_common] skipping $script for benchmark=$benchmark (BROWSERGYM_SKIP_GEMINI=1)"
        return 0
    fi

    local model_subdir split_subdir
    model_subdir="$(run_common::model_subdir_for_script "$script")"
    split_subdir="$(run_common::split_subdir_for_benchmark "$benchmark")"

    local complete_dir
    if complete_dir="$(run_common::split_is_complete "$results_root" "$model_subdir" "$benchmark")"; then
        echo "[run_common] SKIP  $results_root/$model_subdir/$split_subdir ($script, $benchmark) -- already $SELECTIVE_REQUIRED_INSTANCES instances at $complete_dir"
        return 0
    fi

    run_common::run_bench "$results_root" "$script" "$benchmark"
}

# -----------------------------------------------------------------------------
# Selective-mode plan printer (used by run_selective.sh)
# -----------------------------------------------------------------------------
# Usage: run_common::print_selective_plan <model_subdir> <results_root_csv> -- <split...>
#
# Example: run_common::print_selective_plan gpt "final_axt final_axt_ss" -- "${KNOWS_NEW_SPLITS[@]}"
run_common::print_selective_plan() {
    local model_subdir="$1"
    local results_roots="$2"
    shift 2
    if [[ "$1" == "--" ]]; then
        shift
    fi
    local splits=("$@")

    echo "[run_common] selective-mode plan (required instances per split = $SELECTIVE_REQUIRED_INSTANCES):"
    printf "  %-14s %-8s %-15s %-6s\n" RESULTS MODEL SPLIT_DIR STATUS
    local results_root split split_subdir status detail primary_count
    for results_root in $results_roots; do
        for split in "${splits[@]}"; do
            split_subdir="$(run_common::split_subdir_for_benchmark "$split")"
            if detail="$(run_common::split_is_complete "$results_root" "$model_subdir" "$split")"; then
                status="SKIP ($(run_common::count_unique_instances "$detail")/$SELECTIVE_REQUIRED_INSTANCES @ $(basename "$detail"))"
            else
                primary_count="$(run_common::count_unique_instances "$REPO_ROOT/$results_root/$model_subdir/$split_subdir")"
                status="RUN ($primary_count/$SELECTIVE_REQUIRED_INSTANCES)"
            fi
            printf "  %-14s %-8s %-15s %s\n" "$results_root" "$model_subdir" "$split_subdir" "$status"
        done
    done
}
