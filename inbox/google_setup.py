"""One-time Google setup for the demo.

    python -m inbox.google_setup login         # browser sign-in; saves google-credentials/token.json
    python -m inbox.google_setup whoami        # which Gmail account the token belongs to
    python -m inbox.google_setup create-sheet  # creates the "Inbox log" spreadsheet, saves SHEET_ID to .env
    python -m inbox.google_setup seed          # puts the fictional sample emails into the test inbox
    python -m inbox.google_setup seed-jobs     # writes about 9 weeks of fictional jobs to a "Jobs" tab
    python -m inbox.google_setup labels        # creates every AI/... label (the n8n version needs them)

Every command except login refuses to run unless the token belongs to INBOX_TEST_ACCOUNT (.env),
so a personal inbox is never touched by mistake. Nothing here sends email.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from email.utils import formatdate
from pathlib import Path

from dotenv import load_dotenv, set_key

from .google_api import JOB_COLUMNS, GmailMailbox, GoogleSheet, load_credentials, services

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def account(gmail) -> str:
    return gmail.users().getProfile(userId="me").execute()["emailAddress"]


def require_test_account(gmail) -> str:
    expected = os.getenv("INBOX_TEST_ACCOUNT", "").strip().lower()
    actual = account(gmail).lower()
    if not expected:
        sys.exit("Set INBOX_TEST_ACCOUNT in .env to the demo Gmail address first.")
    if actual != expected:
        sys.exit(f"Logged in as {actual}, but INBOX_TEST_ACCOUNT is {expected}. Refusing to continue.")
    return actual


def main() -> None:
    load_dotenv(ENV)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["login", "whoami", "create-sheet", "seed", "seed-jobs", "labels"])
    p.add_argument("--fixture", default=str(ROOT / "fixtures" / "sample_inbox.jsonl"))
    args = p.parse_args()

    if args.command == "login":
        creds = load_credentials(interactive=True)
        gmail, _ = services(creds)
        print(f"Logged in as {account(gmail)}. Token saved (git-ignored).")
        return

    gmail, sheets = services(load_credentials())
    who = require_test_account(gmail)
    if args.command == "whoami":
        print(who)
    elif args.command == "create-sheet":
        if os.getenv("SHEET_ID"):
            sys.exit(f"SHEET_ID is already set: https://docs.google.com/spreadsheets/d/{os.environ['SHEET_ID']}")
        sheet = GoogleSheet.create(sheets, "Harbor Home Services: inbox log (demo)")
        set_key(str(ENV), "SHEET_ID", sheet.id)
        print(f"Created {sheet.url}\nSaved SHEET_ID to .env")
    elif args.command == "seed":
        mailbox = GmailMailbox(gmail)
        now = time.time()
        count = 0
        for line in Path(args.fixture).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            when = formatdate(now - d.get("hours_ago", 0) * 3600, localtime=True)
            mailbox.insert_test_email(d["from_name"], d["from_email"], d["subject"], d["body"], when)
            count += 1
        print(f"Inserted {count} fictional emails into {who}'s inbox (nothing was sent).")
    elif args.command == "labels":
        from .google_api import DEMO_LABEL, PROCESSED_LABEL, REPORT_LABEL
        from .service import LABELS
        names = [f"AI/{v}" for v in LABELS.values()] + ["AI/Urgent", PROCESSED_LABEL, REPORT_LABEL, DEMO_LABEL]
        GmailMailbox(gmail)._label_ids(names)
        print(f"Labels ready: {', '.join(names)}")
        if os.getenv("SHEET_ID"):  # make sure the log's header has every column
            from .google_api import LOG_COLUMNS
            GoogleSheet(sheets, os.environ["SHEET_ID"])._write_header()
            print(f"Inbox log header: {len(LOG_COLUMNS)} columns")
    elif args.command == "seed-jobs":
        import datetime as dt
        from zoneinfo import ZoneInfo
        from .rules import load_rules
        from .sample_jobs import sample_jobs
        if not os.getenv("SHEET_ID"):
            sys.exit("Run create-sheet first.")
        tab = GoogleSheet(sheets, os.environ["SHEET_ID"], "Jobs", JOB_COLUMNS).ensure_tab()
        if tab.rows():
            sys.exit("The Jobs tab already has rows; clear it first if you want fresh sample data.")
        today = dt.datetime.now(ZoneInfo(load_rules(ROOT / "routing.yaml").timezone)).date()
        jobs = sample_jobs(today)
        tab.write_rows([{**j.model_dump(), "date": j.date.isoformat(),
                         "due_date": j.due_date.isoformat() if j.due_date else ""} for j in jobs])
        print(f"Wrote {len(jobs)} fictional jobs to the Jobs tab: {tab.url}")


if __name__ == "__main__":
    main()
