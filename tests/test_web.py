from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from inbox.adapters import FakeMailbox, FakeSheet
from inbox.alerts import LogNotifier
from inbox.classifier import KeywordClassifier
from inbox.config import Settings
from inbox.demo import load_fixture
from inbox.drafts import TemplateDrafter
from inbox.report import FakeJobs
from inbox.reporting import FakeSink, Reporter
from inbox.rules import load_rules
from inbox.sample_jobs import sample_jobs
from inbox.service import Pipeline
from inbox.store import Store
from inbox.web import create_app

ROOT = Path(__file__).resolve().parent.parent
RULES = load_rules(ROOT / "routing.yaml")
NOW = 1_793_700_000.0


class Broken:
    name = "broken"

    def classify(self, email, rules):
        raise RuntimeError("API down")


def make(tmp_path, demo=True, classifier=None):
    store = Store(str(tmp_path / "w.db"))
    mailbox = FakeMailbox(load_fixture(ROOT / "fixtures" / "sample_inbox.jsonl", NOW))
    p = Pipeline(store, mailbox, FakeSheet(), classifier or KeywordClassifier(), RULES, TemplateDrafter(),
                 LogNotifier(), clock=lambda: NOW)
    import datetime as dt
    reporter = Reporter(store, FakeJobs(sample_jobs(dt.date(2026, 11, 3))), FakeSheet(), FakeSink(), RULES)
    settings = Settings(demo_mode=demo, admin_password="correct-horse-battery" if not demo else "")
    return p, create_app(p, reporter, settings, poll=False)


def test_dashboard_lists_emails_and_waiting_items(tmp_path):
    p, app = make(tmp_path)
    p.poll()
    with TestClient(app) as c:
        emails = c.get("/api/emails").json()
        assert len(emails) == 7
        leak = next(e for e in emails if e["from"] == "Maria Lopez")
        assert leak["urgency"] == "now" and leak["assigned_to"] == "Office team" and leak["has_draft"]
        assert "body" not in leak and len(leak["preview"]) <= 280
        assert len(c.get("/api/emails?category=complaint").json()) == 1
        assert c.get("/api/emails?category=bogus").status_code == 422
        page = c.get("/dashboard")
        assert page.status_code == 200 and "frame-ancestors 'none'" in page.headers["content-security-policy"]
        detail = c.get(f"/api/emails/{leak['id']}").json()
        assert [e["kind"] for e in detail["events"]][:2] == ["received", "classified"]


def test_retry_from_the_dashboard_and_csrf(tmp_path):
    p, app = make(tmp_path, classifier=Broken())
    p.poll()
    with TestClient(app) as c:
        failing = c.get("/api/emails?state=needs_attention").json()
        assert len(failing) == 7 and "API down" in failing[0]["error"]
        eid = failing[0]["id"]
        assert c.post(f"/api/emails/{eid}/retry", headers={"Origin": "https://evil.example"}).status_code == 403
        p.classifier = KeywordClassifier()
        r = c.post(f"/api/emails/{eid}/retry")
        assert r.status_code == 200 and r.json()["state"] == "done"
        assert c.post(f"/api/emails/{eid}/retry").status_code == 409


def test_report_preview_writes_nothing(tmp_path):
    p, app = make(tmp_path)
    with TestClient(app) as c:
        d = c.get("/api/report?week=2026-10-26").json()
        assert d["source"] == "preview" and d["subject"].startswith("Weekly report, 26 Oct")
        assert any("Revenue fell 50%" in i for i in d["issues"])
    assert p.store.report("2026-10-26") is None


def test_live_mode_requires_password(tmp_path):
    p, _ = make(tmp_path)
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        create_app(p, None, Settings(demo_mode=False, admin_password="short"))
    p, app = make(tmp_path, demo=False)
    with TestClient(app) as c:
        assert c.get("/dashboard").status_code == 401
        assert c.get("/api/emails", auth=("a", "wrong")).status_code == 401
        assert c.get("/api/emails", auth=("a", "correct-horse-battery")).status_code == 200
        assert c.get("/health").json()["demo_mode"] is False
