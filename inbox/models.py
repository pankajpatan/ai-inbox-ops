"""Data shapes shared by the mailbox, classifiers, checks and storage."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

Category = Literal["new_job", "quote_request", "complaint", "invoice_payment", "supplier", "spam_marketing", "other"]
Urgency = Literal["now", "today", "later"]


class Email(BaseModel):
    message_id: str
    thread_id: str
    from_name: str = ""
    from_email: str
    subject: str = ""
    body: str = ""
    received_at: float          # unix seconds
    link: str | None = None     # opens the message in Gmail

    @property
    def text(self) -> str:
        return f"{self.subject}\n{self.body}"


class Extracted(BaseModel):
    name: str | None = None
    phone: str | None = None
    address: str | None = None
    job_type: str | None = None
    invoice_number: str | None = None
    amount: str | None = None


class Classification(BaseModel):
    """What a classifier returns. Code checks it before routing (checks.py)."""

    category: Category
    urgency: Urgency
    # Short quote from the email that shows why it is urgent. Required for "now" and "today".
    urgency_evidence: str | None = None
    extracted: Extracted = Extracted()
    needs_reply: bool = True
    language: str = "en"
    summary: str = ""
    meta: dict[str, Any] = {}


class Checked(BaseModel):
    classification: Classification
    corrections: list[str] = []
