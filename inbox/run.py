"""Run the inbox pipeline against the real Gmail inbox and Google Sheet.

    python -m inbox.run --once            # one pass: fetch, process, follow-up check
    python -m inbox.run                   # keep running: poll every 2 minutes, follow-ups hourly

Needs: the Google login (python -m inbox.google_setup login), SHEET_ID and INBOX_TEST_ACCOUNT in
.env. Without ANTHROPIC_API_KEY, keyword rules and templates are used.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .alerts import LogNotifier, SlackNotifier
from .classifier import ClaudeClassifier, KeywordClassifier
from .config import Settings
from .drafts import ClaudeDrafter, TemplateDrafter
from .google_api import DEFAULT_QUERY, GmailMailbox, GoogleSheet, load_credentials, services
from .google_setup import require_test_account
from .rules import load_rules
from .service import Pipeline
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("inbox")


def build() -> Pipeline:
    settings = Settings.from_env()
    settings.validate()
    sheet_id = os.getenv("SHEET_ID")
    if not sheet_id:
        sys.exit("Set SHEET_ID (python -m inbox.google_setup create-sheet).")
    gmail, sheets = services(load_credentials())
    require_test_account(gmail)
    rules = load_rules(ROOT / settings.rules_path)
    if settings.anthropic_api_key:
        classifier = ClaudeClassifier(settings.anthropic_api_key, settings.model, settings.effort)
        drafter = ClaudeDrafter(settings.anthropic_api_key, settings.model, settings.effort)
    else:
        classifier, drafter = KeywordClassifier(), TemplateDrafter()
    notifier = SlackNotifier(settings.alert_webhook_url) if settings.alert_webhook_url else LogNotifier()
    query = os.getenv("INBOX_QUERY", "").strip() or DEFAULT_QUERY
    log.info("reading Gmail messages matching: %s", query)
    return Pipeline(Store(str(ROOT / settings.database_path)), GmailMailbox(gmail, query),
                    GoogleSheet(sheets, sheet_id), classifier, rules, drafter, notifier, settings.public_url)


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true")
    p.add_argument("--poll-seconds", type=int, default=120)
    p.add_argument("--follow-up-seconds", type=int, default=3600)
    args = p.parse_args()

    pipeline = build()
    resumed = pipeline.resume_pending()
    if resumed:
        log.info("resumed %s unfinished emails", len(resumed))
    last_follow_up = 0.0
    while True:
        try:
            handled = pipeline.poll()
            if handled:
                log.info("handled %s new emails", len(handled))
            if time.time() - last_follow_up >= args.follow_up_seconds or args.once:
                log.info("follow-up: %s", pipeline.follow_up())
                last_follow_up = time.time()
        except Exception:  # e.g. Gmail unreachable: log it and try again next round
            log.exception("poll failed; will retry")
        if args.once:
            return
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
