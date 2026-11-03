"""SQLite storage: one row per email with a state machine, and an event log for every step."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    from_name TEXT,
    from_email TEXT NOT NULL,
    subject TEXT,
    body TEXT,
    received_at REAL NOT NULL,
    link TEXT,
    state TEXT NOT NULL DEFAULT 'received',
    category TEXT,
    urgency TEXT,
    assignee TEXT,
    deadline REAL,
    follow_status TEXT,
    replied_at REAL,
    classification TEXT,
    classifier TEXT,
    actions TEXT,
    draft TEXT,
    error TEXT,
    resume_state TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL REFERENCES emails(id),
    kind TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    week_start TEXT PRIMARY KEY,
    metrics TEXT,
    issues TEXT,
    summary TEXT,
    summary_ok INTEGER,
    summary_note TEXT,
    sheet_ref TEXT,
    message_id TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_email ON events(email_id, id);
CREATE INDEX IF NOT EXISTS idx_emails_state ON emails(state);
CREATE INDEX IF NOT EXISTS idx_emails_follow ON emails(follow_status, deadline);
"""

# received --classify--> classified --act--> acted --finish--> done
TRANSITIONS: dict[str, set[str]] = {
    "received": {"classified", "needs_attention"},
    "classified": {"acted", "needs_attention"},
    "acted": {"done", "needs_attention"},
    "needs_attention": {"received", "classified", "acted"},
    "done": set(),
}
STATES = set(TRANSITIONS)
JSON_FIELDS = ("classification", "actions", "draft")
FIELDS = {"category", "urgency", "assignee", "deadline", "follow_status", "replied_at", "classification", "classifier",
          "actions", "draft", "error", "resume_state", "attempts"}


class InvalidTransition(ValueError):
    pass


def _encode(fields: dict[str, Any]) -> dict[str, Any]:
    unknown = set(fields) - FIELDS
    if unknown:
        raise ValueError(f"Unknown email fields: {sorted(unknown)}")
    return {k: json.dumps(v) if k in JSON_FIELDS and v is not None and not isinstance(v, str) else v
            for k, v in fields.items()}


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in JSON_FIELDS:
        if d.get(k):
            d[k] = json.loads(d[k])
    return d


class Store:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(emails)")}
        if "replied_at" not in have:  # databases from milestone 3
            self._db.execute("ALTER TABLE emails ADD COLUMN replied_at REAL")
        self._db.commit()

    def add_email(self, e) -> tuple[int, bool]:
        """Insert an email. Returns (id, created). A message seen before returns its first row."""
        now = time.time()
        with self._lock:
            try:
                cur = self._db.execute(
                    "INSERT INTO emails (message_id, thread_id, from_name, from_email, subject, body, received_at, "
                    "link, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (e.message_id, e.thread_id, e.from_name, e.from_email, e.subject, e.body, e.received_at,
                     e.link, now, now))
            except sqlite3.IntegrityError:
                row = self._db.execute("SELECT id FROM emails WHERE message_id = ?", (e.message_id,)).fetchone()
                return row["id"], False
            self._log(cur.lastrowid, "received", None, now)
            self._db.commit()
            return cur.lastrowid, True

    def update(self, email_id: int, **fields: Any) -> None:
        fields = _encode(fields)
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE emails SET {sets}, updated_at = ? WHERE id = ?",
                             (*fields.values(), time.time(), email_id))
            self._db.commit()

    def transition(self, email_id: int, new_state: str, detail: Any = None, **fields: Any) -> None:
        fields = _encode(fields)
        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT state FROM emails WHERE id = ?", (email_id,)).fetchone()
            if row is None:
                raise KeyError(email_id)
            if new_state not in TRANSITIONS[row["state"]]:
                raise InvalidTransition(f"{row['state']} -> {new_state}")
            sets = "".join(f"{k} = ?, " for k in fields)
            self._db.execute(f"UPDATE emails SET state = ?, {sets}updated_at = ? WHERE id = ?",
                             (new_state, *fields.values(), now, email_id))
            self._log(email_id, new_state, detail, now)
            self._db.commit()

    def bump_attempts(self, email_id: int) -> int:
        with self._lock:
            self._db.execute("UPDATE emails SET attempts = attempts + 1 WHERE id = ?", (email_id,))
            self._db.commit()
            return self._db.execute("SELECT attempts FROM emails WHERE id = ?", (email_id,)).fetchone()[0]

    def log(self, email_id: int, kind: str, detail: Any = None) -> None:
        with self._lock:
            self._log(email_id, kind, detail, time.time())
            self._db.commit()

    def _log(self, email_id: int, kind: str, detail: Any, now: float) -> None:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail)
        self._db.execute("INSERT INTO events (email_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
                         (email_id, kind, detail, now))

    def get(self, email_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
        return _decode(row) if row else None

    def by_message_id(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM emails WHERE message_id = ?", (message_id,)).fetchone()
        return _decode(row) if row else None

    def emails(self, state: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM emails", []
        if state:
            q, args = q + " WHERE state = ?", [state]
        with self._lock:
            rows = self._db.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [_decode(r) for r in rows]

    def open_follow_ups(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM emails WHERE state = 'done' AND follow_status IN ('open', 'overdue') "
                "ORDER BY deadline").fetchall()
        return [_decode(r) for r in rows]

    # ---------- weekly reports ----------

    def report(self, week_start: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM reports WHERE week_start = ?", (week_start,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        for k in ("metrics", "issues"):
            if d.get(k):
                d[k] = json.loads(d[k])
        return d

    def save_report(self, week_start: str, **fields: Any) -> None:
        allowed = {"metrics", "issues", "summary", "summary_ok", "summary_note", "sheet_ref", "message_id", "error"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown report fields: {sorted(unknown)}")
        fields = {k: json.dumps(v) if k in ("metrics", "issues") and v is not None else v for k, v in fields.items()}
        now = time.time()
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO reports (week_start, created_at, updated_at) VALUES (?, ?, ?)",
                             (week_start, now, now))
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                self._db.execute(f"UPDATE reports SET {sets}, updated_at = ? WHERE week_start = ?",
                                 (*fields.values(), now, week_start))
            self._db.commit()

    def all_emails_for_report(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT received_at, category, follow_status, replied_at, from_name, from_email, subject "
                "FROM emails WHERE state = 'done'").fetchall()
        return [dict(r) for r in rows]

    def events(self, email_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT kind, detail, created_at FROM events WHERE email_id = ? ORDER BY id",
                                    (email_id,)).fetchall()
        return [dict(r) for r in rows]
