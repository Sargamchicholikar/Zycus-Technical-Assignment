"""Pure scoring functions for the Project Health RAG methodology.

No LLM/API calls live here on purpose (auditability requirement from the
build spec) — every number this module produces can be recomputed by hand
from the inputs alone. llm_explainer.py only ever explains what this
module already decided; it never changes it.

Signal table (weight, Green/Amber/Red thresholds) matches rag_methodology.pdf
exactly. See data_loader.py for how raw workbook rows become the `tasks`,
`milestones`, `blockers`, `comments` lists consumed here.

Six signals as of the latest revision: the original five plus critical-path
health, added after auditing every column in the real files and finding
`Total Float` (schedule slack) essentially fully populated (99.8%/100%) and
entirely unused. Weights were rebalanced to make room for it — see
rag_methodology.pdf for the full reasoning.
"""

from datetime import timedelta
from typing import Optional

GREEN, AMBER, RED = 100, 60, 20

WEIGHTS = {
    "schedule_slippage": 0.25,
    "budget_burn": 0.20,
    "milestone_health": 0.18,
    "blockers": 0.12,
    "stakeholder_sentiment": 0.10,
    "critical_path_health": 0.15,
}

# Sentiment heuristic keyword tiers. This is a deliberately simple, fully
# auditable stand-in for real NLP sentiment analysis: rag_engine.py cannot
# call an LLM, so free-text comments are scored by keyword match instead.
_ESCALATION_KEYWORDS = (
    "escalate", "escalation", "unacceptable", "dissatisf", "frustrat",
    "unresolved", "urgent",
)
_NEGATIVE_KEYWORDS = (
    "delay", "risk", "concern", "block", "impact", "pending", "issue",
    "gap", "miss", "slip",
)
_POSITIVE_KEYWORDS = (
    "on track", "resolved", "confirmed", "confident", "no issue",
    "aligned", "smooth", "covered", "as per schedule", "as scheduled",
)


def score_schedule_slippage(tasks: list[dict], report_date) -> tuple[int, str]:
    """25% weight. Overdue = leaf task, in scope (not Not-Applicable),
    end date before report_date, and not 100% complete."""
    in_scope = [t for t in tasks if t["is_leaf"] and not t["not_applicable"]]
    if not in_scope:
        return AMBER, "No leaf-level tasks found to evaluate schedule against"

    def is_overdue(t):
        if t["end_date"] is None:
            return False
        pct = t["pct_complete"]
        return t["end_date"] < report_date and (pct is None or pct < 1.0)

    overdue = [t for t in in_scope if is_overdue(t)]
    pct_overdue = 100 * len(overdue) / len(in_scope)

    if pct_overdue < 5:
        score = GREEN
    elif pct_overdue <= 20:
        score = AMBER
    else:
        score = RED

    raw = f"{len(overdue)}/{len(in_scope)} leaf tasks overdue ({pct_overdue:.1f}%)"
    return score, raw


def score_budget_burn(has_budget_data: bool, budget_variance_pct: Optional[float] = None) -> Optional[tuple[int, str]]:
    """20% weight. Neither sample source file has a budget/cost column at
    all — a structural absence from this data source, not a per-project
    gap — so this signal is dropped and its weight redistributed, exactly
    like stakeholder sentiment is dropped when a project has no Comments
    data. Forcing a synthetic Amber score here would just be assumption-
    stacking on top of data that was never captured in the first place."""
    if not has_budget_data or budget_variance_pct is None:
        return None

    if budget_variance_pct <= 10:
        score = GREEN
    elif budget_variance_pct <= 25:
        score = AMBER
    else:
        score = RED
    return score, f"Actual spend {budget_variance_pct:.1f}% over plan"


