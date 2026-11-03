import copy
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest
import yaml

from inbox.adapters import AdapterError, FakeMailbox, FakeSheet
from inbox.alerts import LogNotifier, attention_text, urgent_text
from inbox.checks import check
from inbox.classifier import ClassifierError, ClaudeClassifier, KeywordClassifier, _Answer, _Extracted, cost_usd, system_prompt
from inbox.demo import load_fixture
from inbox.drafts import ClaudeDrafter, Draft, TemplateDrafter, _DraftOut, check_draft
from inbox.models import Classification, Email, Extracted
from inbox.rules import Rules, load_rules
from inbox.service import Pipeline, labels_for
from inbox.store import InvalidTransition, Store

ROOT = Path(__file__).resolve().parent.parent
RULES = load_rules(ROOT / "routing.yaml")
NOW = 1_793_700_000.0  # fixed clock: 2026-11-03
LEAK = Email(message_id="m1", thread_id="t1", from_name="Maria Lopez", from_email="maria@example.com",
             subject="Water coming through kitchen ceiling!!",
             body="Water is coming through our kitchen ceiling and it's getting worse. Can someone come today? "
                  "14 Elm Street. My number is 555-201-3344.", received_at=NOW - 1800, link="https://mail.example/m1")


def email(i=2, subject="Outlet not working", body="Can you book someone this week?", hours_ago=2, **kw):
    return Email(message_id=f"m{i}", thread_id=f"t{i}", from_name=kw.pop("from_name", "Greg Hill"),
                 from_email=kw.pop("from_email", "greg@example.com"), subject=subject, body=body,
                 received_at=NOW - hours_ago * 3600, **kw)


class Fixed:
    """Classifier that returns a set answer (or raises)."""

    name = "fixed"

    def __init__(self, result):
        self.result, self.calls = result, 0

    def classify(self, email, rules):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result(email) if callable(self.result) else self.result


def make(tmp_path, emails=(), classifier=None, mailbox=None, sheet=None, drafter="template", notifier=None):
    mailbox = mailbox or FakeMailbox(list(emails))
    sheet = sheet or FakeSheet()
    notifier = notifier or LogNotifier()
    drafter = TemplateDrafter() if drafter == "template" else drafter
    p = Pipeline(Store(str(tmp_path / "t.db")), mailbox, sheet, classifier or KeywordClassifier(), RULES,
                 drafter, notifier, clock=lambda: NOW)
    return p


# ---------- rules ----------

def raw_rules():
    return yaml.safe_load((ROOT / "routing.yaml").read_text())


def test_routing_rules():
    assert RULES.route("complaint", "later").assign == "manager"
    assert RULES.route("new_job", "now").reply_within_hours == 1 and RULES.route("new_job", "now").alert
    assert RULES.route("quote_request", "later").reply_within_hours == 24
    assert RULES.route("spam_marketing", "now").assign is None


@pytest.mark.parametrize("change,message", [
    (lambda r: r["rules"].pop(), "no rule matches"),
    (lambda r: r["rules"][0].update(assign="nobody"), "unknown person"),
    (lambda r: r["rules"][0].update(category="cats"), "unknown category"),
    (lambda r: r["categories"].pop("other"), "categories must be exactly"),
])
def test_bad_rules_are_refused(change, message):
    r = raw_rules()
    change(r)
    with pytest.raises(ValueError, match=message):
        Rules.model_validate(r)


# ---------- checks ----------

def test_urgency_without_quote_is_lowered_one_step_only():
    c = Classification(category="new_job", urgency="now", urgency_evidence="the house is on fire")
    out = check(c, LEAK)
    assert out.classification.urgency == "today" and len(out.corrections) == 1
    c2 = Classification(category="new_job", urgency="today", urgency_evidence=None)
    assert check(c2, LEAK).classification.urgency == "later"


def test_urgency_with_real_quote_is_kept_loosely_matched():
    c = Classification(category="new_job", urgency="now", urgency_evidence="“water is coming through our kitchen ceiling”")
    out = check(c, LEAK)
    assert out.classification.urgency == "now" and out.corrections == []


