"""Dashboard and the always-on process: polls Gmail, runs follow-ups and the weekly report.

    python -m inbox.web                 # dashboard at http://127.0.0.1:8030/dashboard, poller running
    python -m inbox.web --no-poll       # dashboard only

Outside demo mode the dashboard needs ADMIN_PASSWORD (HTTP basic auth). Run one process only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import Settings
from .report import due_week, render
from .reporting import Reporter
from .rules import CATEGORIES
from .service import LABELS, Pipeline
from .store import STATES

log = logging.getLogger("inbox")
PAGE = Path(__file__).parent / "static" / "dashboard.html"
basic = HTTPBasic(auto_error=False)


def require_same_origin(request: Request) -> None:
    """Browsers resend basic-auth logins automatically, so reject actions coming from other sites (CSRF)."""
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        raise HTTPException(403, "Cross-site request blocked")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Cross-site request blocked")


def public(row: dict, pipeline: Pipeline) -> dict:
    """What the dashboard shows for one email (no raw body beyond a preview)."""
    c = row.get("classification") or {}
    person = pipeline.rules.people.get(row["assignee"]) if row.get("assignee") else None
    draft = row.get("draft") or {}
    return {
        "id": row["id"], "state": row["state"], "from": row.get("from_name") or row["from_email"],
        "from_email": row["from_email"], "subject": row.get("subject") or "", "preview": (row.get("body") or "")[:280],
        "received": pipeline.when(row["received_at"]), "category": row.get("category"),
        "category_label": LABELS.get(row.get("category") or "", ""), "urgency": row.get("urgency"),
        "urgency_evidence": c.get("urgency_evidence"), "summary": c.get("summary", ""),
        "assigned_to": person.name if person else "", "reply_by": pipeline.when(row.get("deadline")),
        "overdue_now": bool(row.get("deadline") and row.get("follow_status") == "open"
                            and pipeline.clock() > row["deadline"]),
        "follow_status": row.get("follow_status"), "link": row.get("link"),
        "draft_warnings": draft.get("warnings", []), "has_draft": bool((row.get("actions") or {}).get("draft_id")),
        "error": row.get("error"), "resume_state": row.get("resume_state"), "attempts": row.get("attempts"),
    }


def create_app(pipeline: Pipeline, reporter: Reporter | None, settings: Settings, poll: bool = False,
               poll_seconds: int = 120, follow_up_seconds: int = 3600) -> FastAPI:
    settings.validate()
    stop = threading.Event()

    def loop() -> None:
        last_follow_up = 0.0
        while not stop.is_set():
            try:
                pipeline.poll()
                if time.time() - last_follow_up >= follow_up_seconds:
                    pipeline.follow_up()
                    last_follow_up = time.time()
                if reporter is not None:
                    week = due_week(dt.datetime.now(pipeline.tz), pipeline.rules)
                    if week and not (reporter.store.report(week.isoformat()) or {}).get("message_id"):
                        reporter.run(week)
            except Exception:
                log.exception("background round failed; will retry")
            stop.wait(poll_seconds)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        threading.Thread(target=pipeline.resume_pending, daemon=True, name="resume").start()
        if poll:
            threading.Thread(target=loop, daemon=True, name="poller").start()
        yield
        stop.set()

    app = FastAPI(title="AI Inbox Ops", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def require_admin(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if settings.demo_mode:
            return
        ok = credentials is not None and secrets.compare_digest(credentials.password.encode(),
                                                                settings.admin_password.encode())
        if not ok:
            raise HTTPException(401, "Authentication required", {"WWW-Authenticate": "Basic"})

    admin = [Depends(require_admin)]

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "mailbox": pipeline.mailbox.name, "sheet": pipeline.sheet.name,
                "classifier": pipeline.classifier.name, "polling": poll, "demo_mode": settings.demo_mode}

    @app.get("/api/emails", dependencies=admin)
    def emails(state: str | None = None, category: str | None = None) -> list[dict]:
        if state and state not in STATES:
            raise HTTPException(422, f"state must be one of {sorted(STATES)}")
        if category and category not in CATEGORIES:
            raise HTTPException(422, f"category must be one of {CATEGORIES}")
        rows = [r for r in pipeline.store.emails(state) if not category or r.get("category") == category]
        return [public(r, pipeline) for r in rows]

    @app.get("/api/emails/{email_id}", dependencies=admin)
    def email(email_id: int) -> dict:
        row = pipeline.store.get(email_id)
        if row is None:
            raise HTTPException(404, "Email not found")
        return {**public(row, pipeline), "events": pipeline.store.events(email_id)}

    @app.post("/api/emails/{email_id}/retry", dependencies=admin + [Depends(require_same_origin)])
    def retry(email_id: int) -> dict:
        if not pipeline.retry(email_id):
            raise HTTPException(409, "Only emails that need attention can be retried")
        return public(pipeline.store.get(email_id), pipeline)

    @app.get("/api/report", dependencies=admin)
    def report(week: str | None = None) -> dict:
        """The saved report for a week, or a live preview of last week (nothing written)."""
        if reporter is None:
            raise HTTPException(404, "Weekly report is not set up")
        tz = pipeline.tz
        start = dt.date.fromisoformat(week) if week else (
            dt.datetime.now(tz).date() - dt.timedelta(days=dt.datetime.now(tz).weekday() + 7))
        saved = reporter.store.report(start.isoformat())
        if saved and saved.get("metrics"):
            built, source = saved, "delivered" if saved.get("message_id") else "saved"
        else:
            built, source = reporter.build(start), "preview"
        subject, text, _ = render(built["metrics"], built["issues"], built["summary"], built["summary_note"],
                                  pipeline.rules.business)
        return {"week": start.isoformat(), "source": source, "subject": subject, "text": text,
                "issues": built["issues"], "summary_ok": bool(built["summary_ok"])}

    @app.get("/dashboard", response_class=HTMLResponse, dependencies=admin)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                       "style-src 'unsafe-inline'; frame-ancestors 'none'"})

    return app


def main() -> None:
    import os

    import uvicorn
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-poll", action="store_true")
    p.add_argument("--port", type=int, default=8030)
    args = p.parse_args()

    from .report import ClaudeSummary
    from .run import build
    from .weekly import live_reporter
    pipeline = build()
    key = os.getenv("ANTHROPIC_API_KEY")
    reporter = live_reporter(pipeline.rules, ClaudeSummary(key, os.getenv("INBOX_MODEL", "claude-opus-5-5"))
                             if key else None)
    app = create_app(pipeline, reporter, Settings.from_env(), poll=not args.no_poll)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