def score_milestone_health(milestones: list[dict], report_date) -> Optional[tuple[int, str]]:
    """18% weight. Milestones are hierarchy_depth==1 rows. Delayed = the
    baseline-vs-actual variance is negative, or it's still open past its
    end date."""
    if not milestones:
        return None

    def is_delayed(m):
        if m["variance_days"] is not None and m["variance_days"] < 0:
            return True
        if m["status"] != "Completed" and m["end_date"] is not None and m["end_date"] < report_date:
            return True
        return False

    delayed = [m for m in milestones if is_delayed(m)]
    count = len(delayed)

    if count == 0:
        score = GREEN
    elif count == 1:
        score = AMBER
    else:
        score = RED

    shown = delayed[:8]
    names = ", ".join(m["task_name"] for m in shown)
    if len(delayed) > len(shown):
        names += f", +{len(delayed) - len(shown)} more"
    suffix = f": {names}" if names else ""
    return score, f"{count}/{len(milestones)} milestones delayed or missed{suffix}"


def score_blockers(blockers: list[dict], report_date) -> tuple[int, str]:
    """12% weight. 'Blocker' proxy = a row flagged At Risk? that isn't
    Completed/Not Applicable (neither file has a dedicated blocker
    tracker). Age = days since the task's Start Date."""
    if not blockers:
        return GREEN, "No open blockers"

    def age_days(b):
        if b["start_date"] is None:
            return 0
        return max(0, (report_date - b["start_date"]).days)

    # Sorted oldest-first so the task name(s) surfaced below actually line
    # up with "oldest N days" — a status saying a blocker is old is only
    # useful if the reader also learns which task that is.
    aged = sorted(blockers, key=age_days, reverse=True)
    max_age = age_days(aged[0])
    count = len(blockers)

    if count >= 3 or max_age > 5:
        score = RED
    else:
        score = AMBER

    shown = [b["task_name"] for b in aged[:3] if b.get("task_name")]
    names = ", ".join(shown)
    if len(blockers) > len(shown):
        names += f", +{len(blockers) - len(shown)} more"
    suffix = f": {names}" if names else ""

    return score, f"{count} open blocker(s) flagged At-Risk, oldest {max_age} day(s){suffix}"


def score_critical_path_health(tasks: list[dict], report_date) -> Optional[tuple[int, str]]:
    """15% weight. Distinct from schedule_slippage: that signal treats
    every overdue leaf task the same regardless of schedule buffer, but
    an overdue task with 100 days of float barely threatens the project
    while an overdue task with zero float is directly delaying the end
    date. `Total Float` (schedule slack, in days) is used directly rather
    than the pre-derived `Critical?` boolean flag, since the raw number is
    more precise than trusting someone else's threshold on it.

    Returns None (dropped, weight redistributed) only if no task in this
    project has Total Float data at all — in the two real sample files
    this is essentially never the case (99.8%/100% populated), but a
    project from a different export might genuinely lack it."""
    eligible = [
        t for t in tasks
        if t["is_leaf"] and not t["not_applicable"] and t["total_float"] is not None
    ]
    if not eligible:
        return None

    critical_tasks = [t for t in eligible if t["total_float"] <= 0.5]
    if not critical_tasks:
        return GREEN, "0 critical-path (zero-float) tasks identified in this plan"

    def is_overdue(t):
        if t["end_date"] is None:
            return False
        pct = t["pct_complete"]
        return t["end_date"] < report_date and (pct is None or pct < 1.0)

    overdue_critical = [t for t in critical_tasks if is_overdue(t)]
    count = len(overdue_critical)

    if count == 0:
        score = GREEN
    elif count <= 2:
        score = AMBER
    else:
        score = RED

    return score, f"{count}/{len(critical_tasks)} critical-path (zero-float) tasks currently overdue"


