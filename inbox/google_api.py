"""Gmail and Google Sheets adapters, and the one-time login.

Scopes:
- gmail.modify: read mail, add labels, create drafts. Google has no drafts-only scope, so this
  scope could also send; this code never calls a send method (tests/test_google.py checks).
- spreadsheets: create and edit the log sheet.
"""
from __future__ import annotations

import base64
import html
import logging
import re
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any

from .adapters import AdapterError
from .models import Email

log = logging.getLogger("inbox")

SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/spreadsheets"]
CREDENTIALS_DIR = Path(__file__).resolve().parent.parent / "google-credentials"
CLIENT_FILE = CREDENTIALS_DIR / "client_secret.json"
TOKEN_FILE = CREDENTIALS_DIR / "token.json"
PROCESSED_LABEL = "AI/Processed"
DEMO_LABEL = "Demo"
DEFAULT_QUERY = "in:inbox -from:me newer_than:14d"
LOG_COLUMNS = ["received", "from", "subject", "category", "urgency", "assigned_to", "reply_by", "summary",
               "phone", "draft", "status", "link"]
STATUS_COLUMN = chr(ord("A") + LOG_COLUMNS.index("status"))


def load_credentials(interactive: bool = False):
    """Return valid Google credentials, refreshing them; with interactive=True, run the browser login."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES) if TOKEN_FILE.exists() else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
            return creds
        except Exception as exc:  # testing-mode tokens expire after 7 days
            if not interactive:
                raise AdapterError(f"Google login expired ({exc}). Run: python -m inbox.google_setup login") from exc
    if not interactive:
        raise AdapterError("Not logged in to Google. Run: python -m inbox.google_setup login")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent")
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    return creds


def _run(request, what: str) -> Any:
    """Execute a Google API request. The client retries 429 and 5xx itself (num_retries)."""
    from googleapiclient.errors import HttpError
    try:
        return request.execute(num_retries=3)
    except HttpError as exc:
        status = getattr(exc.resp, "status", 0)
        raise AdapterError(f"Google {what} failed ({status}): {exc.reason if hasattr(exc, 'reason') else exc}"[:300],
                           retryable=status == 429 or status >= 500) from exc
    except OSError as exc:
        raise AdapterError(f"Google {what} unreachable: {exc}"[:300], retryable=True) from exc


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", markup)
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def message_text(payload: dict) -> str:
    """Plain-text body of a Gmail message: prefer text/plain, fall back to stripped HTML."""
    plain, rich = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            rich.append(_html_to_text(_decode(data)))
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    text = "\n".join(plain) if plain else "\n".join(rich)
    text = re.sub(r"[ \t ]+", " ", text)  # &nbsp; must match normal spaces for the quote checks
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:20000]


class GmailMailbox:
    name = "gmail"

    def __init__(self, service, query: str = DEFAULT_QUERY, max_results: int = 50):
        self.svc = service
        self.query = f"{query} -label:{PROCESSED_LABEL.replace('/', '-')}"
        self.max_results = max_results
        self._labels: dict[str, str] | None = None

    def _label_ids(self, names: list[str]) -> list[str]:
        if self._labels is None:
            res = _run(self.svc.users().labels().list(userId="me"), "list labels")
            self._labels = {l["name"]: l["id"] for l in res.get("labels", [])}
        ids = []
        for name in names:
            if name not in self._labels:
                body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
                created = _run(self.svc.users().labels().create(userId="me", body=body), "create label")
                self._labels[name] = created["id"]
            ids.append(self._labels[name])
        return ids

    def fetch_new(self) -> list[Email]:
        res = _run(self.svc.users().messages().list(userId="me", q=self.query, maxResults=self.max_results),
                   "list messages")
        emails = []
        for ref in res.get("messages", []):
            m = _run(self.svc.users().messages().get(userId="me", id=ref["id"], format="full"), "get message")
            headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
            name, addr = parseaddr(headers.get("from", ""))
            emails.append(Email(
                message_id=m["id"], thread_id=m["threadId"], from_name=name, from_email=addr or "unknown@unknown",
                subject=headers.get("subject", ""), body=message_text(m.get("payload", {})),
                received_at=int(m.get("internalDate", "0")) / 1000,
                link=f"https://mail.google.com/mail/u/0/#all/{m['id']}",
            ))
        return sorted(emails, key=lambda e: e.received_at)

    def add_labels(self, message_id: str, labels: list[str]) -> None:
        body = {"addLabelIds": self._label_ids(labels)}
        _run(self.svc.users().messages().modify(userId="me", id=message_id, body=body), "add labels")

    def create_draft(self, email: Email, subject: str, body: str) -> str:
        original = _run(self.svc.users().messages().get(userId="me", id=email.message_id, format="metadata",
                                                        metadataHeaders=["Message-ID", "References"]), "get headers")
        headers = {h["name"].lower(): h["value"] for h in original.get("payload", {}).get("headers", [])}
        msg = EmailMessage()
        msg["To"] = email.from_email
        msg["Subject"] = " ".join(subject.split())
        if headers.get("message-id"):  # keeps the draft in the customer's thread in their mail app too
            msg["In-Reply-To"] = headers["message-id"]
            msg["References"] = f"{headers.get('references', '')} {headers['message-id']}".strip()
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        draft = _run(self.svc.users().drafts().create(
            userId="me", body={"message": {"raw": raw, "threadId": email.thread_id}}), "create draft")
        return draft["id"]

    def mark_processed(self, message_id: str) -> None:
        self.add_labels(message_id, [PROCESSED_LABEL])

    def replied_since(self, thread_id: str, since: float) -> bool:
        t = _run(self.svc.users().threads().get(userId="me", id=thread_id, format="minimal"), "get thread")
        return any("SENT" in m.get("labelIds", []) and int(m.get("internalDate", "0")) / 1000 > since
                   for m in t.get("messages", []))

    def insert_test_email(self, from_name: str, from_email: str, subject: str, body: str, date_header: str) -> str:
        """Put a fictional email straight into this inbox, as if received, labelled DEMO_LABEL so the
        demo can be limited to these emails. Nothing is sent anywhere."""
        msg = EmailMessage()
        msg["From"] = formataddr((from_name, from_email))  # quotes names like "Accounts, Riverside Cafe"
        msg["To"] = "me"
        msg["Subject"] = subject
        msg["Date"] = date_header
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        labels = ["INBOX", "UNREAD", *self._label_ids([DEMO_LABEL])]
        res = _run(self.svc.users().messages().insert(
            userId="me", internalDateSource="dateHeader", body={"raw": raw, "labelIds": labels}), "insert test email")
        return res["id"]


class GoogleSheet:
    name = "google_sheets"

    def __init__(self, service, spreadsheet_id: str, tab: str = "Inbox log"):
        self.svc = service.spreadsheets()
        self.id = spreadsheet_id
        self.tab = tab

    @classmethod
    def create(cls, service, title: str, tab: str = "Inbox log") -> "GoogleSheet":
        body = {"properties": {"title": title}, "sheets": [{"properties": {"title": tab, "gridProperties": {
            "frozenRowCount": 1}}}]}
        res = _run(service.spreadsheets().create(body=body, fields="spreadsheetId"), "create spreadsheet")
        sheet = cls(service, res["spreadsheetId"], tab)
        _run(sheet.svc.values().update(spreadsheetId=sheet.id, range=f"'{tab}'!A1", valueInputOption="RAW",
                                       body={"values": [[c.replace("_", " ").title() for c in LOG_COLUMNS]]}),
             "write header")
        return sheet

    def append(self, row: dict) -> str:
        values = [[str(row.get(c, "") or "") for c in LOG_COLUMNS]]
        # RAW: text from emails is stored as text, never run as a formula
        res = _run(self.svc.values().append(spreadsheetId=self.id, range=f"'{self.tab}'!A1",
                                            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                                            body={"values": values}), "append row")
        updated = res["updates"]["updatedRange"]            # e.g. 'Inbox log'!A5:L5
        return re.search(r"![A-Z]+(\d+)", updated).group(1)

    def update_status(self, ref: str, status: str) -> None:
        _run(self.svc.values().update(spreadsheetId=self.id, range=f"'{self.tab}'!{STATUS_COLUMN}{ref}",
                                      valueInputOption="RAW", body={"values": [[status]]}), "update status")

    @property
    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.id}"


def services(creds) -> tuple[Any, Any]:
    from googleapiclient.discovery import build
    return (build("gmail", "v1", credentials=creds, cache_discovery=False),
            build("sheets", "v4", credentials=creds, cache_discovery=False))
