"""Builds one client-safe 5-7 slide deck PER PROJECT, from the same JSON
reports generator.py reads for the portfolio deck.

Why this exists alongside generator.py's combined deck: the assignment
asks for a deck "a VP could present to a client with minimal edits," but
the combined portfolio deck necessarily shows every tracked project side
by side — and in these sample files, S2P and Plan B are two different,
unrelated client engagements (Titan, UniSan). Handing either client a
deck that names the other client's project would be a real confidentiality
problem, not just a rough edge. So: the portfolio deck is the internal
leadership review (cross-project trends, as literally requested); this
module produces the thing you'd actually hand to one specific client —
same visual style, same 5-7 slide structure, but scoped to just their
project, with zero references to any other project or client.

"Trends across projects" doesn't apply here by definition (one project,
one client) — this deck leans on the other two Phase 3 asks instead:
highlighting emerging risks and executive-level recommendations, both of
which are still fully meaningful for a single project.

Run:
    python presentation/client_deck.py
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches
from pptx.dml.color import RGBColor

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "outputs"

sys.path.insert(0, str(BASE_DIR))
import llm_client
from llm_explainer import plain_english_phrase
from pdf_report import AREA_LABELS, STATUS_GLOSS, CONFIDENCE_GLOSS

from presentation.generator import (
    NAVY, ICE, WHITE, SLATE, MUTED, CARD_BG, RAG_COLORS, SLIDE_W, SLIDE_H,
    _blank_slide, _rect, _textbox, _slide_header, _rag_chip, _fit_rows, _add_overflow_note, _period_label,
    _latest_reports, _report_history, _current_month_reports,
    add_milestone_tracker_slide, add_trend_analysis_slide, add_recommendations_slide,
)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return slug or "project"


def _deterministic_single_recommendations(report: dict) -> list[str]:
    """No-LLM fallback for one project's recommendations — mirrors
    generator.py's _deterministic_recommendations but never references
    any other project, since this deck is meant to leave the building."""
    recs = []
    if report["rag_status"] in ("Red", "Amber") and report.get("red_signals"):
        worst = ", ".join(AREA_LABELS.get(n, n.replace("_", " ").title()) for n in report["red_signals"])
        recs.append(f"Address {worst} first — these are the areas currently driving the status.")
    for name, result in report["signals"].items():
        if result and result["score"] == 60:
            phrase = plain_english_phrase(name, (result["score"], result["raw_metric"]))
            recs.append(f"Keep a close eye on {AREA_LABELS.get(name, name)}: {phrase}.")
    if not recs:
        recs.append("No urgent action needed this month; maintain the current monitoring cadence.")
    return recs[:5]


def _generate_single_project_recommendations(report: dict) -> list[str]:
    prompt = (
        f"Project: {report['project_name']}\n"
        f"Status: {report['rag_status']} (confidence {report['confidence']})\n"
        f"{report['explanation']['narrative']}\n\n"
        "Write 3 to 5 short, specific, action-oriented recommendations suitable to present directly "
        "to this client. Do not reference any other project, client, or company by name. "
        "Output ONLY a fenced ```json code block containing a JSON array of strings, nothing else."
    )
    text = llm_client.generate(
        "You produce crisp, client-facing project recommendations grounded strictly in the data given.",
        prompt,
    )

    def _valid_list(candidate) -> bool:
        return isinstance(candidate, list) and bool(candidate) and all(isinstance(x, str) for x in candidate)

    for pattern in (r"```json\s*(\[.*?\])\s*```", r"\[.*\]"):
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1) if match.groups() else match.group(0))
                if _valid_list(parsed):
                    return parsed[:5]
            except json.JSONDecodeError:
                pass

    return _deterministic_single_recommendations(report)


def add_client_title_slide(prs, report: dict, generated_label: str, history: dict = None):
    slide = _blank_slide(prs)
    _rect(slide, 0, 0, SLIDE_W, SLIDE_H, NAVY)
    _textbox(slide, Inches(0.9), Inches(2.6), Inches(11.5), Inches(0.9),
              report["project_name"], size=36, color=WHITE, bold=True, font="Cambria")
    _textbox(slide, Inches(0.9), Inches(3.6), Inches(11.5), Inches(0.6),
              "Monthly Project Health Summary", size=18, color=ICE)
    _textbox(slide, Inches(0.9), Inches(4.2), Inches(11.5), Inches(0.5),
              f"Generated {generated_label}", size=13, color=ICE)
    period = _period_label({report["project_name"]: history.get(report["project_name"], [report])} if history else None, [report])
    if period:
        _textbox(slide, Inches(0.9), Inches(4.65), Inches(11.5), Inches(0.5),
                  period, size=13, color=ICE)


def add_status_overview_slide(prs, report: dict):
    slide = _blank_slide(prs)
    _slide_header(slide, "Status at a Glance")

    status = report["rag_status"]
    _rag_chip(slide, Inches(0.6), Inches(1.75), status, w=Inches(1.8), h=Inches(0.55))
    _textbox(slide, Inches(2.7), Inches(1.8), Inches(9.8), Inches(0.5),
              STATUS_GLOSS.get(status, ""), size=15, color=NAVY, bold=True)

    confidence = report["confidence"]
    gloss = CONFIDENCE_GLOSS.get(confidence, "")
    _textbox(slide, Inches(0.6), Inches(2.55), Inches(12.1), Inches(0.5),
              f"Confidence in this read: {confidence} — {gloss}.", size=13, color=SLATE)

    y = Inches(3.25)
    if report.get("escalation_applied"):
        friendly = [AREA_LABELS.get(n, n.replace("_", " ").title()) for n in report["red_signals"]]
        if len(friendly) >= 2:
            note = (
                f"Escalation note: {' and '.join(friendly)} are both seriously behind this month. When two "
                "or more areas are this critical, the status is capped at Red regardless of the composite score."
            )
        else:
            note = (
                f"Escalation note: {friendly[0]} is seriously behind this month. When one area is this "
                "critical, the status is capped at Amber regardless of the composite score."
            )
        _rect(slide, Inches(0.6), y, Inches(12.1), Inches(0.75), ICE)
        _textbox(slide, Inches(0.9), y + Inches(0.15), Inches(11.5), Inches(0.5),
                  note, size=12, color=NAVY, bold=True)
        y += Inches(1.05)

    _textbox(slide, Inches(0.6), y, Inches(12.1), Inches(1.5), report["explanation"]["narrative"], size=14, color=SLATE)


def add_signal_detail_slide(prs, report: dict):
    slide = _blank_slide(prs)
    _slide_header(slide, "What's Driving This Status", "Every area we measured, in plain terms")

    items = list(report["signals"].items())
    gap = Inches(0.15)
    shown_count, row_h, list_bottom, overflow = _fit_rows(
        Inches(1.7), Inches(7.15), Inches(0.75), gap, len(items)
    )

    y = Inches(1.7)
    for name, result in items[:shown_count]:
        area = AREA_LABELS.get(name, name.replace("_", " ").title())
        _rect(slide, Inches(0.6), y, Inches(12.1), row_h, CARD_BG)
        _textbox(slide, Inches(0.9), y + Inches(0.12), Inches(2.6), row_h - Inches(0.24),
                  area, size=14, color=NAVY, bold=True)
        if result is None:
            _rag_chip(slide, Inches(3.7), y + (row_h - Inches(0.35)) / 2, "N/A", w=Inches(1.1), h=Inches(0.35))
            _textbox(slide, Inches(5.1), y + Inches(0.12), Inches(7.3), row_h - Inches(0.24),
                      "Not enough data for this project — left out of the score.", size=12, color=MUTED)
        else:
            status_word = {100: "Green", 60: "Amber", 20: "Red"}.get(result["score"], "Red")
            _rag_chip(slide, Inches(3.7), y + (row_h - Inches(0.35)) / 2, status_word, w=Inches(1.1), h=Inches(0.35))
            phrase = plain_english_phrase(name, (result["score"], result["raw_metric"]))
            phrase = phrase[0].upper() + phrase[1:]
            _textbox(slide, Inches(5.1), y + Inches(0.12), Inches(7.3), row_h - Inches(0.24),
                      phrase, size=12, color=SLATE)
        y += row_h + gap

    _add_overflow_note(slide, list_bottom, overflow)


def build_client_deck(report: dict) -> Path:
    recommendations = _generate_single_project_recommendations(report)

    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H

    history = _current_month_reports(_report_history())
    generated_label = datetime.now().strftime("%Y-%m-%d %H:%M")
    add_client_title_slide(prs, report, generated_label, history)
    add_status_overview_slide(prs, report)
    add_signal_detail_slide(prs, report)
    add_milestone_tracker_slide(prs, [report])
    add_trend_analysis_slide(prs, [report], history)
    add_recommendations_slide(prs, recommendations, subtitle="Generated for a VP audience from this month's project data")

    OUTPUT_DIR.mkdir(exist_ok=True)
    slug = _slugify(report["project_name"])
    path = OUTPUT_DIR / f"{slug}_client_deck.pptx"
    prs.save(path)
    return path


def main():
    reports = _latest_reports()
    if not reports:
        raise RuntimeError("No JSON reports found in outputs/. Run main.py first.")
    paths = []
    for report in reports:
        path = build_client_deck(report)
        print(f"Client deck written to {path}")
        paths.append(path)
    return paths


if __name__ == "__main__":
    main()
