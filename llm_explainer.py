"""Turns rag_engine.py's composite RAG output into a plain-English,
VP-facing explanation.

This module never decides the status — it only explains a decision
rag_engine.py already made. The prompt is explicit about that, and the
model is instructed to reference the specific signals that drove the
result rather than restate generic project-management language.
"""

import json
import re

import llm_client

SYSTEM_PROMPT = """You write weekly project-health explanations for VP-level stakeholders.

You are given a project's RAG status and the five signal scores that produced it via a fixed, \
already-finalized weighted formula. That status and those scores are final and authoritative — \
you must not change, second-guess, recompute, or hedge on them. Your only job is to explain in \
plain English why the project landed where it did, naming the specific signal(s) that drove the \
result (say "milestone slippage" or "open blockers", not internal field names like \
"milestone_health").

Write 3 to 5 sentences as a single plain-text paragraph: concise, no jargon, no bullet points, \
suitable for a VP with no patience for detail. Then, on its own lines, output a fenced ```json \
code block containing exactly these keys: "status" (the RAG status you were given, verbatim), \
"top_driver" (the single signal that most influenced the result, in a few plain words), and \
"confidence" (the confidence tag you were given, verbatim). Output nothing after the code block."""

_STRICT_SUFFIX = """

IMPORTANT: Your previous response could not be parsed. You MUST follow the exact format: a plain \
paragraph first, then a single fenced ```json code block with only the keys status, top_driver, \
confidence. Do not add any other text, headers, or code blocks."""


def _build_user_prompt(project_name: str, composite: dict) -> str:
    lines = [
        f"Project: {project_name}",
        f"RAG status: {composite['status']}",
        f"Composite score: {composite.get('composite_score')}",
        f"Data confidence: {composite['confidence']}",
        f"Escalation override applied: {composite.get('escalation_applied', False)}",
        "",
        "Signal scores (100=Green, 60=Amber, 20=Red; None=not computable for this project):",
    ]
    for name, result in composite["signals"].items():
        if result is None:
            lines.append(f"- {name}: not computable (weight redistributed to other signals)")
        else:
            score, raw = result
            lines.append(f"- {name}: {score} ({raw})")
    return "\n".join(lines)


def _extract_json_block(text: str):
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        return None, text
    json_str = match.group(1)
    narrative = text[: match.start()].strip()
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return None, narrative
    return parsed, narrative


def _call_model(user_prompt: str, strict: bool = False) -> str:
    system = SYSTEM_PROMPT + (_STRICT_SUFFIX if strict else "")
    return llm_client.generate(system, user_prompt)


def _weakest_signal_name(composite: dict):
    computable = [(name, result) for name, result in composite["signals"].items() if result is not None]
    if not computable:
        return None
    name, _ = min(computable, key=lambda item: item[1][0])
    return name.replace("_", " ")


_SIGNAL_KEYWORDS = {
    "schedule_slippage": ("schedule", "slippage", "overdue"),
    "budget_burn": ("budget",),
    "milestone_health": ("milestone",),
    "blockers": ("blocker",),
    "stakeholder_sentiment": ("sentiment", "stakeholder"),
    "critical_path_health": ("critical path", "critical-path", "float", "buffer"),
}


def _covers_weakest_signals(text: str, composite: dict, n: int = 2) -> bool:
    """A weaker/smaller model's raw prose can be non-empty and still be a
    bad explanation — e.g. it discusses only the healthy signals and never
    mentions the ones that actually drove the status. This checks whether
    the text at least names the n lowest-scoring computable signals before
    trusting it over the fully deterministic narrative."""
    computable = [(name, result) for name, result in composite["signals"].items() if result is not None]
    if not computable:
        return True
    weakest = sorted(computable, key=lambda item: item[1][0])[:n]
    text_lower = text.lower()
    return all(
        any(kw in text_lower for kw in _SIGNAL_KEYWORDS.get(name, (name,)))
        for name, _ in weakest
    )


