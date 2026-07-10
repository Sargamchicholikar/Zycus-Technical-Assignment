"""Renders one project's weekly report dict (built by main.py) as a
formatted PDF — the human-facing deliverable, written so someone brand
new to the project can read it in one pass with zero internal jargon.
main.py still writes the JSON alongside it; presentation/generator.py
reads that JSON to build the portfolio deck, so the PDF is additional,
not a replacement for it.
"""

from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from llm_explainer import plain_english_phrase

RAG_COLORS = {
    "Green": colors.HexColor("#2E8B57"),
    "Amber": colors.HexColor("#E09F2D"),
    "Red": colors.HexColor("#C0392B"),
    "Insufficient Data": colors.HexColor("#808080"),
}

STATUS_GLOSS = {
    "Green": "This project is healthy — no action needed right now.",
    "Amber": "This project needs attention — some areas are slipping.",
    "Red": "This project needs attention now — one or more areas are seriously behind.",
    "Insufficient Data": "There isn't enough data yet to call a status with confidence.",
}

# Friendlier area names — no internal field names anywhere a reader sees.
AREA_LABELS = {
    "schedule_slippage": "Schedule",
    "budget_burn": "Budget",
    "milestone_health": "Milestones",
    "blockers": "Blockers",
    "stakeholder_sentiment": "Team Feedback",
    "critical_path_health": "Key Deadlines",
}

CONFIDENCE_GLOSS = {
    "High": "most of the expected data was filled in, so this read can be trusted",
    "Medium": "some expected data was missing, so treat this read with a little caution",
    "Low": "a lot of expected data was missing — treat this read as a rough estimate",
}

_styles = getSampleStyleSheet()
_title_style = ParagraphStyle("TitleX", parent=_styles["Title"], fontSize=19, alignment=TA_LEFT, spaceAfter=2)
_sub_style = ParagraphStyle("SubX", parent=_styles["Normal"], fontSize=9.5, textColor=colors.HexColor("#666666"), spaceAfter=10)
_h_style = ParagraphStyle("HX", parent=_styles["Heading2"], fontSize=13, spaceBefore=12, spaceAfter=4, textColor=colors.HexColor("#1a1a1a"))
_gloss_style = ParagraphStyle("GlossX", parent=_styles["Normal"], fontSize=12, leading=15, spaceAfter=4, fontName="Helvetica-Bold")
_body_style = ParagraphStyle("BodyX", parent=_styles["Normal"], fontSize=11, leading=15, spaceAfter=6)
_escalation_style = ParagraphStyle("EscX", parent=_body_style, textColor=colors.HexColor("#8A5A00"), fontSize=10)
_small_style = ParagraphStyle("SmallX", parent=_styles["Normal"], fontSize=9, textColor=colors.HexColor("#555555"), spaceAfter=3)
_cell_style = ParagraphStyle("CellX", parent=_styles["Normal"], fontSize=9.5, leading=12)
_cell_head_style = ParagraphStyle("CellHeadX", parent=_cell_style, textColor=colors.white, fontName="Helvetica-Bold")
_status_word_style = ParagraphStyle("StatusWordX", parent=_cell_style, fontName="Helvetica-Bold")
_badge_style = ParagraphStyle("BadgeX", fontSize=16, textColor=colors.white, alignment=TA_CENTER, fontName="Helvetica-Bold")


def _esc(text) -> str:
    """ReportLab's Paragraph text is a tiny XML-like markup language, so
    any raw data value that might contain &, <, or > must be escaped
    before being embedded — and real task names very often do contain
    "&" (e.g. "OTK to share D&B creds"). Without this, ReportLab silently
    misparses the "&" as the start of an XML entity reference instead of
    treating it as literal text, corrupting the rendered name. Only ever
    call this on the dynamic value being substituted in, never on a whole
    string that already contains intentional markup like <b>."""
    if text is None:
        return ""
    return _xml_escape(str(text))


