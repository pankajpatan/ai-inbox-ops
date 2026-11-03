"""Run the inbox pipeline on sample emails, with an in-memory mailbox and sheet.

    python -m inbox.demo                      # keyword rules, free
    python -m inbox.demo --claude             # Claude (uses ANTHROPIC_API_KEY, a few cents)
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

from .adapters import FakeMailbox, FakeSheet
from .alerts import LogNotifier
from .classifier import ClaudeClassifier, KeywordClassifier
from .drafts import ClaudeDrafter, TemplateDrafter
from .models import Email
from .rules import load_rules
from .service import Pipeline
from .store import Store

ROOT = Path(__file__).resolve().parent.parent


def load_fixture(path: Path, now: float) -> list[Email]:
    emails = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            hours = d.pop("hours_ago", 0)
            emails.append(Email(**d, received_at=now - hours * 3600))
    return emails


def main() -> None:
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--claude", action="store_true")
    p.add_argument("--model", default=os.getenv("INBOX_MODEL", "claude-opus-5-5"))
    p.add_argument("--fixture", default=str(ROOT / "fixtures" / "sample_inbox.jsonl"))
    args = p.parse_args()

    rules = load_rules(ROOT / "routing.yaml")
    now = time.time()
    mailbox, sheet, notifier = FakeMailbox(load_fixture(Path(args.fixture), now)), FakeSheet(), LogNotifier()
    if args.claude:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise SystemExit("Set ANTHROPIC_API_KEY in .env to use Claude.")
        classifier, drafter = ClaudeClassifier(key, args.model), ClaudeDrafter(key, args.model)
    else:
        classifier, drafter = KeywordClassifier(), TemplateDrafter()

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(str(Path(tmp) / "demo.db"))
        pipeline = Pipeline(store, mailbox, sheet, classifier, rules, drafter, notifier)
        pipeline.poll()
        counts = pipeline.follow_up()
        print(f"{'from':24} {'category':16} {'urgency':7} {'assigned to':16} {'reply by':16} draft")
        for row in sorted(store.emails(), key=lambda r: r["id"]):
            person = rules.people[row["assignee"]].name if row["assignee"] else "-"
            print(f"{(row['from_name'] or row['from_email'])[:23]:24} {row['category'] or '-':16} "
                  f"{row['urgency'] or '-':7} {person[:15]:16} {pipeline.when(row['deadline']) or '-':16} "
                  f"{'yes' if (row['actions'] or {}).get('draft_id') else '-'}")
        print(f"\nstates: {sorted({r['state'] for r in store.emails()})} · follow-up: {counts}")
        print(f"alerts sent: {len(notifier.sent)} · drafts saved: {len(mailbox.drafts)} · sheet rows: {len(sheet.rows)}")


if __name__ == "__main__":
    main()
