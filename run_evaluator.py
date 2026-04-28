"""Run a KNOWS evaluator directly against a Google Workspace document.

Usage
-----
    python run_evaluator.py --split docs_1 --instance 1 --id <doc_id>
    python run_evaluator.py --split sheets_2 --instance 3 --id <spreadsheet_id>
    python run_evaluator.py --split slides_20 --instance 2 --id <presentation_id>

Available splits
----------------
    docs_1   → docs_1_formal_letter
    docs_5   → docs_5_influential_papers
    sheets_2 → sheets_2_personal_recipe_foodcomposition
    sheets_6 → sheets_6_investmenttracker
    sheets_38 → sheets_38_apartment_finder
    slides_17 → slides_17_removeimagesaddplaceholders
    slides_20 → slides_20_Illustrated_Book_Report

Flags
-----
    --split       Task split short-name (e.g. docs_1, sheets_38, slides_20)
    --instance    Instance number (1–5)
    --id          Google Workspace file ID (the long token from the URL)
    --no-share    Skip the auto-share step (use if the doc is already
                  accessible to the service account)
    --debug       Set DEBUG=true for the evaluator (keeps generated files)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Sys-path / PYTHONPATH wiring — mirrors benchmark.py so every sub-import
# resolves correctly regardless of the user's current working directory.
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent
for _p in [
    _REPO / "browsergym" / "core" / "src",
    _REPO / "browsergym" / "experiments" / "src",
    _REPO / "browsergym" / "knows" / "src",
    _REPO / "AgentLab-Knows" / "src",
    _REPO,
]:
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(_p) for _p in [
        _REPO / "browsergym" / "core" / "src",
        _REPO / "browsergym" / "experiments" / "src",
        _REPO / "browsergym" / "knows" / "src",
        _REPO / "AgentLab-Knows" / "src",
        _REPO,
    ] if _p.is_dir()]
    + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
)

# ---------------------------------------------------------------------------
# Mapping: short split name → task class
# ---------------------------------------------------------------------------
def _build_split_map():
    from browsergym.knows.task import (  # type: ignore
        DocsFormalLetterTask,
        DocsInfluentialPapersTask,
        SheetsApartmentFinderTask,
        SheetsPersonalRecipeTask,
        SheetsStockTrackerTask,
        SlidesIllustratedBookReportTask,
        SlidesRemoveImagesAddPlaceholdersTask,
    )
    return {
        "docs_1":    DocsFormalLetterTask,
        "docs_5":    DocsInfluentialPapersTask,
        "sheets_2":  SheetsPersonalRecipeTask,
        "sheets_6":  SheetsStockTrackerTask,
        "sheets_38": SheetsApartmentFinderTask,
        "slides_17": SlidesRemoveImagesAddPlaceholdersTask,
        "slides_20": SlidesIllustratedBookReportTask,
    }


def _share_doc(doc_id: str) -> None:
    """Best-effort: share *doc_id* with the evaluator service account."""
    try:
        from browsergym.knows.doc_setup import share_doc_with_service_account  # type: ignore
        ok = share_doc_with_service_account(doc_id)
        if ok:
            print(f"[share] Document {doc_id} shared with the service account.")
        else:
            print(
                f"[share] Warning: could not share {doc_id} with the service "
                "account — grading may fail if the doc isn't already accessible."
            )
    except Exception as exc:
        print(f"[share] Warning: share step raised {exc!r}; continuing anyway.")


def run(split: str, instance: int, doc_id: str, *, share: bool = True) -> None:
    split_map = _build_split_map()
    if split not in split_map:
        valid = ", ".join(sorted(split_map))
        print(f"Error: unknown split '{split}'. Valid splits: {valid}", file=sys.stderr)
        sys.exit(1)

    task_cls = split_map[split]
    if instance not in task_cls.AVAILABLE_INSTANCES:
        valid_inst = ", ".join(str(i) for i in sorted(task_cls.AVAILABLE_INSTANCES))
        print(
            f"Error: instance {instance} not available for '{split}'. "
            f"Available: {valid_inst}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Split    : {split}  →  {task_cls.TASK_FAMILY_FOLDER}")
    print(f"Instance : {instance}")
    print(f"Doc ID   : {doc_id}")
    print()

    if share:
        _share_doc(doc_id)
        print()

    task = task_cls(instance_id=instance)
    evaluator = task._load_evaluator()

    grade_fn = evaluator.grade_checkpoints
    accepted = task._accepted_kwargs(grade_fn)
    call_kwargs: dict = {}
    if "workspace_doc_id" in accepted:
        call_kwargs["workspace_doc_id"] = doc_id
    if "browsing_history" in accepted:
        call_kwargs["browsing_history"] = []
    if "browsing_history_list" in accepted:
        call_kwargs["browsing_history_list"] = []
    if "cached_models" in accepted:
        call_kwargs["cached_models"] = None

    result = grade_fn(**call_kwargs)

    # -----------------------------------------------------------------------
    # Print results
    # -----------------------------------------------------------------------
    score = result.final_score
    print()
    print("=" * 50)
    print(f"FINAL SCORE:  {score['result']} / {score['total']}")
    print("=" * 50)

    detailed = result.get_detailed_report()
    for cp in detailed["checkpoints"]:
        cp_result, cp_total = (
            cp["score"].split("/") if "/" in str(cp["score"]) else (None, None)
        )
        print(f"\nCheckpoint '{cp['name']}':  {cp['score']}")
        for step in cp["steps"]:
            status = "PASS" if step["success"] else "FAIL"
            details = step.get("details") or "No details"
            t = f"  {step.get('execution_time', 0):.1f}s" if step.get("execution_time") else ""
            print(f"  [{status}] {step['name']}: {details}{t}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a KNOWS evaluator against a Google Workspace document.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--split", required=True, help="Task split (e.g. docs_1, sheets_38)")
    parser.add_argument("--instance", required=True, type=int, help="Instance number (1–5)")
    parser.add_argument("--id", required=True, dest="doc_id", help="Google Workspace file ID")
    parser.add_argument(
        "--no-share",
        action="store_true",
        help="Skip auto-sharing the document with the service account",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Keep generated files after evaluation (sets DEBUG=true)",
    )
    args = parser.parse_args()

    if args.debug:
        os.environ["DEBUG"] = "true"
        os.environ["CLEANUP"] = "false"

    run(args.split, args.instance, args.doc_id, share=not args.no_share)


if __name__ == "__main__":
    main()
