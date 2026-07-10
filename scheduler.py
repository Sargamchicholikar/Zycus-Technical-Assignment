"""Bonus: runs main.py (which writes weekly reports and builds both the
portfolio deck and the per-project client decks in one pass) on a weekly
schedule.

Usage:
    python scheduler.py            # runs weekly, defaults to every Friday 18:00 (EOD)
    python scheduler.py --now      # also runs once immediately, then schedules

Defaults to Friday EOD because rag_engine.current_week_start()/
current_week_end() define "this week" as the Mon-Fri business week —
a report is meant to summarize a week that's actually finished, not
one that's only just started.

Day/time are configurable, not hardcoded:
    SCHEDULE_DAY=wednesday SCHEDULE_TIME=06:30 python scheduler.py

Cron equivalent (if you'd rather not keep a process running):
    0 18 * * 5 cd /path/to/project && /path/to/python main.py >> scheduler.log 2>&1
"""

import os
import sys
import time

import schedule

import main as report_main

WEEKLY_DAY = os.environ.get("SCHEDULE_DAY", "friday").lower()
WEEKLY_TIME = os.environ.get("SCHEDULE_TIME", "18:00")


def run_weekly_job():
    print(f"[scheduler] starting weekly run at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        report_main.main()
    except Exception as exc:
        print(f"[scheduler] weekly run failed: {exc}", file=sys.stderr)
    else:
        print("[scheduler] weekly run complete (reports + portfolio deck + client decks)")


def main():
    if "--now" in sys.argv:
        run_weekly_job()

    getattr(schedule.every(), WEEKLY_DAY).at(WEEKLY_TIME).do(run_weekly_job)
    print(f"[scheduler] scheduled weekly run: every {WEEKLY_DAY} at {WEEKLY_TIME}. Waiting...")

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
