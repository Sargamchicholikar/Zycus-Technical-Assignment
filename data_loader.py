"""Loads a project plan workbook (S2P-style or Plan-B-style) into one
consistent internal shape, so rag_engine.py never needs to know which
source schema a project came from.

Real-file quirks handled here (see README for details):
  - S2P has a `Level` column; Plan B does not and uses `Ancestors` instead.
    Both are integer hierarchy-depth markers in practice (0 = project
    rollup, 1 = phase/milestone, 2+ = tasks), so we fall back to whichever
    is present and expose it uniformly as `hierarchy_depth`.
  - "Leaf" tasks (no children) are detected structurally: a row has
    children if the very next row in sheet order is one level deeper.
    This assumes the sheet is in DFS/outline order, which both files are.
  - The Comments sheet has no real header row; entries are `"Row N"` in
    column A followed by text/author/timestamp, with blank spacer rows
    between entries. `Row N` is the workbook row number (row 2 = first
    data row), so the joined task index is `N - 2`.
  - Neither file has a budget/cost column at all, so `has_budget_data`
    is always False and rag_engine drops that signal entirely.
  - There is no dedicated "blocker" tracker in either file. We use the
    `At Risk?` flag on a row, while its Status is not Completed/Not
    Applicable, as the open-blocker proxy. This is a documented
    assumption, not something the source data labels explicitly.
  - `Total Float` (schedule slack, in days) is captured per task and is
    essentially fully populated in both files (99.8%/100%) — used by
    rag_engine's critical-path health signal. The `Critical?` flag is
    also captured but intentionally unused for scoring: it's a boolean
    derived from this same float value, so reading the float directly
    is more precise than trusting a pre-derived proxy.
"""

import re
from datetime import datetime
from pathlib import Path

import pandas as pd

KEY_COMPLETENESS_FIELDS = ["start_date", "end_date", "pct_complete", "status", "owner"]


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={c: str(c).strip() for c in df.columns})
    df = df.rename(columns={"Critical ?": "Critical?"})
    return df


def _to_bool_flag(val) -> bool:
    if pd.isna(val):
        return False
    return str(val).strip().lower() in ("1", "1.0", "true", "yes")