def score_stakeholder_sentiment(comments: list[dict], has_sentiment_data: bool) -> Optional[tuple[int, str]]:
    """10% weight. Returns None when a project has no Comments-sheet
    data at all (e.g. Project Plan B) so the composite can drop and
    redistribute this signal's weight, per the methodology's explicit rule."""
    if not has_sentiment_data or not comments:
        return None

    def comment_score(c):
        text = (c.get("text") or "").lower()
        if any(k in text for k in _ESCALATION_KEYWORDS):
            return RED
        if any(k in text for k in _POSITIVE_KEYWORDS):
            return GREEN
        if any(k in text for k in _NEGATIVE_KEYWORDS):
            return AMBER
        return AMBER  # neutral status-update text: no confidence signal either way

    scores = [comment_score(c) for c in comments]
    avg = sum(scores) / len(scores)

    if avg >= 90:
        score = GREEN
    elif avg >= 40:
        score = AMBER
    else:
        score = RED

    tiers = {GREEN: 0, AMBER: 0, RED: 0}
    for s in scores:
        tiers[s] += 1
    raw = (
        f"{len(comments)} comment(s) analyzed "
        f"(confident: {tiers[GREEN]}, mixed/cautionary: {tiers[AMBER]}, escalation: {tiers[RED]})"
    )
    return score, raw


def sentiment_trend(comments: list[dict], has_sentiment_data: bool, as_of_dates: list) -> list[Optional[tuple[int, str]]]:
    """Stakeholder sentiment is the one signal where a genuine historical
    trend is possible without modeling anything: comment timestamps are
    real, parsed facts (see data_loader.py's _parse_comment_timestamp),
    so "what would this score have been as of date X" only requires
    filtering to comments that actually existed by then — no assumption
    about unknowable past values, unlike every other signal (which only
    have a single current-state measurement, not a time series).

    Comments with an unparseable timestamp are excluded from every cutoff
    rather than guessed at, since we can't know when they were posted.

    Returns one score_stakeholder_sentiment()-shaped result per as_of
    date, in the same order as as_of_dates."""
    results = []
    for cutoff in as_of_dates:
        available = [c for c in comments if c.get("timestamp_parsed") is not None and c["timestamp_parsed"] <= cutoff]
        results.append(score_stakeholder_sentiment(available, has_sentiment_data))
    return results


def _confidence_tag(completeness_pct: float) -> str:
    if completeness_pct > 90:
        return "High"
    if completeness_pct >= 70:
        return "Medium"
    return "Low"


def compute_composite(signal_results: dict[str, Optional[tuple[int, str]]], completeness_pct: float) -> dict:
    """Combines the six per-signal (score, raw_metric) results into one
    RAG status, applying the escalation override, then attaches the
    separate Data Confidence tag.

    `signal_results` keys must match WEIGHTS keys; a value of None means
    that signal could not be computed for this project (e.g. no
    sentiment source) and its weight is redistributed proportionally
    across the remaining computable signals.
    """
    computable = {k: v for k, v in signal_results.items() if v is not None}
    confidence = _confidence_tag(completeness_pct)

    # Original rule (5 signals) required at least 3 — a clear majority.
    # Scaled to 4-of-6 to preserve that same "need most signals, not just
    # a couple" intent now that a 6th signal exists.
    if len(computable) < 4:
        return {
            "status": "Insufficient Data",
            "composite_score": None,
            "confidence": confidence,
            "signals": signal_results,
            "weights_used": {},
            "escalation_applied": False,
        }

    total_weight = sum(WEIGHTS[k] for k in computable)
    weights_used = {k: WEIGHTS[k] / total_weight for k in computable}
    composite_score = sum(computable[k][0] * weights_used[k] for k in computable)

    if composite_score >= 80:
        status = "Green"
    elif composite_score >= 50:
        status = "Amber"
    else:
        status = "Red"

    red_signals = [k for k, v in computable.items() if v[0] == RED]
    escalation_applied = False
    if len(red_signals) >= 2:
        status = "Red"
        escalation_applied = True
    elif len(red_signals) == 1 and status == "Green":
        status = "Amber"
        escalation_applied = True

    return {
        "status": status,
        "composite_score": round(composite_score, 1),
        "confidence": confidence,
        "signals": signal_results,
        "weights_used": {k: round(v, 4) for k, v in weights_used.items()},
        "escalation_applied": escalation_applied,
        "red_signals": red_signals,
    }


