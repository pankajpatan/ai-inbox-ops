"""Where emails come from and where the log goes. Gmail and Google Sheets arrive in milestone 3;
these interfaces and the in-memory fakes are what the pipeline and tests use until then."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Protocol

from .models import Email


class AdapterError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class Mailbox(Protocol):
    name: str

    def fetch_new(self) -> list[Email]:
        """Messages not yet marked processed, oldest first."""

    def add_labels(self, message_id: str, labels: list[str]) -> None: ...

    def create_draft(self, email: Email, subject: str, body: str) -> str:
        """Save a reply draft in the email's thread. Returns the draft ID. Never sends."""

    def mark_processed(self, message_id: str) -> None: ...

    def first_reply_after(self, thread_id: str, since: float) -> float | None:
        """Time of the business's first reply in this thread after `since`, or None."""


class LogSheet(Protocol):
    name: str

    def append(self, row: dict) -> str:
        """Add a row. Returns a reference used to update it later."""

    def update_status(self, ref: str, status: str) -> None: ...


@dataclass
class FakeMailbox:
    """In-memory mailbox for the demo and tests."""

    emails: list[Email] = field(default_factory=list)
    processed: set[str] = field(default_factory=set)
    labels: dict[str, list[str]] = field(default_factory=dict)
    drafts: list[dict] = field(default_factory=list)
    replies: dict[str, float] = field(default_factory=dict)   # thread_id -> time we replied
    fail_on: str | None = None
    name: str = "fake"

    def _maybe_fail(self, step: str) -> None:
        if self.fail_on == step:
            raise AdapterError(f"fake mailbox failure at {step}", retryable=True)

    def fetch_new(self) -> list[Email]:
        self._maybe_fail("fetch")
        return sorted((e for e in self.emails if e.message_id not in self.processed), key=lambda e: e.received_at)

    def add_labels(self, message_id: str, labels: list[str]) -> None:
        self._maybe_fail("labels")
        current = self.labels.setdefault(message_id, [])
        current.extend(l for l in labels if l not in current)

    def create_draft(self, email: Email, subject: str, body: str) -> str:
        self._maybe_fail("draft")
        self.drafts.append({"thread_id": email.thread_id, "to": email.from_email, "subject": subject, "body": body})
        return f"draft-{len(self.drafts)}"

    def mark_processed(self, message_id: str) -> None:
        self._maybe_fail("processed")
        self.processed.add(message_id)

    def first_reply_after(self, thread_id: str, since: float) -> float | None:
        t = self.replies.get(thread_id)
        return t if t is not None and t > since else None


@dataclass
class FakeSheet:
    rows: dict[str, dict] = field(default_factory=dict)
    fail_on: str | None = None
    name: str = "fake"
    _ids: itertools.count = field(default_factory=lambda: itertools.count(2))  # row 1 is the header

    def append(self, row: dict) -> str:
        if self.fail_on == "append":
            raise AdapterError("fake sheet failure at append", retryable=True)
        ref = f"row-{next(self._ids)}"
        self.rows[ref] = dict(row)
        return ref

    def update_status(self, ref: str, status: str) -> None:
        if self.fail_on == "update":
            raise AdapterError("fake sheet failure at update", retryable=True)
        self.rows[ref]["status"] = status
