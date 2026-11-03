import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from inbox.adapters import FakeSheet
from inbox.report import (ClaudeSummary, FakeJobs, Job, _SummaryOut, check_numbers, compute, due_week, find_issues,
                          render, template_summary, week_start)
from inbox.reporting import FakeSink, Reporter
from inbox.rules import load_rules
from inbox.sample_jobs import sample_jobs
from inbox.store import Store

ROOT = Path(__file__).resolve().parent.parent
RULES = load_rules(ROOT / "routing.yaml")
TZ = ZoneInfo(RULES.timezone)
TODAY = dt.date(2026, 11, 3)                      # a Tuesday
LAST_WEEK = dt.date(2026, 10, 26)


def ts(day: dt.date, hour=10) -> float:
    return dt.datetime.combine(day, dt.time(hour), TZ).timestamp()


EMAILS = [
    {"received_at": ts(dt.date(2026, 10, 27)), "category": "new_job", "follow_status": "replied",
     "replied_at": ts(dt.date(2026, 10, 27), 12), "from_name": "A", "from_email": "a@x", "subject": "s"},
    {"received_at": ts(dt.date(2026, 10, 28)), "category": "complaint", "follow_status": "replied",
     "replied_at": ts(dt.date(2026, 10, 28), 15), "from_name": "B", "from_email": "b@x", "subject": "s"},
    {"received_at": ts(dt.date(2026, 10, 29)), "category": "complaint", "follow_status": "overdue",
     "replied_at": None, "from_name": "C", "from_email": "c@x", "subject": "Still broken"},
    {"received_at": ts(dt.date(2026, 10, 30)), "category": "spam_marketing", "follow_status": "none",
     "replied_at": None, "from_name": "D", "from_email": "d@x", "subject": "s"},
    {"received_at": ts(dt.date(2026, 11, 2)), "category": "new_job", "follow_status": "open",   # next week
     "replied_at": None, "from_name": "E", "from_email": "e@x", "subject": "s"},
]


def metrics(jobs=None, emails=EMAILS):
    return compute(LAST_WEEK, jobs if jobs is not None else sample_jobs(TODAY), emails, TZ, TODAY)


# ---------- metrics and issues ----------

def test_metrics_from_sample_data():
    m = metrics()
    assert (m["jobs_completed"], m["revenue"], m["revenue_previous_week"]) == (5, 2040, 4120)
    assert m["revenue_change_pct"] == -50 and m["average_job_value"] == 408
    assert m["emails_received"] == 4 and m["complaints"] == 2
    assert m["emails_by_category"] == {"New jobs": 1, "Complaints": 2, "Marketing": 1}
    assert m["median_reply_hours"] == 3.5 and m["replies_measured"] == 2        # 2h and 5h
    assert m["unanswered_past_deadline"] == 1 and m["unanswered_list"][0]["subject"] == "Still broken"
    assert [i["days_late"] for i in m["unpaid_overdue_invoices"]] == [26, 19, 3]
    assert m["unpaid_overdue_total"] == 1310


def test_planted_issues_are_found_and_the_small_one_is_not():
    issues = find_issues(metrics(), RULES)
    assert issues[0].startswith("Revenue fell 50%")
    assert sum("overdue" in i and "Invoice" in i for i in issues) == 2          # 26 and 19 days, not 3
    assert not any("3 days overdue" in i for i in issues)
    assert "1 customer email is past its reply deadline." in issues
    assert not any("complaints this week" in i for i in issues)                  # 2 < 3


def test_no_previous_week_and_no_jobs():
    m = metrics(jobs=[], emails=[])
    assert m["revenue_change_pct"] is None and m["average_job_value"] == 0 and m["median_reply_hours"] is None
    assert find_issues(m, RULES) == []
    assert "Nothing needs attention" in template_summary(m, [])


def test_scheduled_and_cancelled_jobs_do_not_count():
    jobs = [Job(date=LAST_WEEK, customer="X", status="scheduled", amount=900),
            Job(date=LAST_WEEK, customer="Y", status="cancelled", amount=500),
            Job(date=LAST_WEEK, customer="Z", status="completed", amount=300)]
    m = metrics(jobs=jobs, emails=[])
    assert m["jobs_completed"] == 1 and m["revenue"] == 300


# ---------- the number check ----------

def test_number_check_accepts_real_numbers_and_catches_invented_ones():
    m = metrics()
    issues = find_issues(m, RULES)
    good = ("Revenue fell 50% to $2,040 from $4,120 across 5 jobs, and 2 invoices worth $900 are badly late. "
            "Invoice INV-2396 is 26 days overdue. The median first reply took 3.5 hours.")
    assert check_numbers(good, m, issues) == ["$900"]           # 640 + 260 is a new number: not allowed
    assert check_numbers(good.replace(", and 2 invoices worth $900 are badly late", ""), m, issues) == []
    assert check_numbers("Revenue fell 48% to $2,100.", m, issues) == ["48%", "$2,100"]


