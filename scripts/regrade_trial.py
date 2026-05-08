"""Re-grade a previously completed KNOWS trial.

Use this when a trial finished without producing per-checkpoint scores in
``summary_info.json`` (e.g. the agent reported the task infeasible, was
truncated, or returned an empty action), but the underlying Google Doc still
exists and can be evaluated. The script:

1. Locates the trial's Google Doc id (from ``task_info.json`` ``visited_urls``
   or ``current_url``, with chat-message and goal-object fallbacks).
2. Determines the task instance id (from the directory name or
   ``exp_args.pkl``).
3. Runs the corresponding evaluator's ``grade_checkpoints(...)``.
4. Patches ``task_info.json`` and ``summary_info.json`` in-place so that
   per-checkpoint scores are exposed and ``cum_reward`` / ``cum_raw_reward``
   reflect the aggregated checkpoint score.

Example:
    python scripts/regrade_trial.py \
        "results/2026-04-24_22-22-34_GenericAgent-..._on_knows.docs_1_formal_letter.4_20"
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import pickle
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BG_LOCAL_SRCS = [
    _REPO_ROOT / "browsergym" / "core" / "src",
    _REPO_ROOT / "browsergym" / "experiments" / "src",
    _REPO_ROOT / "browsergym" / "knows",
    _REPO_ROOT / "browsergym" / "knows" / "src",
    _REPO_ROOT / "AgentLab-Knows" / "src",
]
for _p in reversed(_BG_LOCAL_SRCS):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_existing_pp = os.environ.get("PYTHONPATH", "")
_pp_parts = _existing_pp.split(os.pathsep) if _existing_pp else []
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(p) for p in _BG_LOCAL_SRCS if p.is_dir()]
    + [p for p in _pp_parts if p not in {str(s) for s in _BG_LOCAL_SRCS}]
)


def _load_dotenv_files() -> None:
    """Mirror benchmark.py's implicit env loading.

    The evaluators read ``GOOGLE_AI_API_KEY`` (and similar) at module-import
    time, so we need these set before ``_load_evaluator`` runs. We look in
    standard locations and only set vars that aren't already in the
    environment, so explicit overrides win.
    """
    candidate_envs = [
        _REPO_ROOT / ".env",
        _REPO_ROOT / "browsergym" / "knows" / ".env",
    ]
    for env_path in candidate_envs:
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
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception as exc:  # noqa: BLE001 - best-effort loader
            print(f"  Warning: failed to read {env_path}: {exc}")


_load_dotenv_files()

_WORKSPACE_RE = re.compile(r"/(?:document|spreadsheets|presentation)/d/([a-zA-Z0-9_-]+)")
_INSTANCE_RE = re.compile(r"\.(\d+)_\d+$")


def _load_task_info(exp_dir: Path) -> Dict[str, Any]:
    task_info_path = exp_dir / "task_info.json"
    if not task_info_path.exists():
        return {}
    try:
        with open(task_info_path) as f:
            data = json.load(f)
    except Exception as exc:
        print(f"  Warning: could not parse task_info.json: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _url_from_entry(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("url", "") or "")
    return str(entry or "")


def _find_doc_id(exp_dir: Path) -> Optional[str]:
    """Search the trial directory for the doc id the agent worked on."""
    data = _load_task_info(exp_dir)

    for k in ("doc_id", "created_doc_id"):
        v = data.get(k)
        if isinstance(v, str) and v:
            return v

    for history_key in ("visited", "visited_urls"):
        for url_entry in data.get(history_key, []) or []:
            m = _WORKSPACE_RE.search(_url_from_entry(url_entry))
            if m:
                return m.group(1)

    m = _WORKSPACE_RE.search(data.get("current_url", "") or "")
    if m:
        return m.group(1)

    # Fallback: scan step_0 chat messages / goal for a doc URL.
    step0_path = exp_dir / "step_0.pkl.gz"
    if step0_path.exists():
        try:
            with gzip.open(step0_path, "rb") as f:
                step0 = pickle.load(f)
            obs = getattr(step0, "obs", None) or {}
            chat = obs.get("chat_messages", []) if isinstance(obs, dict) else []
            for msg in chat:
                text = msg.get("message", "") if isinstance(msg, dict) else ""
                m = _WORKSPACE_RE.search(text)
                if m:
                    return m.group(1)
            goal = obs.get("goal", "") if isinstance(obs, dict) else ""
            m = _WORKSPACE_RE.search(goal or "")
            if m:
                return m.group(1)
        except Exception as exc:
            print(f"  Warning: failed to load step_0.pkl.gz for doc_id lookup: {exc}")

    return None


def _find_instance_id(exp_dir: Path) -> Optional[int]:
    """Locate the task instance id (e.g. 4 for docs_1_formal_letter instance 4)."""
    data = _load_task_info(exp_dir)
    try:
        instance_id = data.get("instance_id")
        if instance_id is not None:
            return int(instance_id)
    except (TypeError, ValueError):
        pass

    m = _INSTANCE_RE.search(exp_dir.name)
    if m:
        return int(m.group(1))

    exp_args_path = exp_dir / "exp_args.pkl"
    if exp_args_path.exists():
        try:
            with open(exp_args_path, "rb") as f:
                exp_args = pickle.load(f)
            task_kwargs = getattr(getattr(exp_args, "env_args", None), "task_kwargs", None)
            if isinstance(task_kwargs, dict) and "instance_id" in task_kwargs:
                return int(task_kwargs["instance_id"])
            task_name = getattr(getattr(exp_args, "env_args", None), "task_name", "") or ""
            m = _INSTANCE_RE.search(task_name)
            if m:
                return int(m.group(1))
        except Exception as exc:
            print(f"  Warning: failed to read exp_args.pkl: {exc}")
    return None


def _find_browsing_history(exp_dir: Path) -> List[str]:
    data = _load_task_info(exp_dir)
    urls: List[str] = []
    for history_key in ("visited", "visited_urls"):
        for entry in data.get(history_key, []) or []:
            url = _url_from_entry(entry)
            if url:
                urls.append(url)
    return urls


def _find_task_class(exp_dir: Path) -> Type[Any]:
    """Resolve a trial directory to the matching task class."""
    from browsergym.knows.task import (  # type: ignore
        DocsFormalLetterTask,
        DocsInfluentialPapersTask,
        SheetsMovieRecommendationTask,
        SheetsApartmentFinderTask,
        SheetsPersonalRecipeTask,
        SheetsStockTrackerTask,
        SlidesBuyCarPresTask,
        SlidesEventAnnouncementPosterTask,
        SlidesIllustratedBookReportTask,
        SlidesPersonalLookbookPaintColorsTask,
        SlidesRemoveImagesAddPlaceholdersTask,
        SlidesWikipediaPhotosTask,
    )

    family_to_task = {
        DocsFormalLetterTask.TASK_FAMILY_FOLDER: DocsFormalLetterTask,
        DocsInfluentialPapersTask.TASK_FAMILY_FOLDER: DocsInfluentialPapersTask,
        SheetsMovieRecommendationTask.TASK_FAMILY_FOLDER: SheetsMovieRecommendationTask,
        SheetsApartmentFinderTask.TASK_FAMILY_FOLDER: SheetsApartmentFinderTask,
        SheetsPersonalRecipeTask.TASK_FAMILY_FOLDER: SheetsPersonalRecipeTask,
        SheetsStockTrackerTask.TASK_FAMILY_FOLDER: SheetsStockTrackerTask,
        SlidesBuyCarPresTask.TASK_FAMILY_FOLDER: SlidesBuyCarPresTask,
        SlidesEventAnnouncementPosterTask.TASK_FAMILY_FOLDER: SlidesEventAnnouncementPosterTask,
        SlidesIllustratedBookReportTask.TASK_FAMILY_FOLDER: SlidesIllustratedBookReportTask,
        SlidesPersonalLookbookPaintColorsTask.TASK_FAMILY_FOLDER: SlidesPersonalLookbookPaintColorsTask,
        SlidesRemoveImagesAddPlaceholdersTask.TASK_FAMILY_FOLDER: SlidesRemoveImagesAddPlaceholdersTask,
    }
    prefix_to_task = {
        "docs_1_formal_letter": DocsFormalLetterTask,
        "docs_5_influential_papers": DocsInfluentialPapersTask,
        "sheets_55_movie_recommendation": SheetsMovieRecommendationTask,
        "sheets_2_personal_recipe": SheetsPersonalRecipeTask,
        "sheets_6_stock_tracker": SheetsStockTrackerTask,
        "sheets_38_apartment_finder": SheetsApartmentFinderTask,
        "slides_20_illustrated_book_report": SlidesIllustratedBookReportTask,
        "slides_17_remove_images_add_placeholders": SlidesRemoveImagesAddPlaceholdersTask,
        "slides_29_buy_car_pres": SlidesBuyCarPresTask,
        "slides_39_personal_lookbook_paintcolors": SlidesPersonalLookbookPaintColorsTask,
        "slides_51_event_announcement_poster": SlidesEventAnnouncementPosterTask,
        "slides_30_wikipedia_photos": SlidesWikipediaPhotosTask,
    }

    data = _load_task_info(exp_dir)
    family = data.get("task_family")
    if isinstance(family, str) and family in family_to_task:
        return family_to_task[family]

    for marker, task_cls in prefix_to_task.items():
        if marker in exp_dir.name:
            return task_cls

    raise RuntimeError(
        f"Could not determine task family from {exp_dir.name} or task_info.json."
    )


def _make_json_safe(value: Any) -> Any:
    """Convert non-JSON-serializable scalars to plain Python types."""
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _clear_stale_error_fields(data: Dict[str, Any]) -> None:
    """Remove failure markers from older grading attempts after a good regrade."""
    for key in ("evaluation_error", "evaluation_skipped", "evaluation_skip_reason"):
        data.pop(key, None)


def regrade(exp_dir: Path) -> Dict[str, Any]:
    """Re-evaluate ``exp_dir`` and patch its summary / task info files.

    Returns the score breakdown dict that was written.
    """
    if not exp_dir.exists():
        raise FileNotFoundError(f"Experiment directory not found: {exp_dir}")

    doc_id = _find_doc_id(exp_dir)
    if not doc_id:
        raise RuntimeError(
            f"Could not locate a Google Docs id in {exp_dir}; cannot re-grade."
        )

    instance_id = _find_instance_id(exp_dir)
    if instance_id is None:
        raise RuntimeError(
            f"Could not determine the task instance id from {exp_dir.name} or "
            f"exp_args.pkl."
        )

    print(f"Re-grading trial: {exp_dir.name}")
    print(f"  doc_id     = {doc_id}")
    print(f"  instance_id = {instance_id}")

    task_cls = _find_task_class(exp_dir)
    task = task_cls(instance_id=instance_id)

    # Old trials were run before doc_setup auto-shared each created doc with
    # the evaluator's service account. The evaluator authenticates as that
    # service account, so without explicit sharing it can't see (and 403s on)
    # docs sitting inside the user's personal Drive. Try a best-effort
    # two-tier share here (Drive API first, then Playwright UI fallback that
    # auto-refreshes storage_state.json when its cookies are stale) so
    # re-grading those trials succeeds even when the original owner's
    # session has aged past Google's rotation window.
    #
    # Set ``KNOWS_SKIP_REGRADE_SHARE=1`` to skip the share entirely (useful
    # when you know the SA already has access) and
    # ``KNOWS_DISABLE_UI_SHARE_FALLBACK=1`` to skip just the Playwright tier.
    if os.environ.get("KNOWS_SKIP_REGRADE_SHARE", "").strip().lower() in {
        "1", "true", "yes", "on"
    }:
        print(f"  Skipping share step (KNOWS_SKIP_REGRADE_SHARE set).")
    else:
        try:
            from browsergym.knows.share_ui_fallback import (  # type: ignore
                share_doc_with_fallback,
                kind_from_split_or_family,
            )

            # Prefer the recorded task family (set by the runner) over the
            # directory-name guess so re-grading a Sheets trial doesn't
            # accidentally drive a Docs-shaped Share dialog.
            kind: Optional[str] = None
            data = _load_task_info(exp_dir)
            workspace_kind = data.get("workspace_kind")
            if isinstance(workspace_kind, str) and workspace_kind:
                kind = workspace_kind
            if kind is None:
                family = data.get("task_family")
                if isinstance(family, str) and family:
                    kind = kind_from_split_or_family(family)
            if kind is None:
                kind = kind_from_split_or_family(exp_dir.name)

            if share_doc_with_fallback(doc_id, kind=kind):
                print(
                    f"  Shared doc {doc_id} (kind={kind}) with the evaluator's "
                    "service account."
                )
            else:
                print(
                    f"  Note: could not auto-share doc {doc_id} with the evaluator "
                    "(see warnings above). Grading may fail if the doc isn't "
                    "already accessible to the service account."
                )
        except Exception as exc:  # noqa: BLE001 - best-effort
            print(f"  Warning: doc-share helper raised: {exc}")

    evaluator = task._load_evaluator()

    print("Running evaluator.grade_checkpoints(...)")
    grade_fn = evaluator.grade_checkpoints
    accepted = task._accepted_kwargs(grade_fn)
    browsing_history = _find_browsing_history(exp_dir)
    call_kwargs: Dict[str, Any] = {}
    if "workspace_doc_id" in accepted:
        call_kwargs["workspace_doc_id"] = doc_id
    if "browsing_history" in accepted:
        call_kwargs["browsing_history"] = browsing_history
    if "browsing_history_list" in accepted:
        call_kwargs["browsing_history_list"] = browsing_history
    if "cached_models" in accepted and "cached_models" not in call_kwargs:
        call_kwargs["cached_models"] = None
    result = grade_fn(**call_kwargs)

    info: Dict[str, Any] = {
        "doc_id": doc_id,
        "instance_id": instance_id,
    }
    reward, score_breakdown = task._summarize_result(result, info)
    info.update(score_breakdown)
    info["regrade.reward"] = reward
    info["regrade.source"] = "regrade_trial.py"

    task_info_path = exp_dir / "task_info.json"
    if task_info_path.exists():
        with open(task_info_path) as f:
            task_info = json.load(f)
    else:
        task_info = {}
    _clear_stale_error_fields(task_info)
    task_info.update(info)
    with open(task_info_path, "w") as f:
        json.dump(task_info, f, indent=4, default=str)
    print(f"Patched {task_info_path.name}")

    summary_info_path = exp_dir / "summary_info.json"
    if summary_info_path.exists():
        with open(summary_info_path) as f:
            summary_info = json.load(f)
    else:
        summary_info = {}
    _clear_stale_error_fields(summary_info)
    summary_info["cum_reward"] = float(reward)
    summary_info["cum_raw_reward"] = float(reward)
    for k, v in info.items():
        if isinstance(v, (int, float, str, bool)) or v is None:
            summary_info[k] = _make_json_safe(v)
    with open(summary_info_path, "w") as f:
        json.dump(summary_info, f, indent=4, default=str)
    print(f"Patched {summary_info_path.name}")

    print(
        f"\nFinal aggregated reward = {reward:.4f} "
        f"({info.get('eval.score_result')}/{info.get('eval.score_total')})"
    )
    print("Per-checkpoint breakdown:")
    n_cp = info.get("eval.n_checkpoints", 0) or 0
    for i in range(1, int(n_cp) + 1):
        name = info.get(f"eval.cp{i}_name", f"checkpoint_{i}")
        r = info.get(f"eval.cp{i}_result")
        t = info.get(f"eval.cp{i}_total")
        frac = info.get(f"eval.cp{i}_fraction")
        print(f"  cp{i} '{name}': {r}/{t}  (fraction={frac:.3f})")

    return info


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-grade a completed KNOWS trial directory."
    )
    parser.add_argument(
        "exp_dir",
        type=Path,
        help="Path to the trial directory (the one containing summary_info.json).",
    )
    parser.add_argument(
        "--no-share",
        action="store_true",
        help=(
            "Skip the auto-share step entirely (sets KNOWS_SKIP_REGRADE_SHARE=1). "
            "Use when the SA already has access and you don't want to wait on "
            "the share-fallback path."
        ),
    )
    parser.add_argument(
        "--no-ui-share-fallback",
        action="store_true",
        help=(
            "Try the Drive API share but skip the Playwright UI fallback "
            "(sets KNOWS_DISABLE_UI_SHARE_FALLBACK=1). Useful when "
            "storage_state.json is stale and you'd rather just record a 0."
        ),
    )
    args = parser.parse_args()

    if args.no_share:
        os.environ["KNOWS_SKIP_REGRADE_SHARE"] = "1"
    if args.no_ui_share_fallback:
        os.environ["KNOWS_DISABLE_UI_SHARE_FALLBACK"] = "1"

    try:
        regrade(args.exp_dir.resolve())
    except Exception as exc:
        print(f"Re-grade failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
