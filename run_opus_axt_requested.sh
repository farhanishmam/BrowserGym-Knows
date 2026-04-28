#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Dedicated runner for Opus AXT on the requested Knows splits only:
# docs_5, sheets_6, slides_20, and sheets_38.

if [[ -f "/opt/miniconda3/etc/profile.d/conda.sh" ]]; then
    source "/opt/miniconda3/etc/profile.d/conda.sh"
    conda activate knows
fi

set -a
source .env
set +a

REPO_ROOT="$(pwd)"

export PYTHONPATH="$REPO_ROOT/AgentLab-Knows/src${PYTHONPATH:+:$PYTHONPATH}"
export BROWSERGYM_AUTH_MODE="${BROWSERGYM_AUTH_MODE:-snapshot}"
export BROWSERGYM_N_JOBS="${BROWSERGYM_N_JOBS:-5}"

PROFILE_DIR="$REPO_ROOT/playwright_chrome_profile"
STORAGE_STATE_FILE="$REPO_ROOT/storage_state.json"

if [[ -z "${GOOGLE_USER_EMAIL:-}" || -z "${GOOGLE_USER_PASSWORD:-}" ]]; then
    echo "ERROR: GOOGLE_USER_EMAIL / GOOGLE_USER_PASSWORD must be set in .env." >&2
    exit 1
fi

if [[ ! -d "$PROFILE_DIR" ]]; then
    echo "ERROR: persistent Chrome profile not found at $PROFILE_DIR" >&2
    echo "       Bootstrap it once with: python extract_auth_state.py --headed" >&2
    exit 1
fi

if [[ "$BROWSERGYM_AUTH_MODE" == "persistent_profile" ]]; then
    export BROWSERGYM_PERSISTENT_PROFILE="$PROFILE_DIR"
    export BROWSERGYM_PERSISTENT_CHANNEL="chrome"
    export BROWSERGYM_PERSISTENT_POOL_DIR="$REPO_ROOT/.bg_profile_pool"
    export BROWSERGYM_PERSISTENT_PARALLEL=1
fi

export BROWSERGYM_EXTRA_GOAL_INSTRUCTIONS="If at any point you find yourself signed out of Google (e.g. redirected to https://accounts.google.com/ServiceLogin, https://accounts.google.com/AccountChooser, or shown a 'Sign in' / 'Choose an account' / 'Verify it's you' page), sign back in as ${GOOGLE_USER_EMAIL} using password ${GOOGLE_USER_PASSWORD}. If a 'Verify it's you' interstitial appears with just a Next button, click Next and continue. Then resume the task on the document URL provided above. You are strictly prohibited from using Google Apps Script, script.google.com, or any Apps Script editor/API to complete this task."

refresh_auth() {
    echo "[run_opus_axt_requested] Refreshing auth state via extract_auth_state.py..."
    if python "$REPO_ROOT/extract_auth_state.py" \
        --output "$STORAGE_STATE_FILE" \
        --profile-dir "$PROFILE_DIR" \
        --headless \
        --verbose 2>&1 | sed 's/^/  /'; then
        echo "[run_opus_axt_requested] Auth refresh OK ($(date '+%H:%M:%S'))."
        return 0
    fi
    echo "[run_opus_axt_requested] WARNING: auth refresh failed; benchmark will run on existing storage_state.json." >&2
    return 1
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

run_opus_axt_split() {
    local benchmark="$1"
    local split_subdir
    split_subdir="$(split_subdir_for_benchmark "$benchmark")"
    local out_dir="$REPO_ROOT/final_axt/claude/$split_subdir"

    mkdir -p "$out_dir"
    refresh_auth || true

    echo "[run_opus_axt_requested] Starting Opus AXT on $benchmark -> $out_dir"
    AGENTLAB_EXP_ROOT="$out_dir" KNOWS_BENCHMARK="$benchmark" \
        python "$REPO_ROOT/benchmarks/opus47_axt.py"
}

REQUESTED_SPLITS=(
    knows_docs_5
    knows_sheets_6
    knows_slides_20
    knows_sheets_38
)

for split in "${REQUESTED_SPLITS[@]}"; do
    run_opus_axt_split "$split"
done
