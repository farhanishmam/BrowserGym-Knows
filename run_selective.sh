#!/usr/bin/env bash
# Selective benchmark sweep: same matrix as run.sh, but each (results_root,
# model, split) triple is skipped if it already has
# SELECTIVE_REQUIRED_INSTANCES (default: 5) unique per-instance trial dirs.
#
# Shared bootstrap and routing helpers live in scripts/_run_common.sh.
# Override the completeness threshold via:
#     SELECTIVE_REQUIRED_INSTANCES=N ./run_selective.sh

set -euo pipefail
cd "$(dirname "$0")"
REPO_ROOT="$(pwd)"
export REPO_ROOT

# shellcheck source=scripts/_run_common.sh
source "$REPO_ROOT/scripts/_run_common.sh"
run_common::bootstrap_env

KNOWS_NEW_SPLITS=(
    knows_docs_1
    knows_sheets_2
    knows_docs_5
    knows_sheets_6
    knows_sheets_10
    knows_docs_11
    knows_slides_17
    knows_slides_20
    knows_sheets_25
    knows_slides_26
    knows_slides_29
    knows_slides_30
    knows_docs_31
    knows_docs_37
    knows_sheets_38
    knows_slides_39
    knows_sheets_45
    knows_slides_51
    knows_sheets_55
)

# In selective mode we only target gpt (gpt55_*) on the axt and axt+ss
# observation modes. Claude (opus47_*), gemini, and the screenshot-only
# (final_ss) lane are intentionally commented out below for this run.
run_common::print_selective_plan gpt "final_axt final_axt_ss" -- "${KNOWS_NEW_SPLITS[@]}"

for split in "${KNOWS_NEW_SPLITS[@]}"; do
    # Accessibility-tree-only runs -> final_axt/<model>/<split>/
    # run_common::run_bench_skip_if_complete final_axt    gemini31_axt.py     "$split"
    run_common::run_bench_skip_if_complete   final_axt    gpt55_axt.py        "$split"
    # run_common::run_bench_skip_if_complete final_axt    opus47_axt.py       "$split"

    # Screenshot-only runs -> final_ss/<model>/<split>/
    # run_common::run_bench_skip_if_complete final_ss     gemini31_screenshot.py "$split"
    # run_common::run_bench_skip_if_complete final_ss     gpt55_screenshot.py    "$split"
    # run_common::run_bench_skip_if_complete final_ss     opus47_screenshot.py   "$split"

    # Accessibility-tree + screenshot runs -> final_axt_ss/<model>/<split>/
    # run_common::run_bench_skip_if_complete final_axt_ss gemini31_axt_screenshot.py "$split"
    run_common::run_bench_skip_if_complete   final_axt_ss gpt55_axt_screenshot.py    "$split"
    # run_common::run_bench_skip_if_complete final_axt_ss opus47_axt_screenshot.py   "$split"
done
