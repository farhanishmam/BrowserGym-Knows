"""Run a KNOWS evaluator directly against a Google Workspace document.

Usage
-----
    python scripts/run_evaluator.py --split docs_1 --instance 1 --id <doc_id>
    python scripts/run_evaluator.py --split sheets_2 --instance 3 --id <spreadsheet_id>
    python scripts/run_evaluator.py --split slides_20 --instance 2 --id <presentation_id>

Available splits
----------------
    docs_1    → docs_1_formal_letter
    docs_5    → docs_5_influential_papers
    docs_11   → docs_11_personal_recipe_ocr
    docs_31   → docs_31_education_lesson_plan
    docs_37   → docs_37_reference_list
    sheets_2  → sheets_2_personal_recipe_foodcomposition
    sheets_6  → sheets_6_investmenttracker
    sheets_10 → sheets_10_paper_sorting
    sheets_7  → sheets_7_running_analysis
    sheets_25 → sheets_25_skitourplan
    sheets_28 → sheets_28_personal_travel_planner
    sheets_38 → sheets_38_apartment_finder
    sheets_45 → sheets_45_Personal_WeddingPlanner_weddingcolorpallette
    sheets_55 → sheets_55_Movie_Recommendation
    slides_17 → slides_17_removeimagesaddplaceholders
    slides_20 → slides_20_Illustrated_Book_Report
    slides_26 → slides_26_basic_educational_slide_deck
    slides_29 → slides_29_buy_car_pres
    slides_30 → slides_30_Work_Wikipedia_Photos
    slides_39 → slides_39_Personal_Lookbook_PaintColors
    slides_51 → slides_51_event_announcement_poster

Flags
-----
    --split       Task split short-name (e.g. docs_1, sheets_38, slides_20)
    --instance    Instance number (1-5)
    --id          Google Workspace file ID (the long token from the URL)
    --no-share    Skip the auto-share step entirely (use if the doc is
                  already accessible to the service account).
    --debug       Set DEBUG=true for the evaluator (keeps generated files)

Sharing
-------
The auto-share step is two-tiered, mirroring `create_task_workspace`:

1. Drive API call (`share_doc_with_service_account`). Idempotent: a
   doc the service account already has access to short-circuits here.
2. Playwright UI fallback (`share_workspace_via_ui_standalone`). Always
   runs when the API call returns False, using `storage_state.json` to
   open the file owner's session, drive the Share dialog, add the
   service account email, and click Send. If `storage_state.json` is
   missing or stale and the headless re-mint also fails, the script
   auto-prompts (Y/n) and launches `scripts/google_auto_login.py
   --headed` so the operator can satisfy any first-run device-trust /
   2-Step Verification challenge in a real browser window.

The kind (docs / sheets / slides) is inferred from `--split` so the
fallback hits the right URL pattern automatically. Knobs:

- `--no-share`              : skip auto-sharing entirely.
- `--no-ui-share-fallback`  : try the Drive API tier only.
- `--no-auto-prompt`        : skip the headed-login Y/n prompt; just
                              print a hint and continue.
- `KNOWS_DISABLE_UI_SHARE_FALLBACK=1` : disable tier 2.
- `KNOWS_DISABLE_AUTO_PROMPT_LOGIN=1` : disable the headed-login prompt.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Sys-path / PYTHONPATH wiring — mirrors benchmark.py so every sub-import
# resolves correctly regardless of the user's current working directory.
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent.parent
_LOCAL_PATHS = [
    _REPO / "browsergym" / "core" / "src",
    _REPO / "browsergym" / "experiments" / "src",
    _REPO / "browsergym" / "knows",
    _REPO / "browsergym" / "knows" / "src",
    _REPO / "AgentLab-Knows" / "src",
    _REPO,
]
for _p in _LOCAL_PATHS:
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(_p) for _p in _LOCAL_PATHS if _p.is_dir()]
    + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
)


def _load_dotenv_files() -> None:
    """Load repo env files before sharing/evaluator imports need credentials."""
    for env_path in (_REPO / ".env", _REPO / "browsergym" / "knows" / ".env"):
        if not env_path.is_file():
            continue
        try:
            with open(env_path) as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[len("export ") :]
                    if "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
        except Exception as exc:  # noqa: BLE001 - best-effort env loading
            print(f"Warning: failed to read {env_path}: {exc}", file=sys.stderr)


_load_dotenv_files()

# ---------------------------------------------------------------------------
# Mapping: short split name → task class
# ---------------------------------------------------------------------------
def _build_split_map():
    from browsergym.knows.task import (  # type: ignore
        DocsEducationLessonPlanTask,
        DocsFormalLetterTask,
        DocsInfluentialPapersTask,
        DocsPersonalRecipeOcrTask,
        DocsReferenceListTask,
        SheetsApartmentFinderTask,
        SheetsMovieRecommendationTask,
        SheetsPaperSortingTask,
        SheetsPersonalRecipeTask,
        SheetsPersonalTravelPlannerTask,
        SheetsRunningAnalysisTask,
        SheetsSkiTourPlanTask,
        SheetsStockTrackerTask,
        SheetsWeddingPlannerTask,
        SlidesBasicEducationalSlideDeckTask,
        SlidesBuyCarPresTask,
        SlidesEventAnnouncementPosterTask,
        SlidesIllustratedBookReportTask,
        SlidesPersonalLookbookPaintColorsTask,
        SlidesRemoveImagesAddPlaceholdersTask,
        SlidesWikipediaPhotosTask,
    )
    return {
        "docs_1": DocsFormalLetterTask,
        "docs_5": DocsInfluentialPapersTask,
        "docs_11": DocsPersonalRecipeOcrTask,
        "docs_31": DocsEducationLessonPlanTask,
        "docs_37": DocsReferenceListTask,
        "sheets_2": SheetsPersonalRecipeTask,
        "sheets_6": SheetsStockTrackerTask,
        "sheets_10": SheetsPaperSortingTask,
        "sheets_7": SheetsRunningAnalysisTask,
        "sheets_25": SheetsSkiTourPlanTask,
        "sheets_28": SheetsPersonalTravelPlannerTask,
        "sheets_38": SheetsApartmentFinderTask,
        "sheets_45": SheetsWeddingPlannerTask,
        "sheets_55": SheetsMovieRecommendationTask,
        "slides_17": SlidesRemoveImagesAddPlaceholdersTask,
        "slides_20": SlidesIllustratedBookReportTask,
        "slides_26": SlidesBasicEducationalSlideDeckTask,
        "slides_29": SlidesBuyCarPresTask,
        "slides_30": SlidesWikipediaPhotosTask,
        "slides_39": SlidesPersonalLookbookPaintColorsTask,
        "slides_51": SlidesEventAnnouncementPosterTask,
    }


def _result_has_fetch_permission_error(result) -> bool:
    """True when grading failed because the service account could not read the doc."""
    for cp in result.get_detailed_report().get("checkpoints", []):
        for step in cp.get("steps", []):
            details = step.get("details") or ""
            if "Error fetching document content" in details:
                return True
    return False


def _share_doc(doc_id: str, *, kind: Optional[str]) -> None:
    """Best-effort: share *doc_id* with the evaluator service account.

    Delegates to :func:`browsergym.knows.share_ui_fallback.share_doc_with_fallback`
    which runs the two-tier flow (Drive API first, then Playwright UI
    fallback against the editor's Share dialog). The UI fallback now
    auto-refreshes ``storage_state.json`` when the navigation lands on a
    Google sign-in page, so a stale snapshot no longer aborts the whole
    flow.

    Set ``KNOWS_DISABLE_UI_SHARE_FALLBACK=1`` to skip the UI fallback
    entirely (useful when debugging the API path on its own).
    """
    from browsergym.knows.share_ui_fallback import share_doc_with_fallback  # type: ignore

    share_doc_with_fallback(doc_id, kind=kind)


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
        from browsergym.knows.share_ui_fallback import kind_from_split_or_family  # type: ignore

        _share_doc(doc_id, kind=kind_from_split_or_family(split))
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

    if not share and _result_has_fetch_permission_error(result):
        print(
            "\n[run_evaluator] Service account cannot read this document "
            "(likely not shared yet). Retrying with auto-share...\n",
            file=sys.stderr,
        )
        from browsergym.knows.share_ui_fallback import kind_from_split_or_family  # type: ignore

        _share_doc(doc_id, kind=kind_from_split_or_family(split))
        print()
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
        "--no-ui-share-fallback",
        action="store_true",
        help=(
            "Try the Drive API share but skip the Playwright UI fallback "
            "(sets KNOWS_DISABLE_UI_SHARE_FALLBACK=1). Useful for batch "
            "re-evaluation when storage_state.json is stale and the UI tier "
            "would just hang."
        ),
    )
    parser.add_argument(
        "--no-auto-prompt",
        action="store_true",
        help=(
            "Skip the interactive Y/n prompt that auto-launches "
            "`google_auto_login.py --headed` when the headless mint of "
            "storage_state.json fails (sets KNOWS_DISABLE_AUTO_PROMPT_LOGIN=1). "
            "By default the prompt runs whenever the script is attached to "
            "a TTY so first-run device trust / 2SV challenges can be "
            "completed without re-running the harness manually."
        ),
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

    if args.no_ui_share_fallback:
        os.environ["KNOWS_DISABLE_UI_SHARE_FALLBACK"] = "1"

    if args.no_auto_prompt:
        os.environ["KNOWS_DISABLE_AUTO_PROMPT_LOGIN"] = "1"

    run(args.split, args.instance, args.doc_id, share=not args.no_share)


if __name__ == "__main__":
    main()