def _parse_variance_days(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    match = re.match(r"^\s*(-?\d+)\s*d?\s*$", str(val))
    return int(match.group(1)) if match else None


def _parse_duration_days(val):
    """Duration is stored as free text like '80d' or '1d', or the bare
    string '0' (no 'd' suffix) for a same-day task — never a real
    numeric Excel cell. Returns None if unparseable rather than guessing."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    match = re.match(r"^\s*(\d+)\s*d?\s*$", str(val).strip())
    return int(match.group(1)) if match else None


def _clean_scalar(val):
    if val is None:
        return None
    # pd.isna() (not the narrower isinstance(val, float) check) is what's
    # needed here: pandas represents a missing datetime cell as NaT, not
    # float('nan'), and NaT.date() silently returns NaT itself rather than
    # raising — so a stray NaT start/end date would slip through every
    # isinstance(..., float) guard uncaught and only blow up later, deep
    # in a date-arithmetic call far from where the bad value entered.
    if pd.isna(val):
        return None
    if val == "#UNPARSEABLE":
        return None
    return val


def _load_main_sheet(xl: pd.ExcelFile) -> tuple[pd.DataFrame, str]:
    main_sheet = next(s for s in xl.sheet_names if s not in ("Comments", "Summary"))
    df = _clean_columns(xl.parse(main_sheet))

    depth_field = "Level" if "Level" in df.columns else "Ancestors"
    df["hierarchy_depth"] = pd.to_numeric(df[depth_field], errors="coerce")

    depths = df["hierarchy_depth"].tolist()
    is_leaf = []
    for i, depth in enumerate(depths):
        if pd.isna(depth):
            is_leaf.append(False)
            continue
        nxt = depths[i + 1] if i + 1 < len(depths) else None
        has_child = nxt is not None and not pd.isna(nxt) and nxt > depth
        is_leaf.append(not has_child)
    df["is_leaf"] = is_leaf

    return df, depth_field


def _build_tasks(df: pd.DataFrame) -> list[dict]:
    tasks = []
    for idx, row in df.iterrows():
        variance = _parse_variance_days(row.get("Variance"))
        if variance is None and "Variance2" in df.columns:
            variance = _parse_variance_days(row.get("Variance2"))

        tasks.append({
            "row_index": idx,
            "task_name": _clean_scalar(row.get("Task Name")),
            "status": _clean_scalar(row.get("Status")),
            "hierarchy_depth": None if pd.isna(row.get("hierarchy_depth")) else int(row["hierarchy_depth"]),
            "is_leaf": bool(row.get("is_leaf")),
            "phase_milestone": _clean_scalar(row.get("Phase/Milestone")),
            "start_date": _clean_scalar(row.get("Start Date")),
            "end_date": _clean_scalar(row.get("End Date")),
            "pct_complete": _clean_scalar(row.get("% Complete")),
            "variance_days": variance,
            "owner": _clean_scalar(row.get("Owner")) or _clean_scalar(row.get("Assigned To")),
            "at_risk": _to_bool_flag(row.get("At Risk?")),
            "critical": _to_bool_flag(row.get("Critical?")),
            "on_hold": _to_bool_flag(row.get("On Hold?")),
            "not_applicable": _to_bool_flag(row.get("Not Applicable?")),
            "area": _clean_scalar(row.get("Area")),
            "total_float": _clean_scalar(row.get("Total Float")),
            "duration_days": _parse_duration_days(row.get("Duration")),
        })
    return tasks


_COMMENT_TIMESTAMP_FORMATS = (
    "%m/%d/%y %I:%M %p",   # e.g. "06/26/26 2:25 PM" — the confirmed real format
    "%m-%d-%y %I:%M %p",   # same, dash-separated
    "%m/%d/%Y %I:%M %p",   # 4-digit year variant, just in case
    "%m-%d-%Y %I:%M %p",
)


def _parse_comment_timestamp(raw):
    """Comment timestamps are stored as free text (e.g. "06/26/26 2:25 PM"),
    not real Excel date cells, so pandas never auto-converts them — this
    is the one place in the pipeline where we parse a date out of a
    string ourselves. Assumes MM/DD/YY per the confirmed real format.
    Any row that doesn't match cleanly returns None rather than raising,
    so one malformed timestamp can't take down the whole comments load —
    it just won't be usable for date-filtered (e.g. sentiment-over-time)
    features, and the original raw string is kept separately either way."""
    if raw is None:
        return None
    if isinstance(raw, (pd.Timestamp,)):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    for fmt in _COMMENT_TIMESTAMP_FORMATS:
        try:
            return pd.Timestamp(datetime.strptime(text, fmt))
        except ValueError:
            continue
    return None


def _load_comments(xl: pd.ExcelFile, df_main: pd.DataFrame) -> list[dict]:
    if "Comments" not in xl.sheet_names:
        return []
    raw = xl.parse("Comments", header=None)
    if raw.empty:
        return []

    comments = []
    for _, row in raw.iterrows():
        cell = row.get(0)
        if not isinstance(cell, str):
            continue
        match = re.match(r"^Row\s+(\d+)$", cell.strip())
        if not match:
            continue
        main_idx = int(match.group(1)) - 2
        task_name = None
        if 0 <= main_idx < len(df_main):
            task_name = _clean_scalar(df_main.iloc[main_idx].get("Task Name"))
        raw_timestamp = _clean_scalar(row.get(3))
        comments.append({
            "row_ref": cell.strip(),
            "main_row_index": main_idx,
            "task_name": task_name,
            "text": _clean_scalar(row.get(1)),
            "author": _clean_scalar(row.get(2)),
            "timestamp": raw_timestamp,
            "timestamp_parsed": _parse_comment_timestamp(raw_timestamp),
        })
    return comments


def _load_summary(xl: pd.ExcelFile) -> dict:
    if "Summary" not in xl.sheet_names:
        return {}
    raw = xl.parse("Summary", header=None)
    snapshot = {}
    for _, row in raw.iterrows():
        key, val = _clean_scalar(row.get(0)), _clean_scalar(row.get(1))
        if key and key != "Project Name":
            snapshot[key] = val
    return snapshot


def _completeness_pct(tasks: list[dict]) -> float:
    leaf_tasks = [t for t in tasks if t["is_leaf"]]
    if not leaf_tasks:
        return 0.0
    total_fields = len(leaf_tasks) * len(KEY_COMPLETENESS_FIELDS)
    filled = sum(
        1
        for t in leaf_tasks
        for field in KEY_COMPLETENESS_FIELDS
        if t.get(field) is not None
    )
    return round(100 * filled / total_fields, 1) if total_fields else 0.0


def load_project(path: str) -> dict:
    """Load one project workbook (S2P-style or Plan-B-style) into the
    normalized shape shared by rag_engine.py."""
    xl = pd.ExcelFile(path)
    df_main, depth_field = _load_main_sheet(xl)

    tasks = _build_tasks(df_main)
    milestones = [t for t in tasks if t["hierarchy_depth"] == 1]
    blockers = [
        t for t in tasks
        if t["at_risk"] and t["status"] not in ("Completed", "Not Applicable")
    ]
    comments = _load_comments(xl, df_main)
    summary_snapshot = _load_summary(xl)

    project_name = tasks[0]["task_name"] if tasks else Path(path).stem
    report_run_date = summary_snapshot.get("Today's Date") or pd.Timestamp.now().normalize()

    return {
        "project_name": project_name,
        "source_file": Path(path).name,
        "tasks": tasks,
        "milestones": milestones,
        "blockers": blockers,
        "comments": comments,
        "hierarchy_depth_field_used": depth_field,
        "completeness_pct": _completeness_pct(tasks),
        "summary_sheet_snapshot": summary_snapshot,
        "report_run_date": report_run_date,
        "has_budget_data": False,
        "has_sentiment_data": len(comments) > 0,
    }
