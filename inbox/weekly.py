"""Weekly report from the command line.

    python -m inbox.weekly preview                    # last week, sample jobs, nothing written anywhere
    python -m inbox.weekly preview --claude           # same, with Claude's summary (a cent or two)
    python -m inbox.weekly send --live                # real: Jobs tab + inbox log -> report tab + owner's inbox
    python -m inbox.weekly send --live --week 2026-10-26

A week is reported once: running "send" again for the same week only finishes missing steps.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .adapters import FakeSheet
from .report import REPORT_COLUMNS, ClaudeSummary, FakeJobs, render, week_start
from .reporting import FakeSink, Reporter
from .rules import load_rules
from .sample_jobs import sample_jobs
from .store import Store

ROOT = Path(__file__).resolve().parent.parent


def live_reporter(rules, summary):
    from .google_api import GmailMailbox, GmailReportSink, GoogleJobs, GoogleSheet, load_credentials, services
    from .google_setup import require_test_account
    sheet_id = os.getenv("SHEET_ID") or sys.exit("Set SHEET_ID first (python -m inbox.google_setup create-sheet).")
    gmail, sheets = services(load_credentials())
    require_test_account(gmail)
    report_tab = GoogleSheet(sheets, sheet_id, "Weekly report", REPORT_COLUMNS).ensure_tab()
    return Reporter(Store(str(ROOT / os.getenv("DATABASE_PATH", "inbox.db"))), GoogleJobs(sheets, sheet_id),
                    report_tab, GmailReportSink(GmailMailbox(gmail)), rules, summary)


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["preview", "send"])
    p.add_argument("--week", help="Monday the week starts (default: last full week)")
    p.add_argument("--live", action="store_true", help="use Google Sheets and Gmail")
    p.add_argument("--claude", action="store_true", help="write the summary with Claude")
    args = p.parse_args()

    rules = load_rules(ROOT / "routing.yaml")
    today = dt.datetime.now(ZoneInfo(rules.timezone)).date()
    week = dt.date.fromisoformat(args.week) if args.week else week_start(today) - dt.timedelta(days=7)
    if week.weekday() != 0:
        sys.exit("--week must be a Monday")
    summary = None
    if args.claude or (args.live and os.getenv("ANTHROPIC_API_KEY")):
        summary = ClaudeSummary(os.getenv("ANTHROPIC_API_KEY"), os.getenv("INBOX_MODEL", "claude-opus-5-5"))

    if args.live:
        reporter = live_reporter(rules, summary)
    else:
        tmp = tempfile.mkdtemp()
        reporter = Reporter(Store(f"{tmp}/r.db"), FakeJobs(sample_jobs(today)), FakeSheet(), FakeSink(), rules, summary)

    if args.command == "preview":
        built = reporter.build(week, today)
        subject, text, _ = render(built["metrics"], built["issues"], built["summary"], built["summary_note"],
                                  rules.business)
        print(f"Subject: {subject}\n\n{text}")
    else:
        if not args.live:
            sys.exit("send needs --live (use preview to try it without Google)")
        r = reporter.run(week, today)
        print(f"Week {r['week_start']}: sheet row {r['sheet_ref']}, delivered {r['message_id']}, "
              f"summary {'checked' if r['summary_ok'] else 'replaced'}" + (f", error: {r['error']}" if r["error"] else ""))


if __name__ == "__main__":
    main()