# ---------- the reporter ----------

class FixedSummary:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.calls = text, error, 0

    def write(self, m, issues, rules):
        self.calls += 1
        if self.error:
            raise self.error
        return self.text


def reporter(tmp_path, summary=None, sink=None, sheet=None):
    store = Store(str(tmp_path / "r.db"))
    return Reporter(store, FakeJobs(sample_jobs(TODAY)), sheet or FakeSheet(), sink or FakeSink(), RULES, summary)


def test_report_runs_once_per_week(tmp_path):
    summary = FixedSummary("Revenue fell 50% to $2,040. Two invoices are overdue.")
    r = reporter(tmp_path, summary)
    first = r.run(LAST_WEEK, TODAY)
    second = r.run(LAST_WEEK, TODAY)
    assert first["message_id"] == second["message_id"] == "report-1"
    assert len(r.sink.delivered) == 1 and len(r.sheet.rows) == 1 and summary.calls == 1
    d = r.sink.delivered[0]
    assert d["to"] == "owner@harbor.example" and d["subject"].endswith("3 issues")
    assert "Revenue fell 50% to $2,040. Two invoices are overdue." in d["text"]
    assert r.sheet.rows["row-2"]["summary_checked"] == "yes"


def test_summary_with_an_invented_number_is_replaced_and_flagged(tmp_path):
    r = reporter(tmp_path, FixedSummary("Revenue fell 47% this week, the worst in 6 months."))
    out = r.run(LAST_WEEK, TODAY)
    assert out["summary_ok"] == 0 and "47%" in out["summary_note"]
    text = r.sink.delivered[0]["text"]
    assert "47%" not in text.split("Note:")[0]                     # the bad summary is not shown
    assert text.startswith("Harbor Home Services") and "5 jobs were completed" in text
    assert r.sheet.rows["row-2"]["summary_checked"].startswith("replaced")


def test_summary_failure_still_delivers_the_figures(tmp_path):
    r = reporter(tmp_path, FixedSummary(error=RuntimeError("API down")))
    out = r.run(LAST_WEEK, TODAY)
    assert out["message_id"] and "could not be written" in out["summary_note"]


def test_failed_delivery_retries_without_repeating_the_sheet_row(tmp_path):
    sink = FakeSink(fail=True)
    r = reporter(tmp_path, sink=sink)
    out = r.run(LAST_WEEK, TODAY)
    assert out["message_id"] is None and "fake delivery failure" in out["error"] and len(r.sheet.rows) == 1
    sink.fail = False
    out = r.run(LAST_WEEK, TODAY)
    assert out["message_id"] == "report-1" and out["error"] is None and len(r.sheet.rows) == 1


def test_html_is_escaped():
    m = metrics()
    m["unpaid_overdue_invoices"] = []
    _, _, html = render(m, ["<script>alert(1)</script>"], "Fine & <b>dandy</b>", None, "Harbor <Home>")
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;b&gt;dandy" in html


def test_claude_summary_request():
    calls = []

    class Msgs:
        def parse(self, **kw):
            calls.append(kw)
            return SimpleNamespace(parsed_output=_SummaryOut(summary=" ok "), stop_reason="end_turn")

    s = ClaudeSummary(client=SimpleNamespace(beta=SimpleNamespace(messages=Msgs())))
    assert s.write(metrics(), ["x"], RULES) == "ok"
    assert "Do not calculate new numbers" in calls[0]["system"] and '"revenue": 2040' in calls[0]["messages"][0]["content"]


# ---------- schedule ----------

@pytest.mark.parametrize("now,expected", [
    (dt.datetime(2026, 11, 2, 7, 59, tzinfo=TZ), None),                       # Monday before 08:00
    (dt.datetime(2026, 11, 2, 8, 0, tzinfo=TZ), dt.date(2026, 10, 26)),       # Monday 08:00
    (dt.datetime(2026, 11, 5, 15, 0, tzinfo=TZ), dt.date(2026, 10, 26)),      # later that week: still due
    (dt.datetime(2026, 11, 8, 23, 0, tzinfo=TZ), dt.date(2026, 10, 26)),      # Sunday
])
def test_due_week(now, expected):
    assert due_week(now, RULES) == expected


def test_week_start():
    assert week_start(dt.date(2026, 11, 8)) == dt.date(2026, 11, 2)
    assert week_start(dt.date(2026, 11, 2)) == dt.date(2026, 11, 2)