def test_invented_fields_are_removed_real_ones_kept():
    c = Classification(category="new_job", urgency="later", extracted=Extracted(
        name="Maria Lopez", phone="(555) 201-3344", address="14 Elm Street",
        invoice_number="INV-9999", amount="$5,000"))
    x = check(c, LEAK).classification.extracted
    assert (x.name, x.phone, x.address) == ("Maria Lopez", "(555) 201-3344", "14 Elm Street")
    assert x.invoice_number is None and x.amount is None
    fake = check(Classification(category="new_job", urgency="later",
                                extracted=Extracted(phone="555-999-0000", address="22 Oak Road")), LEAK)
    assert fake.classification.extracted.phone is None and fake.classification.extracted.address is None


@pytest.mark.parametrize("body,amount,kept", [
    ("We paid a $75 deposit.", "$75", True),           # eval found this: small amounts were dropped
    ("Pay only $25 per lead.", "$25", True),
    ("Invoice total $1,250.00 due.", "$1,250.00", True),
    ("Invoice total $1,250.00 due.", "$250", False),    # digits inside another number don't count
    ("Order 88412 shipped.", "$84", False),
])
def test_amount_must_appear_as_a_number(body, amount, kept):
    e = email(body=body)
    x = check(Classification(category="invoice_payment", urgency="later", extracted=Extracted(amount=amount)), e)
    assert (x.classification.extracted.amount == amount) is kept


def test_spam_never_needs_a_reply():
    out = check(Classification(category="spam_marketing", urgency="later", needs_reply=True), LEAK)
    assert out.classification.needs_reply is False


# ---------- keyword classifier ----------

@pytest.mark.parametrize("subject,body,category,urgency", [
    ("Water coming through kitchen ceiling!!", "Water is coming through, can someone come?", "new_job", "now"),
    ("Still not fixed", "I'm disappointed with the repair.", "complaint", "today"),
    ("Invoice INV-2291", "We've paid invoice INV-2291 ($640.00).", "invoice_payment", "later"),
    ("More leads", "SEO services and backlinks. Unsubscribe here.", "spam_marketing", "later"),
    ("Quote please", "Could you give me an estimate for a fan?", "quote_request", "later"),
    ("Hello", "Just saying hi.", "other", "later"),
])
def test_keyword_classifier(subject, body, category, urgency):
    c = KeywordClassifier().classify(email(subject=subject, body=body), RULES)
    out = check(c, email(subject=subject, body=body)).classification
    assert (out.category, out.urgency) == (category, urgency)


def test_keyword_extracts_invoice_and_amount():
    e = email(subject="Invoice INV-2291", body="We've paid invoice INV-2291 ($640.00) today.")
    x = check(KeywordClassifier().classify(e, RULES), e).classification.extracted
    assert x.invoice_number == "INV-2291" and x.amount == "$640.00"


# ---------- Claude classifier (fake client) ----------

