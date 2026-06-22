#!/usr/bin/env bash
# One-shot convenience runner for any single (script, benchmark) combo.
#
# Bundles every preprocessing step previously copy-pasted on the CLI:
#   - sources `.env` so GOOGLE_USER_EMAIL / GOOGLE_USER_PASSWORD are loaded
#   - validates credentials are present
#   - exports PYTHONPATH for the in-repo AgentLab checkout
#   - pins BROWSERGYM_AUTH_MODE=auto_login + per-worker storage_state
#   - sets BROWSERGYM_EXTRA_GOAL_INSTRUCTIONS (sign-in recovery + Apps Script ban)
#   - mkdir -p the matching `final_<obs>/<model>/<split>/` output tree
#   - launches the benchmark script with the chosen N parallel workers
#
# Usage:
#   ./run_one.sh <script> <benchmark> [n_jobs]
#
# Examples:
#   ./run_one.sh deepseek_v4_axt_screenshot.py knows_sheets_2 5
#   ./run_one.sh gpt55_axt.py                 knows_slides_39 2
#   ./run_one.sh opus47_axt_screenshot.py     knows_docs_1
#
# Optional env overrides:
#   AGENTLAB_EXP_ROOT  - skip auto-routing and write results here instead
#   KNOWS_TASKS        - comma/space list of task ids to subset
#   BROWSERGYM_N_JOBS  - parallel workers (positional arg wins if both set)

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "usage: $0 <script> <benchmark> [n_jobs]" >&2
    echo "  e.g. $0 deepseek_v4_axt_screenshot.py knows_sheets_2 5" >&2
    exit 2
fi

SCRIPT="$1"
BENCHMARK="$2"
# Default to 1 worker because all per-PID storage_state mints derive from
# the same Google account, and Google's server-side __Secure-1PSIDTS
# rotation invalidates every concurrent session except one whenever it
# fires (see benchmarks/_common.py and scripts/extract_auth_state.py).
# Bumping this above 1 only makes sense after a multi-account auth pool
# is wired up. Caller can still opt in explicitly via the 3rd positional
# arg or BROWSERGYM_N_JOBS.
N_JOBS="${3:-${BROWSERGYM_N_JOBS:-1}}"

# This script lives in scripts/; the repo root is its parent directory.
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
export REPO_ROOT

# shellcheck source=scripts/_run_common.sh
source "$REPO_ROOT/scripts/_run_common.sh"
run_common::bootstrap_env

# Override the parallelism floor that bootstrap_env defaulted to 2.
export BROWSERGYM_N_JOBS="$N_JOBS"
export BROWSERGYM_REQUIRE_PER_WORKER_STORAGE=1

# -----------------------------------------------------------------------------
# Auto-route into final_<obs>/<model>/<split>/ unless caller pinned a path.
# -----------------------------------------------------------------------------
if [[ -z "${AGENTLAB_EXP_ROOT:-}" ]]; then
    obs_dir="$(run_common::obs_subdir_for_script "$SCRIPT")"
    model_dir="$(run_common::model_subdir_for_script "$SCRIPT")"
    split_dir="$(run_common::split_subdir_for_benchmark "$BENCHMARK")"
    AGENTLAB_EXP_ROOT="$REPO_ROOT/$obs_dir/$model_dir/$split_dir"
fi
export AGENTLAB_EXP_ROOT
mkdir -p "$AGENTLAB_EXP_ROOT"

# -----------------------------------------------------------------------------
# Launch.
# -----------------------------------------------------------------------------
export KNOWS_BENCHMARK="$BENCHMARK"
PYBIN="${PYBIN:-/opt/miniconda3/envs/knows/bin/python}"
if [[ ! -x "$PYBIN" ]]; then
    PYBIN="$(command -v python)"
fi

echo "[run_one] script=$SCRIPT benchmark=$BENCHMARK n_jobs=$N_JOBS"
echo "[run_one] AGENTLAB_EXP_ROOT=$AGENTLAB_EXP_ROOT"
echo "[run_one] python=$PYBIN"
exec "$PYBIN" "$REPO_ROOT/benchmarks/$SCRIPT"
