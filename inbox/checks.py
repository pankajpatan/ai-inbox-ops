"""Check a classification against the email before anything acts on it.

- Urgency "now" or "today" needs a quote that is really in the email. Without one it is lowered
  by one step only (now -> today, today -> later), so a real emergency is never buried.
- Extracted phone numbers, invoice numbers and amounts must appear in the email; names and
  addresses are kept only if their words appear. Anything else becomes null.
- Spam needs no reply.
"""
from __future__ import annotations

import re

from .models import Checked, Classification, Email

_SAME = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})
STEP_DOWN = {"now": "today", "today": "later"}


def norm(text: str) -> str:
    text = (text or "").translate(_SAME)
    return re.sub(r"\s+", " ", text).strip().strip("\"'.,").lower()


def digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def check(c: Classification, email: Email) -> Checked:
    corrections: list[str] = []
    text = norm(email.text)
    update: dict = {}

    if c.urgency in STEP_DOWN:
        ev = norm(c.urgency_evidence or "")
        if not ev or ev not in text:
            lowered = STEP_DOWN[c.urgency]
            corrections.append(f"urgency '{c.urgency}' had no quote from the email; lowered to '{lowered}'")
            update["urgency"] = lowered
            update["urgency_evidence"] = None
    elif c.urgency_evidence:
        update["urgency_evidence"] = None

    x = c.extracted.model_copy()
    raw_digits = digits(email.text)
    for field in ("phone", "invoice_number", "amount"):
        value = getattr(x, field)
        if value is None:
            continue
        if field == "phone":  # formatting may differ: compare the digits
            found = len(digits(value)) >= 7 and digits(value) in raw_digits
        elif field == "amount":  # the number itself must appear, not just its digits inside another number
            number = re.sub(r"[^\d.,]", "", value).strip(".,")
            found = bool(number) and re.search(rf"(?<![\d.,]){re.escape(number)}(?![\d])", email.text) is not None
        else:
            found = norm(value) in text or (len(digits(value)) >= 3 and digits(value) in raw_digits)
        if not found:
            corrections.append(f"{field} '{value}' is not in the email; removed")
            setattr(x, field, None)
    for field in ("name", "address"):
        value = getattr(x, field)
        if value is None:
            continue
        words = [w for w in re.findall(r"[\w']+", norm(value)) if len(w) > 1]
        sender = norm(f"{email.from_name} {email.from_email}")
        if not words or not all(w in text or w in sender for w in words):
            corrections.append(f"{field} '{value}' is not in the email; removed")
            setattr(x, field, None)
    update["extracted"] = x

    if c.category == "spam_marketing" and c.needs_reply:
        update["needs_reply"] = False
        corrections.append("spam needs no reply")
    return Checked(classification=c.model_copy(update=update), corrections=corrections)