def evaluate_project(project: dict) -> dict:
    """Runs all six signal functions against a data_loader.load_project()
    result and returns the composite RAG evaluation."""
    report_date = project["report_run_date"]

    signals = {
        "schedule_slippage": score_schedule_slippage(project["tasks"], report_date),
        "budget_burn": score_budget_burn(project["has_budget_data"]),
        "milestone_health": score_milestone_health(project["milestones"], report_date),
        "blockers": score_blockers(project["blockers"], report_date),
        "stakeholder_sentiment": score_stakeholder_sentiment(project["comments"], project["has_sentiment_data"]),
        "critical_path_health": score_critical_path_health(project["tasks"], report_date),
    }

    return compute_composite(signals, project["completeness_pct"])


def current_week_start(report_date):
    """Monday of the fixed Mon-Sun calendar week containing report_date
    — always a full 7-day span, never a rolling window whose width or
    alignment shifts depending on which weekday report_date itself falls
    on. Weekly reports are meant to be generated at Friday EOD (see
    scheduler.py's default schedule), reporting on the week through the
    upcoming Sunday. Single source of truth for "current week", used by
    classify_task's in-progress-this-week rule and by main.py to stamp
    the date range a given weekly report covers."""
    return report_date - timedelta(days=report_date.weekday())


def current_week_end(report_date):
    """Sunday of the same fixed Mon-Sun calendar week as
    current_week_start() — Monday + 6 days, always a full week."""
    return current_week_start(report_date) + timedelta(days=6)


def week_calendar_month(report_date) -> str:
    """Which calendar month a Mon-Sun week "belongs to", for deciding how
    many weekly reports fall in a given month (some months have 4, some
    5) and for grouping stored weekly reports by month for the monthly
    deck. A week only ever straddles a month boundary at its very start
    or end, never in the middle, so the week's Wednesday — the middle
    day of the Mon-Fri working week this whole project is built around —
    is used as the tiebreaker rather than an arbitrary day like the
    Monday, Friday, or report_date itself: whichever month contains that
    week's Wednesday is the month that whole week counts toward.
    Returns 'YYYY-MM'."""
    wednesday = current_week_start(report_date) + timedelta(days=2)
    return f"{wednesday.year:04d}-{wednesday.month:02d}"


def is_last_week_of_month(report_date) -> bool:
    """True only if the week containing report_date is the last week
    belonging to its calendar month — i.e. the very next week's
    Wednesday falls in a different month. This is what "the month has
    ended" means for deciding when a monthly deck should actually be
    built, rather than rebuilding it after every single weekly run."""
    this_month = week_calendar_month(report_date)
    next_week_start = current_week_start(report_date) + timedelta(days=7)
    return week_calendar_month(next_week_start) != this_month


def classify_task(task: dict, report_date) -> tuple[str, str]:
    """Per-row drill-down status — layered on top of the project-level
    composite above, not a replacement for it. Reuses the exact same
    underlying facts as the project signals (overdue, critical-path,
    at-risk), just applied to one row instead of aggregated across all
    of them, so "why is this row Red" always traces back to the same
    rules already documented in the methodology.

    One row-level check has no project-level equivalent: a task whose
    Start or End Date falls inside the current reporting week (the 7
    days ending on report_date) but that's still under a third done.
    `end_date < report_date` alone misses this — a task due today, or one
    that just started this week and hasn't moved, isn't "overdue" yet by
    that strict test, but it's already behind pace for the window it was
    scheduled into, which is exactly the "not started yet" gap this rule
    closes. pct_complete of None is treated as 0% (not started).
    Returns (status, reason)."""
    if task["status"] == "Completed":
        return "Green", "Completed"

    is_overdue = (
        task["end_date"] is not None
        and task["end_date"] < report_date
        and (task["pct_complete"] is None or task["pct_complete"] < 1.0)
    )
    is_critical = task["total_float"] is not None and task["total_float"] <= 0.5
    is_at_risk = task["at_risk"] and task["status"] not in ("Completed", "Not Applicable")

    ws, we = current_week_start(report_date), current_week_end(report_date)
    starts_this_week = task["start_date"] is not None and ws <= task["start_date"] <= we
    ends_this_week = task["end_date"] is not None and ws <= task["end_date"] <= we
    pct_now = task["pct_complete"] if task["pct_complete"] is not None else 0.0
    is_behind_this_week = (starts_this_week or ends_this_week) and pct_now < 0.33

    if is_at_risk:
        return "Red", "Flagged At-Risk and not yet resolved"
    if is_behind_this_week:
        return "Red", f"Should be underway this week (Start/End Date falls in this window), but only {pct_now * 100:.0f}% complete"
    if is_overdue and is_critical:
        return "Red", "Overdue and on the critical path (no schedule slack left)"
    if is_overdue:
        return "Amber", "Overdue, but still has schedule slack"
    if task["on_hold"]:
        return "Amber", "On hold"
    return "Green", "On track"