def _status_word(score):
    if score is None:
        return "N/A", colors.HexColor("#808080")
    if score == 100:
        return "Green", RAG_COLORS["Green"]
    if score == 60:
        return "Amber", RAG_COLORS["Amber"]
    return "Red", RAG_COLORS["Red"]


def _short_date(iso_str) -> str:
    """'2026-06-26 00:00:00' -> '06/26' — compact enough for a narrow
    table column."""
    if not iso_str:
        return "—"
    return f"{iso_str[5:7]}/{iso_str[8:10]}"


def _pct_str(pct) -> str:
    return "—" if pct is None else f"{pct * 100:.0f}%"


def _duration_str(days) -> str:
    return "—" if days is None else f"{days}d"


def render_pdf(report: dict, path: Path) -> None:
    doc = SimpleDocTemplate(
        str(path), pagesize=letter,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
    )
    story = []

    story.append(Paragraph("Weekly Project Health Report", _title_style))
    week_start, week_end = report.get("report_week_start"), report.get("report_week_end")
    period = (
        f"week of {week_start[:10]} &ndash; {week_end[:10]}"
        if week_start and week_end else f"report date: {report['report_run_date'][:10]}"
    )
    story.append(Paragraph(
        f"{_esc(report['project_name'])} &middot; {period}",
        _sub_style,
    ))

    status = report["rag_status"]
    badge_color = RAG_COLORS.get(status, RAG_COLORS["Insufficient Data"])
    badge = Table([[Paragraph(status, _badge_style)]], colWidths=[1.8 * inch], rowHeights=[0.45 * inch])
    badge.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), badge_color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(badge)
    story.append(Spacer(1, 8))

    story.append(Paragraph(STATUS_GLOSS.get(status, ""), _gloss_style))

    confidence = report["confidence"]
    gloss = CONFIDENCE_GLOSS.get(confidence, "")
    story.append(Paragraph(f"Confidence in this read: <b>{confidence}</b> — {gloss}.", _body_style))

    if report.get("escalation_applied"):
        friendly_names = [AREA_LABELS.get(n, n.replace("_", " ").title()) for n in report["red_signals"]]
        if len(friendly_names) >= 2:
            note = (
                f"Escalation note: {' and '.join(friendly_names)} are both seriously behind this week. "
                "When two or more areas are this critical, the status is capped at Red regardless of the composite score."
            )
        else:
            note = (
                f"Escalation note: {friendly_names[0]} is seriously behind this week. "
                "When one area is this critical, the status is capped at Amber regardless of the composite score."
            )
        story.append(Paragraph(note, _escalation_style))

    week_rows = report.get("this_week_tasks") or []
    if week_rows:
        story.append(Paragraph("This Week's Tasks", _h_style))
        story.append(Paragraph(
            f"Week of {report['report_week_start'][:10]} &ndash; {report['report_week_end'][:10]}. "
            "Every task or milestone active this week &mdash; Status, Start/End Date, % Complete, "
            "and planned Duration.",
            _small_style,
        ))
        week_data = [[
            Paragraph("Task", _cell_head_style),
            Paragraph("Status", _cell_head_style),
            Paragraph("Start", _cell_head_style),
            Paragraph("End", _cell_head_style),
            Paragraph("%", _cell_head_style),
            Paragraph("Dur.", _cell_head_style),
        ]]
        for row in week_rows:
            word_color = RAG_COLORS.get(row["status"], RAG_COLORS["Insufficient Data"])
            week_data.append([
                Paragraph(_esc(row["task_name"]) or "(unnamed task)", _cell_style),
                Paragraph(row["status"], ParagraphStyle(f"WK{len(week_data)}", parent=_status_word_style, textColor=word_color)),
                Paragraph(_short_date(row["start_date"]), _cell_style),
                Paragraph(_short_date(row["end_date"]), _cell_style),
                Paragraph(_pct_str(row["pct_complete"]), _cell_style),
                Paragraph(_duration_str(row["duration_days"]), _cell_style),
            ])
        week_table = Table(
            week_data,
            colWidths=[2.6 * inch, 0.6 * inch, 0.55 * inch, 0.55 * inch, 0.5 * inch, 0.5 * inch],
            repeatRows=1,
        )
        week_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E2761")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F7F8FC"), colors.white]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(week_table)
        story.append(Spacer(1, 6))

    story.append(Paragraph("Why This Status?", _h_style))
    story.append(Paragraph(_esc(report["explanation"]["narrative"]), _body_style))

    story.append(Paragraph("What We Looked At", _h_style))
    story.append(Paragraph(
        "Supporting detail beyond the core schedule view above — these six areas also factor into "
        "this week's status:",
        _small_style,
    ))
    table_data = [[
        Paragraph("Area", _cell_head_style),
        Paragraph("Status", _cell_head_style),
        Paragraph("What we found", _cell_head_style),
    ]]
    for name, result in report["signals"].items():
        area = AREA_LABELS.get(name, name.replace("_", " ").title())
        if result is None:
            table_data.append([
                Paragraph(area, _cell_style),
                Paragraph("N/A", ParagraphStyle("NAX", parent=_status_word_style, textColor=colors.HexColor("#808080"))),
                Paragraph("Not enough data for this project — this area was left out of the score.", _cell_style),
            ])
        else:
            word, color = _status_word(result["score"])
            phrase = plain_english_phrase(name, (result["score"], result["raw_metric"]))
            phrase = phrase[0].upper() + phrase[1:]
            phrase = _esc(phrase)
            table_data.append([
                Paragraph(area, _cell_style),
                Paragraph(word, ParagraphStyle(f"SW{name}", parent=_status_word_style, textColor=color)),
                Paragraph(phrase, _cell_style),
            ])
    table = Table(table_data, colWidths=[1.3 * inch, 0.7 * inch, 4.3 * inch], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E2761")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F7F8FC"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)

    risk_rows = report.get("top_risk_rows") or []
    if risk_rows:
        story.append(Paragraph("Which Tasks Are Driving This", _h_style))
        story.append(Paragraph(
            "The signals above are a rollup across every task. These are the individual tasks "
            "behind them, worth a PM's attention first:",
            _small_style,
        ))
        drill_data = [[
            Paragraph("Task", _cell_head_style),
            Paragraph("Status", _cell_head_style),
            Paragraph("Why", _cell_head_style),
        ]]
        for row in risk_rows:
            word_color = RAG_COLORS.get(row["status"], RAG_COLORS["Insufficient Data"])
            drill_data.append([
                Paragraph(_esc(row["task_name"]) or "(unnamed task)", _cell_style),
                Paragraph(row["status"], ParagraphStyle(f"DR{len(drill_data)}", parent=_status_word_style, textColor=word_color)),
                Paragraph(_esc(row["reason"]), _cell_style),
            ])
        drill_table = Table(drill_data, colWidths=[2.3 * inch, 0.7 * inch, 3.3 * inch], repeatRows=1)
        drill_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E2761")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F7F8FC"), colors.white]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(drill_table)

    caveats = []
    if not report["has_budget_data"]:
        caveats.append("This project's source file has no budget/cost tracking at all, so Budget isn't scored — its weight was shared across the other areas instead.")
    if not report["has_sentiment_data"]:
        caveats.append("No team comments were logged for this project, so Team Feedback isn't scored — its weight was shared across the other areas instead.")
    if caveats:
        story.append(Paragraph("Data Limitations", _h_style))
        for c in caveats:
            story.append(Paragraph(f"&bull; {c}", _body_style))

    counts = report["counts"]
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"This report is based on {counts['tasks']} tasks and {counts['milestones']} milestones "
        f"tracked in the project plan.",
        _small_style,
    ))

    doc.build(story)
