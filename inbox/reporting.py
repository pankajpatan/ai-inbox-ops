"""Build and deliver the weekly report once per week, finishing whatever a failed run left out."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Protocol
from zoneinfo import ZoneInfo

from .adapters import LogSheet
from .alerts import Notifier
from .report import (REPORT_COLUMNS, ClaudeSummary, JobsSource, check_numbers, compute, find_issues, render,
                     sheet_row, template_summary)
from .rules import Rules
from .store import Store

log = logging.getLogger("inbox")


class ReportSink(Protocol):
    name: str

    def deliver(self, to: str, subject: str, text: str, html: str) -> str:
        """Put the report where the owner reads it. Returns an ID."""


@dataclass
class FakeSink:
    delivered: list[dict] = field(default_factory=list)
    fail: bool = False
    name: str = "fake"

    def deliver(self, to: str, subject: str, text: str, html: str) -> str:
        if self.fail:
            raise RuntimeError("fake delivery failure")
        self.delivered.append({"to": to, "subject": subject, "text": text, "html": html})
        return f"report-{len(self.delivered)}"


class Reporter:
    def __init__(self, store: Store, jobs: JobsSource, sheet: LogSheet, sink: ReportSink, rules: Rules,
                 summary: ClaudeSummary | None = None, notifier: Notifier | None = None):
        self.store, self.jobs, self.sheet, self.sink, self.rules = store, jobs, sheet, sink, rules
        self.summary, self.notifier = summary, notifier
        self.tz = ZoneInfo(rules.timezone)

    def build(self, week: dt.date, today: dt.date | None = None) -> dict:
        """Compute the report for the week starting `week` (a Monday). Does not deliver."""
        today = today or dt.datetime.now(self.tz).date()
        metrics = compute(week, self.jobs.jobs(), self.store.all_emails_for_report(), self.tz, today)
        issues = find_issues(metrics, self.rules)
        text, ok, note = template_summary(metrics, issues), True, None
        if self.summary is not None:
            try:
                written = self.summary.write(metrics, issues, self.rules)
                bad = check_numbers(written, metrics, issues)
                if bad:
                    note = (f"the AI summary was replaced because it contained numbers not in the data: "
                            f"{', '.join(bad)}")
                    ok = False
                else:
                    text = written
            except Exception as exc:  # the table is what matters; never fail the report over the summary
                log.warning("summary failed: %s", exc)
                note = "the AI summary could not be written this week; the figures below are complete"
        return {"metrics": metrics, "issues": issues, "summary": text, "summary_ok": ok, "summary_note": note}

    def run(self, week: dt.date, today: dt.date | None = None) -> dict:
        """Build (once), then add the sheet row and deliver, each only if not done before."""
        key = week.isoformat()
        saved = self.store.report(key)
        if saved is None or saved.get("metrics") is None:
            built = self.build(week, today)
            self.store.save_report(key, **built)
            saved = self.store.report(key)
        try:
            if not saved.get("sheet_ref"):
                ref = self.sheet.append(sheet_row(saved["metrics"], saved["issues"], bool(saved["summary_ok"])))
                self.store.save_report(key, sheet_ref=ref)
            if not saved.get("message_id"):
                subject, text, html = render(saved["metrics"], saved["issues"], saved["summary"],
                                             saved["summary_note"], self.rules.business)
                mid = self.sink.deliver(self.rules.report_recipient(), subject, text, html)
                self.store.save_report(key, message_id=mid, error=None)
        except Exception as exc:
            self.store.save_report(key, error=f"{type(exc).__name__}: {exc}"[:500])
            log.exception("weekly report delivery failed week=%s", key)
            if self.notifier:
                try:
                    self.notifier.notify(f"Weekly report for {key} could not be delivered: {exc}"[:300])
                except Exception:
                    log.exception("alert failed")
            return self.store.report(key)
        if self.notifier and saved["issues"] and not saved.get("message_id"):
            try:
                self.notifier.notify(f"Weekly report {key}: " + " ".join(saved["issues"])[:400])
            except Exception:
                log.exception("alert failed")
        return self.store.report(key)


__all__ = ["Reporter", "FakeSink", "ReportSink", "REPORT_COLUMNS"]
