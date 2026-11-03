"""The inbox pipeline.

poll():       fetch new mail and process each message
process(id):  received --classify--> classified --act--> acted --finish--> done
follow_up():  mark logged emails replied or overdue, alerting once when overdue

A failed step leaves the email in needs_attention with the step to resume from. Each action
(labels, draft, sheet row, alert) is saved as it happens, so a retry never repeats one. The
"processed" mark in the mailbox comes last, so a crash leaves the message safe to pick up again.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Callable
from zoneinfo import ZoneInfo

from .adapters import LogSheet, Mailbox
from .alerts import Notifier, attention_text, overdue_text, urgent_text
from .checks import check
from .classifier import Classifier
from .drafts import Drafter, check_draft
from .models import Classification, Email
from .rules import Rules
from .store import Store

log = logging.getLogger("inbox")
LABELS = {"new_job": "New job", "quote_request": "Quote", "complaint": "Complaint", "invoice_payment": "Billing",
          "supplier": "Supplier", "spam_marketing": "Marketing", "other": "Other"}


def labels_for(category: str, urgency: str) -> list[str]:
    labels = [f"AI/{LABELS[category]}"]
    if urgency == "now":
        labels.append("AI/Urgent")
    return labels


class Pipeline:
    def __init__(self, store: Store, mailbox: Mailbox, sheet: LogSheet, classifier: Classifier, rules: Rules,
                 drafter: Drafter | None = None, notifier: Notifier | None = None,
                 base_url: str = "http://127.0.0.1:8030", clock: Callable[[], float] | None = None):
        import time
        self.store, self.mailbox, self.sheet = store, mailbox, sheet
        self.classifier, self.rules, self.drafter, self.notifier = classifier, rules, drafter, notifier
        self.base_url = base_url.rstrip("/")
        self.clock = clock or time.time
        self.tz = ZoneInfo(rules.timezone)
        self._locks: dict[int, threading.Lock] = {}
        self._guard = threading.Lock()

    # ---------- helpers ----------

    def when(self, ts: float | None) -> str:
        if ts is None:
            return ""
        return dt.datetime.fromtimestamp(ts, self.tz).strftime("%a %d %b %H:%M")

    def _email(self, row: dict) -> Email:
        return Email(message_id=row["message_id"], thread_id=row["thread_id"], from_name=row["from_name"] or "",
                     from_email=row["from_email"], subject=row["subject"] or "", body=row["body"] or "",
                     received_at=row["received_at"], link=row["link"])

    def _alert(self, email_id: int, text: str, kind: str) -> bool:
        if self.notifier is None:
            return False
        try:
            ok = self.notifier.notify(text)
        except Exception:
            log.exception("alert failed email_id=%s", email_id)
            ok = False
        self.store.log(email_id, f"{kind}_alert_sent" if ok else f"{kind}_alert_failed", self.notifier.name)
        return ok

    def _fail(self, email_id: int, step: str, resume: str, exc: Exception) -> bool:
        log.exception("%s failed email_id=%s", step, email_id)
        self.store.transition(email_id, "needs_attention", {"step": step},
                              error=f"{step}: {type(exc).__name__}: {exc}"[:500], resume_state=resume)
        row = self.store.get(email_id)
        self._alert(email_id, attention_text(row, self.base_url), "attention")
        return False

    # ---------- entry points ----------

    def poll(self) -> list[int]:
        """Fetch new mail and process it. Returns the IDs handled in this run."""
        handled = []
        for e in self.mailbox.fetch_new():
            email_id, created = self.store.add_email(e)
            row = self.store.get(email_id)
            if not created and row["state"] == "done":
                # finished before, but the mailbox mark was lost (crash): mark it now
                self.mailbox.mark_processed(e.message_id)
                continue
            self.process(email_id)
            handled.append(email_id)
        return handled

    def process(self, email_id: int) -> None:
        with self._guard:
            lock = self._locks.setdefault(email_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return
        try:
            steps = {"received": self.classify, "classified": self.act, "acted": self.finish}
            while True:
                row = self.store.get(email_id)
                step = steps.get(row["state"]) if row else None
                if step is None or not step(email_id):
                    return
        finally:
            lock.release()

    def retry(self, email_id: int) -> bool:
        row = self.store.get(email_id)
        if row is None or row["state"] != "needs_attention":
            return False
        self.store.transition(email_id, row.get("resume_state") or "received", "manual retry", error=None)
        self.process(email_id)
        return True

    def resume_pending(self) -> list[int]:
        """After a restart, finish emails that were mid-pipeline."""
        pending = sorted(r["id"] for s in ("received", "classified", "acted") for r in self.store.emails(s))
        for email_id in pending:
            self.store.log(email_id, "resumed_after_restart")
            self.process(email_id)
        return pending

    # ---------- steps ----------

    def classify(self, email_id: int) -> bool:
        row = self.store.get(email_id)
        self.store.bump_attempts(email_id)
        email = self._email(row)
        try:
            checked = check(self.classifier.classify(email, self.rules), email)
        except Exception as exc:
            return self._fail(email_id, "classify", "received", exc)
        c = checked.classification
        rule = self.rules.route(c.category, c.urgency)
        deadline = row["received_at"] + rule.reply_within_hours * 3600 if (
            rule.reply_within_hours is not None and c.needs_reply) else None
        detail = {"category": c.category, "urgency": c.urgency, "assignee": rule.assign}
        if checked.corrections:
            detail["corrections"] = checked.corrections
        self.store.transition(
            email_id, "classified", detail,
            category=c.category, urgency=c.urgency, assignee=rule.assign, deadline=deadline,
            follow_status="open" if deadline else "none", classification=c.model_dump(),
            classifier=self.classifier.name, error=None, resume_state=None,
            actions={"labels": False, "draft_id": None, "sheet_ref": None, "alerted": False},
        )
        log.info("classified email_id=%s %s/%s -> %s", email_id, c.category, c.urgency, rule.assign)
        return True

    def act(self, email_id: int) -> bool:
        row = self.store.get(email_id)
        email = self._email(row)
        c = Classification.model_validate(row["classification"])
        rule = self.rules.route(c.category, c.urgency)
        actions = dict(row["actions"])
        person = self.rules.people.get(rule.assign) if rule.assign else None

        def save() -> None:
            self.store.update(email_id, actions=actions)

        try:
            if not actions["labels"]:
                self.mailbox.add_labels(email.message_id, labels_for(c.category, c.urgency))
                actions["labels"] = True
                save()
            wants_draft = (self.drafter is not None and c.needs_reply and rule.assign is not None
                           and c.category in self.rules.reply.draft_for)
            if wants_draft and not actions["draft_id"]:
                draft = check_draft(self.drafter.draft(email, c, self.rules), email, self.rules)
                actions["draft_id"] = self.mailbox.create_draft(email, draft.subject, draft.body)
                self.store.update(email_id, draft=draft.model_dump())
                save()
            if not actions["sheet_ref"]:
                draft = self.store.get(email_id).get("draft") or {}
                actions["sheet_ref"] = self.sheet.append({
                    "received": self.when(row["received_at"]),
                    "from": f"{email.from_name} <{email.from_email}>".strip(),
                    "subject": email.subject,
                    "category": LABELS[c.category],
                    "urgency": c.urgency,
                    "assigned_to": person.name if person else "",
                    "reply_by": self.when(row["deadline"]),
                    "summary": c.summary,
                    "phone": c.extracted.phone or "",
                    "draft": ("saved" + (" (check: " + "; ".join(draft["warnings"]) + ")" if draft.get("warnings")
                                         else "")) if actions["draft_id"] else "",
                    "status": "open" if row["deadline"] else "no reply needed",
                    "link": email.link or "",
                    "received_iso": dt.datetime.fromtimestamp(row["received_at"], self.tz).isoformat(timespec="minutes"),
                })
                save()
        except Exception as exc:
            return self._fail(email_id, "act", "classified", exc)

        if rule.alert and not actions["alerted"]:
            alert_row = {**row, "deadline_text": self.when(row["deadline"])}
            actions["alerted"] = self._alert(email_id, urgent_text(alert_row, person.name if person else None,
                                                                   self.base_url), "urgent")
            save()  # a failed alert is recorded, never blocks the email
        self.store.transition(email_id, "acted", {k: v for k, v in actions.items() if v})
        return True

    def finish(self, email_id: int) -> bool:
        row = self.store.get(email_id)
        try:
            self.mailbox.mark_processed(row["message_id"])
        except Exception as exc:
            return self._fail(email_id, "finish", "acted", exc)
        self.store.transition(email_id, "done")
        return False

    # ---------- follow-ups ----------

    def follow_up(self) -> dict[str, int]:
        """Check every open email: replied, still in time, or overdue (alerted once)."""
        now = self.clock()
        counts = {"replied": 0, "overdue": 0, "open": 0}
        for row in self.store.open_follow_ups():
            try:
                replied_at = self.mailbox.first_reply_after(row["thread_id"], row["received_at"])
                if replied_at is not None:
                    self.sheet.update_status(row["actions"]["sheet_ref"], "replied")
                    self.store.update(row["id"], follow_status="replied", replied_at=replied_at)
                    self.store.log(row["id"], "replied")
                    counts["replied"] += 1
                elif row["follow_status"] == "overdue":
                    counts["overdue"] += 1  # already flagged and alerted once
                elif row["deadline"] and now > row["deadline"]:
                    self.sheet.update_status(row["actions"]["sheet_ref"], "overdue")
                    self.store.update(row["id"], follow_status="overdue")
                    self.store.log(row["id"], "overdue")
                    person = self.rules.people.get(row["assignee"]) if row["assignee"] else None
                    self._alert(row["id"], overdue_text({**row, "deadline_text": self.when(row["deadline"])},
                                                        person.name if person else None), "overdue")
                    counts["overdue"] += 1
                else:
                    counts["open"] += 1
            except Exception:  # try again next run; one bad thread must not stop the others
                log.exception("follow-up check failed email_id=%s", row["id"])
        return counts