def classify_all_tasks(tasks: list[dict], report_date) -> list[dict]:
    """The complete per-row record: every in-scope leaf task and milestone
    gets its own RAG classification, Green included — this is the full
    drill-down data set. `top_risk_rows()` below is just a curated,
    worst-first view into this same list, for quick reading; this
    function is the authoritative "every task has a status" record."""
    candidates = [t for t in tasks if not t["not_applicable"] and (t["is_leaf"] or t["hierarchy_depth"] == 1)]

    classified = []
    for t in candidates:
        status, reason = classify_task(t, report_date)
        days_overdue = (report_date - t["end_date"]).days if t["end_date"] is not None and t["end_date"] < report_date else 0
        classified.append({
            "task_name": t["task_name"],
            "status": status,
            "reason": reason,
            "owner": t["owner"],
            "_sort_key": ({"Red": 0, "Amber": 1, "Green": 2}[status], -days_overdue),
        })

    classified.sort(key=lambda r: r["_sort_key"])
    for r in classified:
        del r["_sort_key"]
    return classified


def top_risk_rows(tasks: list[dict], report_date, limit: int = 10) -> list[dict]:
    """A curated, worst-first subset of classify_all_tasks() — Red first,
    then Amber, oldest-overdue-first — for a quick-glance summary rather
    than requiring a reader to scan the full per-task appendix."""
    all_rows = classify_all_tasks(tasks, report_date)
    return [r for r in all_rows if r["status"] != "Green"][:limit]


def this_week_task_digest(tasks: list[dict], report_date) -> list[dict]:
    """What's actually on the schedule this week: every in-scope task or
    milestone whose Start Date or End Date falls inside the current
    Mon-Sun calendar week — independent of RAG status, so a healthy
    on-schedule task shows up here too if it's genuinely active this
    week. Each row carries the same Red/Amber/Green classification as
    classify_task()."""
    ws, we = current_week_start(report_date), current_week_end(report_date)

    def in_week(d):
        return d is not None and ws <= d <= we

    candidates = [
        t for t in tasks
        if not t["not_applicable"] and (t["is_leaf"] or t["hierarchy_depth"] == 1)
        and (in_week(t["start_date"]) or in_week(t["end_date"]))
    ]

    rows = []
    for t in candidates:
        status, reason = classify_task(t, report_date)
        rows.append({
            "task_name": t["task_name"],
            "owner": t["owner"],
            "status": status,
            "reason": reason,
            "task_status_field": t["status"],
            "pct_complete": t["pct_complete"],
            "duration_days": t["duration_days"],
            "start_date": str(t["start_date"]) if t["start_date"] is not None else None,
            "end_date": str(t["end_date"]) if t["end_date"] is not None else None,
        })

    rows.sort(key=lambda r: {"Red": 0, "Amber": 1, "Green": 2}[r["status"]])
    return rows