class FakeMessages:
    def __init__(self, result=None, stop="end_turn", error=None):
        self.result, self.stop, self.error, self.calls = result, stop, error, []

    def parse(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        r = SimpleNamespace(parsed_output=self.result, stop_reason=self.stop, model=kw["model"],
                            usage=SimpleNamespace(input_tokens=700, output_tokens=200))
        r._request_id = "req_x"
        return r


def answer(**kw):
    base = dict(category="new_job", urgency="now", urgency_evidence="Water is coming through our kitchen ceiling",
                extracted=_Extracted(name="Maria Lopez", phone="555-201-3344", address="14 Elm Street",
                                     job_type="leak", invoice_number=None, amount=None),
                needs_reply=True, language="en", summary="Maria has an active leak.")
    base.update(kw)
    return _Answer(**base)


def claude(messages, model="claude-opus-5-5"):
    return ClaudeClassifier(model=model, client=SimpleNamespace(beta=SimpleNamespace(messages=messages)))


def test_claude_classifier_maps_answer_and_request():
    fake = FakeMessages(answer())
    c = claude(fake).classify(LEAK, RULES)
    assert (c.category, c.urgency, c.extracted.phone) == ("new_job", "now", "555-201-3344")
    assert c.meta["request_id"] == "req_x" and c.meta["input_tokens"] == 700
    call = fake.calls[0]
    assert call["fallbacks"] == "default" and call["output_format"] is _Answer
    assert "<email>\nSubject: Water coming through" in call["messages"][0]["content"]
    assert "not instructions" in call["system"] and "complaint:" in call["system"]
    assert "fallbacks" not in (lambda f: (claude(f, "claude-haiku-5-5").classify(LEAK, RULES), f.calls[0])[1])(
        FakeMessages(answer()))


@pytest.mark.parametrize("stop,result,match", [("refusal", None, "declined"), ("max_tokens", None, "cut off"),
                                               ("end_turn", None, "no usable")])
def test_claude_bad_responses(stop, result, match):
    with pytest.raises(ClassifierError, match=match):
        claude(FakeMessages(result, stop)).classify(LEAK, RULES)


def test_claude_api_errors():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.InternalServerError("boom", response=httpx.Response(500, request=req), body=None)
    with pytest.raises(ClassifierError, match="500"):
        claude(FakeMessages(error=err)).classify(LEAK, RULES)


def test_prompt_covers_every_category_and_cost():
    s = system_prompt(RULES)
    for cat in RULES.categories:
        assert f"- {cat}:" in s
    assert cost_usd("claude-opus-5-5", 1_000_000, 0) == 4.0 and cost_usd("x", 1, 1) is None


# ---------- drafts ----------

def test_template_drafts():
    d = TemplateDrafter()
    urgent = d.draft(LEAK, Classification(category="new_job", urgency="now"), RULES)
    assert "(555) 010-2030" in urgent.body and urgent.subject.startswith("Re: ")
    complaint = d.draft(email(), Classification(category="complaint", urgency="today"), RULES)
    assert "manager" in complaint.body
    for draft in (urgent, complaint):
        assert check_draft(draft, LEAK, RULES).warnings == []


def test_check_draft_flags_promises_prices_and_concessions():
    bad = Draft(subject="Re: leak", body="Hi [Name], we'll send a technician within 30 minutes. "
                                         "The callout is $95 and we'll give you a 10% discount.")
    w = check_draft(bad, LEAK, RULES).warnings
    assert any("$95" in x for x in w) and any("Promises a time" in x for x in w)
    assert any("discount" in x for x in w) and any("placeholder" in x for x in w)
    # repeating the customer's own amount, or their word "refund", is fine
    e = email(body="I want a refund of the $380 I paid.")
    ok = check_draft(Draft(subject="Re", body="We have your refund request about the $380."), e, RULES)
    assert ok.warnings == []


def test_claude_drafter_request():
    fake = FakeMessages(_DraftOut(subject=" Re: leak ", body=" Hi Maria, call us. "))
    d = ClaudeDrafter(client=SimpleNamespace(beta=SimpleNamespace(messages=fake))).draft(
        LEAK, Classification(category="new_job", urgency="now", summary="leak"), RULES)
    assert d.subject == "Re: leak"
    call = fake.calls[0]
    assert "urgent" in call["messages"][0]["content"] and "Never promise a time" in call["system"]


# ---------- pipeline ----------

def test_full_run_on_the_sample_inbox(tmp_path):
    emails = load_fixture(ROOT / "fixtures" / "sample_inbox.jsonl", NOW)
    p = make(tmp_path, emails)
    assert len(p.poll()) == 7
    rows = {r["message_id"]: r for r in p.store.emails()}
    assert all(r["state"] == "done" for r in rows.values())
    leak = rows["m1"]
    assert (leak["category"], leak["urgency"], leak["assignee"]) == ("new_job", "now", "office")
    assert leak["deadline"] == pytest.approx(leak["received_at"] + 3600)
    assert p.mailbox.labels["m1"] == ["AI/New job", "AI/Urgent"]
    assert rows["m5"]["assignee"] is None and rows["m5"]["follow_status"] == "none"
    assert len(p.mailbox.drafts) == 4 and len(p.sheet.rows) == 7          # drafts: leak, quote, complaint, outlet
    assert p.mailbox.processed == {e.message_id for e in emails}
    # alerts: the leak (urgent) and the complaint
    assert len([t for t in p.notifier.sent if t.startswith(("Urgent", "Complaint"))]) == 2
    spam_row = next(r for r in p.sheet.rows.values() if r["category"] == "Marketing")
    assert spam_row["status"] == "no reply needed" and spam_row["assigned_to"] == ""


def test_poll_twice_handles_each_email_once(tmp_path):
    p = make(tmp_path, [LEAK])
    p.poll()
    p.poll()
    assert len(p.store.emails()) == 1 and len(p.mailbox.drafts) == 1 and len(p.sheet.rows) == 1


def test_lost_processed_mark_is_repaired_without_redoing_work(tmp_path):
    p = make(tmp_path, [LEAK])
    p.poll()
    p.mailbox.processed.clear()   # e.g. the label was removed by hand, or the mark was lost
    p.poll()
    assert "m1" in p.mailbox.processed and len(p.sheet.rows) == 1 and len(p.mailbox.drafts) == 1


def test_classifier_failure_then_retry(tmp_path):
    fixed = Fixed(RuntimeError("API down"))
    p = make(tmp_path, [LEAK], classifier=fixed)
    p.poll()
    row = p.store.get(1)
    assert row["state"] == "needs_attention" and row["resume_state"] == "received"
    assert "m1" not in p.mailbox.processed          # still eligible to be picked up again
    assert any("needs attention" in t for t in p.notifier.sent)
    p.classifier = KeywordClassifier()
    assert p.retry(1) and p.store.get(1)["state"] == "done"


def test_failure_halfway_through_actions_resumes_without_repeats(tmp_path):
    sheet = FakeSheet(fail_on="append")
    p = make(tmp_path, [LEAK], sheet=sheet)
    p.poll()
    row = p.store.get(1)
    assert row["state"] == "needs_attention" and row["resume_state"] == "classified"
    assert row["actions"]["labels"] is True and row["actions"]["draft_id"] == "draft-1"
    sheet.fail_on = None
    assert p.retry(1)
    assert p.store.get(1)["state"] == "done"
    assert len(p.mailbox.drafts) == 1 and len(sheet.rows) == 1 and p.mailbox.labels["m1"].count("AI/Urgent") == 1


def test_mark_processed_failure_then_retry(tmp_path):
    mb = FakeMailbox([LEAK], fail_on="processed")
    p = make(tmp_path, mailbox=mb)
    p.poll()
    assert p.store.get(1)["resume_state"] == "acted"
    mb.fail_on = None
    p.retry(1)
    assert p.store.get(1)["state"] == "done" and len(p.sheet.rows) == 1


def test_draft_failure_is_a_failure_but_alert_failure_is_not(tmp_path):
    class BrokenDrafter:
        name = "broken"

        def draft(self, *a):
            raise RuntimeError("drafter down")

    class BrokenNotifier:
        name = "broken"

        def notify(self, text):
            raise RuntimeError("slack down")

    p = make(tmp_path, [LEAK], drafter=BrokenDrafter())
    p.poll()
    assert p.store.get(1)["state"] == "needs_attention"
    (tmp_path / "b").mkdir()
    p2 = make(tmp_path / "b", [LEAK], notifier=BrokenNotifier())
    p2.poll()
    row = p2.store.get(1)
    assert row["state"] == "done" and row["actions"]["alerted"] is False
    assert "urgent_alert_failed" in [e["kind"] for e in p2.store.events(1)]


def test_restart_resumes_unfinished_emails(tmp_path):
    p = make(tmp_path, [LEAK, email()])
    for e in p.mailbox.fetch_new():
        p.store.add_email(e)           # saved, then the process "died" before handling them
    again = Pipeline(p.store, p.mailbox, p.sheet, KeywordClassifier(), RULES, TemplateDrafter(), LogNotifier())
    assert again.resume_pending() == [1, 2]
    assert {r["state"] for r in p.store.emails()} == {"done"} and again.resume_pending() == []


def test_parallel_processing_of_one_email_runs_once(tmp_path):
    def slow(e):
        time.sleep(0.2)
        return KeywordClassifier().classify(e, RULES)

    fixed = Fixed(slow)
    p = make(tmp_path, [LEAK], classifier=fixed)
    p.store.add_email(LEAK)
    threads = [threading.Thread(target=p.process, args=(1,)) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert fixed.calls == 1 and len(p.sheet.rows) == 1


def test_transitions_are_enforced(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    eid, _ = s.add_email(LEAK)
    with pytest.raises(InvalidTransition):
        s.transition(eid, "done")
    with pytest.raises(ValueError):
        s.update(eid, password="x")


# ---------- follow-ups ----------

def test_follow_up_replied_overdue_and_alerted_once(tmp_path):
    emails = [email(2, hours_ago=30), email(3, hours_ago=2), email(4, hours_ago=40)]
    p = make(tmp_path, emails)
    p.poll()
    p.mailbox.replies["t4"] = NOW - 3600          # someone answered the oldest one
    assert p.follow_up() == {"replied": 1, "overdue": 1, "open": 1}
    rows = {r["message_id"]: r for r in p.store.emails()}
    assert rows["m2"]["follow_status"] == "overdue" and rows["m4"]["follow_status"] == "replied"
    statuses = sorted(r["status"] for r in p.sheet.rows.values())
    assert statuses == ["open", "overdue", "replied"]
    overdue_alerts = [t for t in p.notifier.sent if t.startswith("Overdue")]
    assert len(overdue_alerts) == 1
    p.follow_up()                                  # second run: no new alert
    assert len([t for t in p.notifier.sent if t.startswith("Overdue")]) == 1
    p.mailbox.replies["t2"] = NOW - 60             # the overdue one gets answered later
    p.follow_up()
    assert p.store.get(rows["m2"]["id"])["follow_status"] == "replied"


def test_follow_up_survives_a_bad_thread(tmp_path):
    p = make(tmp_path, [email(2, hours_ago=30), email(3, hours_ago=30)])
    p.poll()

    def flaky(thread_id, since):
        if thread_id == "t2":
            raise AdapterError("Gmail hiccup")
        return None

    p.mailbox.first_reply_after = flaky
    assert p.follow_up() == {"replied": 0, "overdue": 1, "open": 0}


# ---------- alerts ----------

def test_alert_text_is_defanged():
    row = {"id": 1, "from_name": "<!channel> @here", "from_email": "x@y.com", "category": "complaint",
           "urgency": "today", "classification": {"summary": "<https://evil.example|click>"}, "link": None,
           "deadline_text": "Tue 03 Nov 14:00", "error": None}
    t = urgent_text(row, "Dana", "http://h")
    assert "<!channel>" not in t and "@here" not in t and "<https" not in t
    assert t.startswith("Complaint from") and "unknown error" in attention_text(row, "http://h")


def test_eval_cases_are_well_formed():
    from inbox.eval import load_cases
    from inbox.rules import CATEGORIES, URGENCIES
    cases = load_cases(ROOT / "evals" / "emails.jsonl")
    assert len(cases) == 30
    for c in cases:
        assert c["category"] in CATEGORIES and c["urgency"] in URGENCIES and isinstance(c["needs_reply"], bool)
        for v in c.get("fields", {}).values():
            assert v.lower() in (c["subject"] + " " + c["body"]).lower()   # expected fields really are in the email
    assert {c["category"] for c in cases} == set(CATEGORIES)
    assert sum(c["urgency"] == "now" for c in cases) == 8


def test_labels():
    assert labels_for("complaint", "today") == ["AI/Complaint"]
    assert labels_for("new_job", "now") == ["AI/New job", "AI/Urgent"]
