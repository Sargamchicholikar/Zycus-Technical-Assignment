# Project Health Reporting Agent

Reads a project plan (Excel), computes a Red/Amber/Green status from six
independently-scored signals, explains it in plain English for a VP
audience, and degrades gracefully when the data is messy or incomplete.
Built against two real sample files: `data/S2P Project.xlsx` and
`data/Project Plan B.xlsx`.

**Runs entirely on a local LLM by default** — [LM Studio](https://lmstudio.ai)
serving `phi-3-mini-4k-instruct` on-device, no API key, no cloud
dependency. See [`rag_methodology.pdf`](rag_methodology.pdf) for the
finalized Phase 1 scoring methodology.

## Architecture

```
data_loader.py              -> normalizes either workbook schema into one shape
rag_engine.py                -> pure scoring functions (no LLM/API calls, auditable)
llm_client.py                 -> local LLM client (LM Studio's OpenAI-compatible API)
llm_explainer.py               -> turns a composite score into a VP-facing paragraph
pdf_report.py                  -> renders one project's report as a formatted PDF
main.py                        -> orchestrates loader -> engine -> explainer -> /outputs
scheduler.py                   -> runs main.py weekly (bonus)
presentation/generator.py      -> internal portfolio deck (all projects, cross-project trends)
presentation/client_deck.py    -> one client-safe deck per project
```

Nothing upstream of `llm_explainer.py` calls a model — the RAG status and
every score are 100% reproducible by hand from the source spreadsheet.
The LLM's only job is prose, never the number.

## How to run

```bash
pip install -r requirements.txt

# 1. Local LLM setup (default, no API key):
#    install LM Studio -> download phi-3-mini-4k-instruct -> load it -> start its local server
#    (GUI: Developer tab -> Start Server, or headless: `lms load phi-3-mini-4k-instruct && lms server start`)
#    verify: curl http://localhost:1234/v1/models

# 2. Run it:
python main.py                       # writes {project}_weekly_report.{json,pdf}; rebuilds both decks only if this week ends the calendar month
python presentation/generator.py     # rebuilds the portfolio deck on demand, any time
python presentation/client_deck.py   # rebuilds the client decks on demand, any time
python scheduler.py --now            # runs main.py once now, then weekly (default: Fridays 18:00)
```

Override the local server's URL/model/temperature if yours differ from
the defaults: `LMSTUDIO_BASE_URL`, `LMSTUDIO_MODEL`, `LLM_TEMPERATURE`.
Schedule day/time are configurable too: `SCHEDULE_DAY`/`SCHEDULE_TIME`.

## Key design decisions

- **Budget signal is dropped, not defaulted.** Neither file has a
  budget/cost column at all — a structural absence, not a gap — so the
  signal is dropped and its weight redistributed rather than faking a
  score.
- **Blockers are a documented proxy.** No file has a blocker tracker, so
  a row counts as one if `At Risk?` is set and Status isn't
  Completed/Not Applicable, aged from its Start Date.
- **Stakeholder sentiment is a keyword heuristic, not an LLM call** —
  `rag_engine.py` must stay LLM-free for auditability, so free-text
  Comments are scored against small keyword lists instead.
- **`Total Float` (schedule slack) became a 6th signal, critical-path
  health.** Found by auditing every unused column in both files —
  99.8–100% populated, genuinely meaningful, and completely unused
  otherwise. An overdue task with 100 days of float barely matters; one
  with zero float directly delays the project.
- **Per-task drill-down, not just a project color.** `classify_task()`
  independently classifies every task/milestone (Red/Amber/Green + why),
  reusing the same rules as the project-level signals. The full record
  lives in the JSON (`all_task_status`); the PDF only surfaces the
  worst-first top-10 ("Which Tasks Are Driving This") to stay readable.
- **Baseline Start/Finish and Variance were tried in the per-task logic,
  then deliberately removed.** They added an early-warning rule for
  milestones drifting off-plan, but weren't reliably explainable — traded
  for a simpler rule set built entirely on Status/Start/End/%
  Complete/Duration, the five fields that are actually populated
  consistently. `Variance` still feeds the project-level milestone
  signal, unaffected.
- **"This week" is a fixed Monday–Sunday calendar week**, not a rolling
  7-day window — the naive version silently produced a
  Friday-through-Thursday span depending on which weekday the report
  happened to run. Month boundaries for the "which weeks belong to this
  month" grouping are resolved by each week's Wednesday, to avoid
  splitting a straddling week across two months.
- **Two presentation decks, not one** — the brief asks for one
  cross-project trends deck *and* a deck safe to hand to "a client," but
  S2P and Plan B are two different, unrelated clients. `generator.py`
  builds the internal cross-project deck; `client_deck.py` builds one
  client-safe deck per project with zero cross-references.
- **Decks only rebuild when a month actually ends** (`is_last_week_of_month()`),
  not on every weekly run — otherwise a half-finished month kept getting
  a "final monthly" deck. Run the two `presentation/*.py` scripts directly
  to build one on demand regardless of month completion.
- **Real bug fixed:** task names containing `&` (66 across both files,
  e.g. "OTK to share D&B creds") corrupted ReportLab's PDF markup parser
  until every dynamic string was escaped (`pdf_report._esc()`).
- **Real bug fixed:** `_clean_scalar()` only checked for float NaN, missing
  `pd.NaT` (pandas's null for dates) — comparisons silently returned
  `False` instead of crashing, until date-arithmetic code called `.date()`
  on it. Fixed by checking `pd.isna()` directly.

## Known limitations (real, not hypothetical)

- No budget data in either source file; no dedicated blocker tracker in
  either file — both are stated plainly in every report, never papered
  over.
- Sentiment data exists for S2P only (Plan B's Comments sheet is empty).
- **Composite month-over-month trend is real once it exists, but needs
  real accumulated weekly runs to exist** — every signal except sentiment
  is a single current-state value per workbook, not a history, so a
  "2 weeks ago" composite can't be reconstructed without assuming a
  progress model. The Trend Analysis slide says so honestly and fills in
  on its own as `scheduler.py` keeps running. Stakeholder sentiment *is*
  real from day one, since comment timestamps are genuine historical facts.

## Outputs

- `outputs/{project}_weekly_report_{timestamp}.{pdf,json}` — the weekly
  deliverable: RAG status, plain-English explanation, a "This Week's
  Tasks" table, a signal breakdown, and the top-10 risk list. The JSON
  additionally carries the full per-task record.
- `outputs/portfolio_health_deck.pptx` — 7-slide monthly internal
  synthesis across all projects (the brief's "Final monthly presentation").
- `outputs/{project}_client_deck.pptx` — one client-safe monthly deck per
  project.
- `outputs_demo/` — a clearly-labeled illustrative run (see its own
  `README_DEMO.txt`) showing what a full month of accumulated weekly
  reports and a populated trend chart look like, since the real
  submission only has one real week of data so far.
