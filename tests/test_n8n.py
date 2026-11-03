"""The n8n workflows must apply the same rules as the Python app.

Runs n8n/src/logic.js with plain Node on the same inputs as the Python code and compares results.
Skipped if Node is not installed.
"""
import base64
import datetime as dt
import email as email_lib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "n8n"))
import build  # noqa: E402

from inbox.checks import check  # noqa: E402
from inbox.drafts import Draft, check_draft  # noqa: E402
from inbox.models import Classification, Email, Extracted  # noqa: E402
from inbox.report import (check_numbers, compute, find_issues, render, sheet_row, template_summary)  # noqa: E402
from inbox.rules import CATEGORIES, URGENCIES, load_rules  # noqa: E402
from inbox.sample_jobs import sample_jobs  # noqa: E402
from inbox.service import Pipeline  # noqa: E402

RULES = load_rules(ROOT / "routing.yaml")
CFG = build.config(ROOT / "routing.yaml", "claude-opus-5-5", "low")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")
TZ = ZoneInfo(RULES.timezone)


def js(calls):
    logic = (ROOT / "n8n" / "src" / "logic.js").read_text().replace("/*CONFIG*/ {}", json.dumps(CFG))
    script = logic + """
const calls = JSON.parse(require('fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(calls.map(c => { try { return module.exports[c[0]](...c[1]); } catch (e) { return { error: e.message }; } })));"""
    r = subprocess.run([NODE, "-e", script], input=json.dumps(calls), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


LEAK = Email(message_id="m1", thread_id="t1", from_name="Maria Lopez", from_email="maria@example.com",
             subject="Water coming through kitchen ceiling!!",
             body="Water is coming through our kitchen ceiling. Can someone come today? 14 Elm Street. "
                  "My number is 555-201-3344. We paid a $75 deposit.", received_at=1_793_700_000)

CHECK_CASES = [
    Classification(category="new_job", urgency="now", urgency_evidence="“water is coming through our kitchen ceiling”",
                   extracted=Extracted(name="Maria Lopez", phone="(555) 201-3344", address="14 Elm Street", amount="$75")),
    Classification(category="new_job", urgency="now", urgency_evidence="the house is on fire",
                   extracted=Extracted(phone="555-999-0000", amount="$7", invoice_number="INV-9")),
    Classification(category="complaint", urgency="today", urgency_evidence=None, extracted=Extracted(address="22 Oak Road")),
    Classification(category="spam_marketing", urgency="later", urgency_evidence="x", needs_reply=True),
]


@needs_node
@pytest.mark.parametrize("c", CHECK_CASES)
def test_checks_match_python(c):
    py = check(c, LEAK)
    [out] = js([["checkClassification", [c.model_dump(), LEAK.model_dump()]]])
    assert out["corrections"] == py.corrections
    got = out["classification"]
    assert (got["urgency"], got["urgency_evidence"], got["needs_reply"]) == (
        py.classification.urgency, py.classification.urgency_evidence, py.classification.needs_reply)
    assert {k: got["extracted"].get(k) for k in Extracted.model_fields} == py.classification.extracted.model_dump()


@needs_node
def test_routing_and_deadlines_match_python():
    calls = [["route", [c, u]] for c in CATEGORIES for u in URGENCIES]
    for (c, u), got in zip([(c, u) for c in CATEGORIES for u in URGENCIES], js(calls)):
        assert got == RULES.route(c, u).model_dump(), (c, u)
    p = Pipeline.__new__(Pipeline)
    p.tz = TZ
    ts = [1_793_700_000, 1_798_000_000, 1_783_000_000]       # winter, a later winter day, summer time
    whens, isos = js([["when", [t]] for t in ts]), js([["isoLocal", [t]] for t in ts])
    assert whens == [Pipeline.when(p, t) for t in ts]
    assert isos == [dt.datetime.fromtimestamp(t, TZ).isoformat(timespec="minutes") for t in ts]


@needs_node
def test_sheet_row_matches_the_python_columns():
    answer = {"category": "complaint", "urgency": "today", "urgency_evidence": "Still not fixed",
              "extracted": {"phone": None}, "needs_reply": True, "summary": "s"}
    e = {**LEAK.model_dump(), "subject": "Still not fixed", "link": "https://mail.google.com/x"}
    [t] = js([["triage", [e, answer]]])
    assert t["rule"]["assign"] == "manager" and t["deadline"] == e["received_at"] + 4 * 3600 and t["wantsDraft"]
    [row] = js([["sheetRow", [e, t, "saved"]]])
    assert len(row) == len(CFG["logColumns"]) and row[CFG["logColumns"].index("assigned_to")] == "Dana (manager)"
    assert row[-1] == dt.datetime.fromtimestamp(e["received_at"], TZ).isoformat(timespec="minutes")


DRAFTS = [("Re: leak", "Hi [Name], we'll send a technician within 30 minutes. The callout is $95 and a 10% discount."),
          ("Re", "We have your refund request about the $75."),
          ("Re", "Thanks, the office will be in touch. " + "word " * 210)]


@needs_node
@pytest.mark.parametrize("subject,body", DRAFTS)
def test_draft_checks_match_python(subject, body):
    py = check_draft(Draft(subject=subject, body=body), LEAK, RULES).warnings
    [got] = js([["checkDraft", [{"subject": subject, "body": body}, LEAK.model_dump()]]])
    assert got == py


@needs_node
def test_report_matches_python():
    today = dt.date(2026, 11, 3)
    jobs = sample_jobs(today)
    ts = lambda d, h=10: dt.datetime.combine(d, dt.time(h), TZ).timestamp()  # noqa: E731
    emails = [
        {"received_at": ts(dt.date(2026, 10, 27)), "category": "new_job", "follow_status": "replied",
         "replied_at": ts(dt.date(2026, 10, 27), 12), "from_name": "A", "from_email": "a@x", "subject": "s"},
        {"received_at": ts(dt.date(2026, 10, 28)), "category": "complaint", "follow_status": "overdue",
         "replied_at": None, "from_name": "B", "from_email": "b@x", "subject": "Still broken"},
        {"received_at": ts(dt.date(2026, 10, 30)), "category": "spam_marketing", "follow_status": "none",
         "replied_at": None, "from_name": "", "from_email": "d@x", "subject": "s"},
    ]
    py = compute(dt.date(2026, 10, 26), jobs, emails, TZ, today)
    js_jobs = [{**j.model_dump(), "date": j.date.isoformat(), "due_date": j.due_date.isoformat() if j.due_date else None}
               for j in jobs]
    [m] = js([["computeMetrics", ["2026-10-26", js_jobs, emails, "2026-11-03"]]])
    assert m == py
    issues = find_issues(py, RULES)
    note = "the AI summary was replaced because it contained numbers not in the data: 47%"
    got_issues, got_summary, got_render, got_row, got_bad = js([
        ["findIssues", [py]], ["templateSummary", [py, issues]], ["renderReport", [py, issues, "S", note]],
        ["reportRow", [py, issues, False]], ["checkNumbers", ["Revenue fell 47% to $2,040 and $900 is late.", py, issues]]])
    assert got_issues == issues and got_summary == template_summary(py, issues)
    subject, text, _ = render(py, issues, "S", note, RULES.business)
    assert (got_render["subject"], got_render["text"]) == (subject, text)
    assert got_row == [str(v) for v in sheet_row(py, issues, False).values()]
    assert got_bad == check_numbers("Revenue fell 47% to $2,040 and $900 is late.", py, issues)


@needs_node
def test_python_style_rounding():
    assert js([["roundHalfEven", [2.5]], ["roundHalfEven", [3.5]], ["roundHalfEven", [-49.5]],
               ["roundHalfEven", [3.25, 1]]]) == [round(2.5), round(3.5), round(-49.5), round(3.25, 1)]


@needs_node
def test_gmail_parsing_and_raw_messages():
    b64 = lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")  # noqa: E731
    msg = {"id": "m9", "threadId": "t9", "internalDate": "1793700000000", "payload": {
        "headers": [{"name": "From", "value": '"Accounts, Riverside Cafe" <accounts@riverside.example>'},
                    {"name": "Subject", "value": "Invoice"}, {"name": "Message-ID", "value": "<orig@x>"}],
        "mimeType": "multipart/alternative",
        "parts": [{"mimeType": "text/html", "body": {"data": b64("<p>Paid&nbsp;today</p>")}}]}}
    [e] = js([["parseMessage", [msg]]])
    assert (e["from_name"], e["from_email"], e["body"], e["received_at"]) == (
        "Accounts, Riverside Cafe", "accounts@riverside.example", "Paid today", 1_793_700_000)
    raw, report = js([["rawReply", [e, "Re: Invoice\nBcc: evil@x.com", "Thanks."]],
                      ["rawReport", ["owner@x.example", "Weekly report", "plain", "<b>html</b>"]]])
    m = email_lib.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert m["In-Reply-To"] == "<orig@x>" and m["Bcc"] is None and m.get_payload().strip() == "Thanks."
    r = email_lib.message_from_bytes(base64.urlsafe_b64decode(report + "=" * (-len(report) % 4)))
    assert r.is_multipart() and [p.get_content_type() for p in r.get_payload()] == ["text/plain", "text/html"]


def test_workflows_are_well_formed_and_never_send():
    wfs = build.build_all(CFG, build.GOOGLE_CRED, "label:Demo", "SHEET", "")
    for name, wf in wfs.items():
        names = [n["name"] for n in wf["nodes"]]
        assert len(names) == len(set(names)), name
        targets = {t["node"] for o in wf["connections"].values() for branch in o["main"] for t in branch}
        assert all(src in names for src in wf["connections"]) and targets <= set(names)
        assert len(set(names) - targets) == 1, name                 # only the trigger has no input
        text = json.dumps(wf)
        assert "/send" not in text and "messages/send" not in text   # nothing ever sends email
    triage = wfs["inbox-triage.json"]
    q = next(n for n in triage["nodes"] if n["name"] == "Gmail: new messages")["parameters"]["queryParameters"]
    assert q["parameters"][0]["value"] == "label:Demo -label:AI-Processed -label:AI-Report"
    # the processed mark is the last step on every path
    order = triage["connections"]
    assert order["Slack: alert"]["main"][0][0]["node"] == "Gmail: mark processed"
    assert order["Alert?"]["main"][1][0]["node"] == "Gmail: mark processed"
    assert "Gmail: mark processed" not in order


def test_schemas_match_the_python_models():
    from inbox.classifier import _Answer, _Extracted
    from inbox.drafts import _DraftOut
    assert set(build.CLASSIFY_SCHEMA["required"]) == set(_Answer.model_fields)
    assert set(build.CLASSIFY_SCHEMA["properties"]["extracted"]["required"]) == set(_Extracted.model_fields)
    assert set(build.DRAFT_SCHEMA["required"]) == set(_DraftOut.model_fields)
