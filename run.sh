#!/usr/bin/env bash
# Full benchmark sweep across the registered knows splits.
#
# All shared bootstrap (.env, BROWSERGYM_AUTH_MODE, parallelism caps,
# extra-goal instructions) and the per-script subdir routing live in
# scripts/_run_common.sh -- this file is just the configuration layer:
# which scripts and which splits to iterate.
#
# Comment / uncomment the run_bench lines inside the loop below to pick
# which (model, observation-mode, split) combinations to exercise.

set -euo pipefail
cd "$(dirname "$0")"
REPO_ROOT="$(pwd)"
export REPO_ROOT

# shellcheck source=scripts/_run_common.sh
source "$REPO_ROOT/scripts/_run_common.sh"
run_common::bootstrap_env

# Each split has 5 instances and is wired through DEFAULT_BENCHMARKS in
# browsergym.experiments. The 3rd run_bench arg sets KNOWS_BENCHMARK so
# the per-model script reuses its existing agent config against a
# different task family (sheets / docs / slides). All three observation
# modes (axt, screenshot, axt+screenshot) are available for each model.
KNOWS_NEW_SPLITS=(
    knows_docs_1
    knows_sheets_2
#add more splits here
)

for split in "${KNOWS_NEW_SPLITS[@]}"; do
    # Add model runs here
    # Accessibility-tree-only runs -> final_axt/<model>/<split>/
    run_common::run_bench final_axt    gemini31_axt.py             "$split"
    run_common::run_bench final_axt    gpt55_axt.py                "$split"
    run_common::run_bench   final_axt    opus47_axt.py               "$split"
    run_common::run_bench final_axt    deepseek_v4_axt.py          "$split"

    # Screenshot-only runs -> final_ss/<model>/<split>/
    run_common::run_bench final_ss     gemini31_screenshot.py      "$split"
    run_common::run_bench final_ss     gpt55_screenshot.py         "$split"
    run_common::run_bench final_ss     opus47_screenshot.py        "$split"


    # Accessibility-tree + screenshot runs -> final_axt_ss/<model>/<split>/
    run_common::run_bench final_axt_ss gemini31_axt_screenshot.py     "$split"
    run_common::run_bench final_axt_ss gpt55_axt_screenshot.py        "$split"
    run_common::run_bench final_axt_ss opus47_axt_screenshot.py       "$split"
done
