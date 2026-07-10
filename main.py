"""Orchestrator: for each project workbook, run
data_loader -> rag_engine -> llm_explainer and write a timestamped
{project}_weekly_report.json (machine-readable, feeds the decks) and .pdf
(human-facing) to /outputs, then build both decks from those same
outputs — the portfolio deck (internal leadership review, cross-project
trends) and one client-safe deck per project (single project, safe to
actually hand to that client). One command does all of it:

    python main.py

No scoring logic lives here — this module only wires the pipeline
together and formats the result for disk.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from data_loader import load_project
from rag_engine import (
    evaluate_project, sentiment_trend, top_risk_rows, classify_all_tasks,
    current_week_start, current_week_end, this_week_task_digest,
    is_last_week_of_month,
)
from llm_explainer import explain
from pdf_report import render_pdf
from presentation import generator as deck_generator
from presentation import client_deck

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "outputs"


def _discover_project_files() -> list[Path]:
    """Any .xlsx dropped in data/ is picked up automatically — nothing is
    hardcoded to the two original sample files. Excel's own temp lock
    files (~$...xlsx, created while a workbook is open) are skipped."""
    if not DATA_DIR.exists():
        return []
    return sorted(p for p in DATA_DIR.glob("*.xlsx") if not p.name.startswith("~$"))


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return slug or "project"


def _serialize_signals(signals: dict) -> dict:
    return {
        name: (None if result is None else {"score": result[0], "raw_metric": result[1]})
        for name, result in signals.items()
    }


def _build_sentiment_trend(project: dict) -> list[dict]:
    """The one genuinely real historical trend available from this data:
    comment timestamps are real, parsed facts, so re-scoring sentiment
    with only the comments that existed as of each past week is measuring
    history, not modeling it — unlike every other signal, which only has
    a single current-state value. Weeks with zero comments-so-far show
    score=None rather than a fabricated Green, since the signal genuinely
    wasn't computable that early."""
    report_date = project["report_run_date"]
    checkpoints = [report_date - pd.Timedelta(weeks=w) for w in (3, 2, 1, 0)]
    results = sentiment_trend(project["comments"], project["has_sentiment_data"], checkpoints)
    return [
        {
            "as_of": str(cutoff),
            "score": None if result is None else result[0],
            "raw_metric": None if result is None else result[1],
        }
        for cutoff, result in zip(checkpoints, results)
    ]


def _build_report(project: dict, composite: dict, explanation: dict, generated_at: str) -> dict:
    return {
        "project_name": project["project_name"],
        "source_file": project["source_file"],
        "generated_at": generated_at,
        "report_run_date": str(project["report_run_date"]),
        "report_week_start": str(current_week_start(project["report_run_date"])),
        "report_week_end": str(current_week_end(project["report_run_date"])),
        "rag_status": composite["status"],
        "composite_score": composite.get("composite_score"),
        "confidence": composite["confidence"],
        "escalation_applied": composite.get("escalation_applied", False),
        "red_signals": composite.get("red_signals", []),
        "signals": _serialize_signals(composite["signals"]),
        "weights_used": composite.get("weights_used", {}),
        "explanation": explanation,
        "data_completeness_pct": project["completeness_pct"],
        "has_budget_data": project["has_budget_data"],
        "has_sentiment_data": project["has_sentiment_data"],
        "sentiment_trend": _build_sentiment_trend(project),
        "top_risk_rows": top_risk_rows(project["tasks"], project["report_run_date"]),
        "all_task_status": classify_all_tasks(project["tasks"], project["report_run_date"]),
        "this_week_tasks": this_week_task_digest(project["tasks"], project["report_run_date"]),
        "hierarchy_depth_field_used": project["hierarchy_depth_field_used"],
        "counts": {
            "tasks": len(project["tasks"]),
            "milestones": len(project["milestones"]),
            "blockers": len(project["blockers"]),
            "comments": len(project["comments"]),
        },
        "source_summary_snapshot": project["summary_sheet_snapshot"],
    }


def run_for_project(path: Path) -> dict:
    print(f"Loading {path.name} ...")
    project = load_project(str(path))

    print(f"Scoring {project['project_name']} ...")
    composite = evaluate_project(project)

    print(f"Generating explanation for {project['project_name']} ...")
    explanation = explain(project["project_name"], composite)

    generated_at = datetime.now().strftime("%Y%m%dT%H%M%S")
    report = _build_report(project, composite, explanation, generated_at)

    OUTPUT_DIR.mkdir(exist_ok=True)
    slug = _slugify(project["project_name"])
    json_path = OUTPUT_DIR / f"{slug}_weekly_report_{generated_at}.json"
    pdf_path = OUTPUT_DIR / f"{slug}_weekly_report_{generated_at}.pdf"

    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    render_pdf(report, pdf_path)

    print(f"  -> {json_path.name}")
    print(f"  -> {pdf_path.name}")
    return report


def main():
    project_files = _discover_project_files()
    if not project_files:
        print(f"No .xlsx files found in {DATA_DIR}", file=sys.stderr)
        return []

    reports = []
    for path in project_files:
        try:
            reports.append(run_for_project(path))
        except Exception as exc:
            # One malformed workbook shouldn't take down the whole weekly
            # run for every other project — report it and keep going.
            print(f"Skipping {path.name}: {exc}", file=sys.stderr)

    if reports:
        latest_date = max(pd.Timestamp(r["report_run_date"]) for r in reports)
        if is_last_week_of_month(latest_date):
            print("This week is the last week of the month — building the monthly decks ...")
            deck_path = deck_generator.main()
            print(f"  -> {deck_path.name}")

            print("Building per-project client decks ...")
            for client_deck_path in client_deck.main():
                print(f"  -> {client_deck_path.name}")
        else:
            print(
                "Month not finished yet — skipping deck generation this week. "
                "Run `python presentation/generator.py` / `python presentation/client_deck.py` "
                "directly to build decks on demand regardless of month completion."
            )

    return reports


if __name__ == "__main__":
    main()