def plain_english_phrase(name: str, result: tuple) -> str:
    """Translates one signal's technical raw_metric string (built for the
    audit-trail table, full of task names/percentages/jargon like
    "zero-float") into a short, plain-English clause — used anywhere
    raw_metric would otherwise be shown verbatim to a non-technical
    audience. Score-aware: a Green result needs to read as reassuring,
    not just a de-jargonned version of the same cautionary phrasing used
    for Amber/Red, since this is called for every row of the PDF's
    breakdown table, not just the weak signals in the narrative below."""
    score, raw = result

    if name == "schedule_slippage":
        m = re.search(r"\(([\d.]+)%\)", raw)
        pct = m.group(1) if m else None
        if pct is None:
            return "a share of tasks are running behind schedule"
        if score == 100:
            return f"only about {pct}% of tasks are behind schedule, which is a healthy range"
        if score == 60:
            return f"about {pct}% of tasks are running behind schedule"
        return f"about {pct}% of tasks are significantly behind schedule"

    if name == "budget_burn":
        return "budget tracking isn't available for this project"

    if name == "milestone_health":
        m = re.match(r"(\d+)/(\d+) milestones delayed or missed(?::\s*(.*))?$", raw)
        if m:
            count, total, names_blob = m.group(1), m.group(2), m.group(3) or ""
            if count == "0":
                return f"all {total} milestones are on track"
            verb = "is" if count == "1" else "are"
            base = f"{count} of its {total} milestones {verb} delayed or missed"
            examples = [n.strip() for n in names_blob.split(",") if n.strip() and not n.strip().startswith("+")][:2]
            if examples:
                base += f", including {' and '.join(examples)}"
            return base
        return "milestones are behind schedule"

    if name == "blockers":
        if score == 100:
            return "there are no open blockers right now"
        m = re.search(r"(\d+) open blocker.*oldest (\d+) day\(s\)(?::\s*(.*))?$", raw)
        if m:
            count, age, names_blob = int(m.group(1)), m.group(2), (m.group(3) or "")
            names = [n.strip() for n in names_blob.split(",") if n.strip() and not n.strip().startswith("+")]
            if count == 1:
                if names:
                    return f"one blocker (\"{names[0]}\") has been open for {age} days"
                return f"one blocker has been open for {age} days"
            base = f"{count} blockers have been open, the oldest for {age} days"
            if names:
                base += f" — the oldest is \"{names[0]}\""
            return base
        return "there are open blockers"

    if name == "stakeholder_sentiment":
        m = re.search(r"confident: (\d+), mixed/cautionary: (\d+), escalation: (\d+)", raw)
        if m:
            confident, mixed, escalation = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if escalation > 0:
                return "stakeholder comments show real escalation or frustration"
            if mixed > confident:
                return "stakeholder comments read mostly cautious or mixed"
            return "stakeholder comments are largely confident"
        return "stakeholder sentiment is mixed"

    if name == "critical_path_health":
        m = re.match(r"(\d+)/(\d+) critical-path", raw)
        if m:
            count = int(m.group(1))
            if count == 0:
                return "no tasks on the tightest part of the schedule are currently late"
            subject = "one task with no schedule buffer is" if count == 1 else f"{count} tasks with no schedule buffer are"
            return f"{subject} already late, which directly threatens the project end date"
        return "some critical-path tasks need attention"

    return raw


def _deterministic_narrative(project_name: str, composite: dict) -> str:
    """Built with no LLM call at all, from data already computed by
    rag_engine.py — but written as an actual plain-English paragraph, not
    a concatenation of raw signal strings. Used when the model's response
    can't be trusted (unparseable, off-topic, or an implausibly long
    markdown dump) rather than as a rare last resort: with a small local
    model this is often the common path, so it has to read like a real
    explanation on its own, not just avoid crashing."""
    weak_names = [
        name for name, result in composite["signals"].items()
        if result is not None and result[0] < 100
    ]
    phrases = [plain_english_phrase(name, composite["signals"][name]) for name in weak_names[:3]]

    if not phrases:
        reason = "every computable signal is currently healthy"
    elif len(phrases) == 1:
        reason = phrases[0]
    elif len(phrases) == 2:
        reason = f"{phrases[0]}, and {phrases[1]}"
    else:
        reason = "; ".join(phrases[:-1]) + f"; and {phrases[-1]}"

    return (
        f"{project_name} is {composite['status']} this week. In plain terms: {reason}. "
        f"Data confidence for this read is {composite['confidence'].lower()}."
    )


def explain(project_name: str, composite: dict) -> dict:
    """Returns {status, top_driver, confidence, narrative}. If the model's
    response can't be parsed as the requested JSON+prose format even after
    one stricter retry — which happens more often with small local models
    than with a hosted API — this degrades gracefully instead of crashing:
    status/confidence are already known deterministically from rag_engine.py
    regardless of what the model echoes back, so a `format_fallback: True`
    flag is attached and the model's raw prose (or, if that's unusably
    empty, a fully deterministic sentence) is used as the narrative."""
    if composite["status"] == "Insufficient Data":
        return {
            "status": "Insufficient Data",
            "top_driver": None,
            "confidence": composite["confidence"],
            "narrative": (
                f"{project_name} cannot be assigned a RAG status this week: fewer than three of "
                "the five health signals could be computed from the available data. Address the "
                "missing inputs before the next reporting cycle rather than treating this as a "
                "de facto Red."
            ),
        }

    user_prompt = _build_user_prompt(project_name, composite)

    raw_text = _call_model(user_prompt, strict=False)
    parsed, narrative = _extract_json_block(raw_text)

    if parsed is None:
        raw_text = _call_model(user_prompt, strict=True)
        parsed, narrative = _extract_json_block(raw_text)

    if parsed is None:
        narrative = narrative.strip()
        # A weak local model's raw prose is only trusted over the fully
        # deterministic narrative if it's both substantively right (names
        # the signals that actually drove the status) and stylistically
        # close to the requested VP-brief length — not a multi-paragraph
        # markdown dump that happens to mention the right words somewhere.
        if not narrative or len(narrative) > 600 or not _covers_weakest_signals(narrative, composite):
            narrative = _deterministic_narrative(project_name, composite)
        return {
            "status": composite["status"],
            "top_driver": _weakest_signal_name(composite),
            "confidence": composite["confidence"],
            "narrative": narrative,
            "format_fallback": True,
        }

    return {
        "status": parsed.get("status", composite["status"]),
        "top_driver": parsed.get("top_driver"),
        "confidence": parsed.get("confidence", composite["confidence"]),
        "narrative": narrative,
    }
