"""Weekly report: numbers computed in code, a short summary written by Claude, every number checked.

Week = Monday to Sunday in the business's timezone. Sources: the jobs sheet and the inbox log
(the local store). Delivery: a row in the "Weekly report" sheet and a message placed in the
owner's inbox (inserted, not sent). Each week is reported once; a retry finishes what is missing.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import statistics
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import anthropic
from pydantic import BaseModel

from .classifier import FALLBACK_MODELS
from .rules import Rules

log = logging.getLogger("inbox")
CATEGORY_NAMES = {"new_job": "New jobs", "quote_request": "Quote requests", "complaint": "Complaints",
                  "invoice_payment": "Billing", "supplier": "Supplier", "spam_marketing": "Marketing",
                  "other": "Other"}


class Job(BaseModel):
    date: dt.date
    customer: str
    job_type: str = ""
    status: Literal["completed", "scheduled", "cancelled"]
    amount: float = 0
    invoice_number: str = ""
    invoice_status: Literal["paid", "unpaid", ""] = ""
    due_date: dt.date | None = None


class JobsSource(Protocol):
    name: str

    def jobs(self) -> list[Job]: ...


class FakeJobs:
    name = "fake"

    def __init__(self, jobs: list[Job]):
        self._jobs = jobs

    def jobs(self) -> list[Job]:
        return list(self._jobs)


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def money(x: float) -> str:
    return f"${x:,.0f}"


# ---------- metrics (code only) ----------

def compute(start: dt.date, jobs: list[Job], emails: list[dict], tz: ZoneInfo, today: dt.date) -> dict[str, Any]:
    end = start + dt.timedelta(days=7)
    prev = start - dt.timedelta(days=7)
    done = [j for j in jobs if j.status == "completed" and start <= j.date < end]
    done_prev = [j for j in jobs if j.status == "completed" and prev <= j.date < start]
    revenue, revenue_prev = round(sum(j.amount for j in done)), round(sum(j.amount for j in done_prev))
    change = round((revenue - revenue_prev) / revenue_prev * 100) if revenue_prev else None

    local = lambda ts: dt.datetime.fromtimestamp(ts, tz).date()  # noqa: E731
    week_emails = [e for e in emails if e.get("category") and start <= local(e["received_at"]) < end]
    by_cat = {CATEGORY_NAMES[c]: sum(e["category"] == c for e in week_emails)
              for c in CATEGORY_NAMES if any(e["category"] == c for e in week_emails)}
    waits = [(e["replied_at"] - e["received_at"]) / 3600 for e in week_emails if e.get("replied_at")]
    unanswered = [e for e in emails if e.get("follow_status") == "overdue" and local(e["received_at"]) < end]

    overdue = sorted(
        ({"customer": j.customer, "invoice": j.invoice_number, "amount": round(j.amount),
          "days_late": (today - j.due_date).days}
         for j in jobs if j.invoice_status == "unpaid" and j.due_date and j.due_date < today),
        key=lambda x: -x["days_late"])
    return {
        "week_start": start.isoformat(), "week_end": (end - dt.timedelta(days=1)).isoformat(),
        "jobs_completed": len(done), "revenue": revenue, "revenue_previous_week": revenue_prev,
        "revenue_change_pct": change, "average_job_value": round(revenue / len(done)) if done else 0,
        "emails_received": len(week_emails), "emails_by_category": by_cat,
        "complaints": sum(e["category"] == "complaint" for e in week_emails),
        "median_reply_hours": round(statistics.median(waits), 1) if waits else None,
        "replies_measured": len(waits),
        "unanswered_past_deadline": len(unanswered),
        "unanswered_list": [{"from": e.get("from_name") or e["from_email"], "subject": e.get("subject", "")}
                            for e in unanswered][:10],
        "unpaid_overdue_invoices": overdue,
        "unpaid_overdue_total": sum(i["amount"] for i in overdue),
    }


def find_issues(m: dict[str, Any], rules: Rules) -> list[str]:
    cfg = rules.report.issues
    issues = []
    if m["revenue_change_pct"] is not None and m["revenue_change_pct"] <= -cfg.revenue_drop_pct:
        issues.append(f"Revenue fell {abs(m['revenue_change_pct'])}% to {money(m['revenue'])} "
                      f"(from {money(m['revenue_previous_week'])} the week before).")
    if m["complaints"] >= cfg.complaints_at_least:
        issues.append(f"{m['complaints']} complaints this week.")
    late = [i for i in m["unpaid_overdue_invoices"] if i["days_late"] > cfg.invoice_overdue_days]
    for i in late:
        issues.append(f"Invoice {i['invoice']} for {i['customer']} ({money(i['amount'])}) is "
                      f"{i['days_late']} days overdue.")
    n = m["unanswered_past_deadline"]
    if n >= cfg.unanswered_overdue_at_least:
        issues.append(f"{n} customer email{'s are' if n != 1 else ' is'} past {'their' if n != 1 else 'its'} "
                      f"reply deadline.")
    return issues


# ---------- the number check ----------

_NUMBER = re.compile(r"(?<![\w-])[$€£]?\d[\d,]*(?:\.\d+)?%?")


def _numbers_in(value: Any) -> set[float]:
    found: set[float] = set()
    if isinstance(value, bool) or value is None:
        return found
    if isinstance(value, (int, float)):
        found |= {float(value), abs(float(value))}
    elif isinstance(value, str):
        for token in re.findall(r"\d[\d,]*(?:\.\d+)?", value):
            found.add(float(token.replace(",", "")))
    elif isinstance(value, dict):
        for v in value.values():
            found |= _numbers_in(v)
    elif isinstance(value, list):
        for v in value:
            found |= _numbers_in(v)
    return found


def check_numbers(text: str, metrics: dict[str, Any], issues: list[str]) -> list[str]:
    """Every number in the summary must be one the code computed. Returns the ones that aren't."""
    allowed = _numbers_in(metrics) | _numbers_in(issues)
    bad = []
    for token in _NUMBER.findall(text):
        value = float(token.strip("$€£%").replace(",", ""))
        if not any(abs(value - a) < 0.051 for a in allowed):
            bad.append(token)
    return bad


# ---------- the summary ----------

def template_summary(m: dict[str, Any], issues: list[str]) -> str:
    parts = [f"{m['jobs_completed']} jobs were completed for {money(m['revenue'])}"]
    if m["revenue_change_pct"] is not None:
        direction = "up" if m["revenue_change_pct"] >= 0 else "down"
        parts[0] += f", {direction} {abs(m['revenue_change_pct'])}% on the week before"
    parts[0] += "."
    parts.append(f"The inbox received {m['emails_received']} emails, including {m['complaints']} complaints.")
    if m["median_reply_hours"] is not None:
        parts.append(f"The median first reply took {m['median_reply_hours']} hours.")
    parts.append(f"{len(issues)} issues need attention." if issues else "Nothing needs attention.")
    return " ".join(parts)


def summary_system(rules: Rules) -> str:
    return (f"You write the weekly summary for the owner of {rules.business}.\n"
            "Rules:\n- 3 to 5 plain sentences. No headings, lists or markdown.\n"
            "- Use only numbers that appear in the data, written exactly as there (digits, not words). "
            "Do not calculate new numbers, totals or percentages.\n"
            "- Lead with what changed most, then anything that needs action.\n"
            "- Do not guess causes the data does not show.")


def summary_user(m: dict[str, Any], issues: list[str]) -> str:
    return f"Data for the week:\n{json.dumps(m, indent=1)}\n\nIssues found by the rules:\n" + (
        "\n".join(f"- {i}" for i in issues) or "- none")


class _SummaryOut(BaseModel):
    summary: str


class ClaudeSummary:
    def __init__(self, api_key: str | None = None, model: str = "claude-opus-5-5", effort: str = "low",
                 client: Any = None):
        self.model, self.effort, self.name = model, effort, f"claude:{model}"
        self.client = client or anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)

    def write(self, m: dict[str, Any], issues: list[str], rules: Rules) -> str:
        system = summary_system(rules)
        user = summary_user(m, issues)
        kwargs: dict[str, Any] = {}
        if self.model in FALLBACK_MODELS:
            kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        r = self.client.beta.messages.parse(
            model=self.model, max_tokens=3000, system=system, messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort}, output_format=_SummaryOut, **kwargs)
        if r.stop_reason in ("refusal", "max_tokens") or r.parsed_output is None:
            raise RuntimeError(f"Claude did not return a summary ({r.stop_reason})")
        return r.parsed_output.summary.strip()


# ---------- rendering ----------

def render(m: dict[str, Any], issues: list[str], summary: str, summary_note: str | None,
           business: str) -> tuple[str, str, str]:
    """Returns (subject, plain text, HTML)."""
    start = dt.date.fromisoformat(m["week_start"])
    week = f"{start:%d %b} to {dt.date.fromisoformat(m['week_end']):%d %b %Y}"
    subject = f"Weekly report, {week}: " + (f"{len(issues)} issue{'s' * (len(issues) != 1)}" if issues
                                            else "no issues")
    change = "–" if m["revenue_change_pct"] is None else f"{m['revenue_change_pct']:+d}%"
    rows = [
        ("Jobs completed", str(m["jobs_completed"])),
        ("Revenue", f"{money(m['revenue'])} ({change} vs {money(m['revenue_previous_week'])})"),
        ("Average job value", money(m["average_job_value"])),
        ("Emails received", str(m["emails_received"])),
        ("Complaints", str(m["complaints"])),
        ("Median first reply", "–" if m["median_reply_hours"] is None else f"{m['median_reply_hours']} hours"),
        ("Unanswered past deadline", str(m["unanswered_past_deadline"])),
        ("Unpaid overdue invoices", f"{len(m['unpaid_overdue_invoices'])} ({money(m['unpaid_overdue_total'])})"),
    ]
    lines = [f"{business}: week {week}", ""]
    if summary:
        lines += [summary, ""]
    if summary_note:
        lines += [f"Note: {summary_note}", ""]
    lines += ["Needs attention:"] + [f"- {i}" for i in issues] + [""] if issues else ["Nothing needs attention.", ""]
    lines += [f"{k}: {v}" for k, v in rows]
    if m["emails_by_category"]:
        lines += ["", "Emails by type: " + ", ".join(f"{k} {v}" for k, v in m["emails_by_category"].items())]
    text = "\n".join(lines)

    import html as h
    table = "".join(f"<tr><td style='padding:4px 12px 4px 0;color:#555'>{h.escape(k)}</td>"
                    f"<td style='padding:4px 0'><b>{h.escape(v)}</b></td></tr>" for k, v in rows)
    issue_html = ("<p style='margin:16px 0 4px'><b>Needs attention</b></p><ul>" +
                  "".join(f"<li>{h.escape(i)}</li>" for i in issues) + "</ul>") if issues else \
        "<p>Nothing needs attention.</p>"
    html_body = (f"<div style='font-family:system-ui,sans-serif;font-size:15px;max-width:560px'>"
                 f"<p style='color:#555'>{h.escape(business)} · week {h.escape(week)}</p>"
                 + (f"<p>{h.escape(summary)}</p>" if summary else "")
                 + (f"<p style='color:#8a5a00'>Note: {h.escape(summary_note)}</p>" if summary_note else "")
                 + issue_html + f"<table style='border-collapse:collapse'>{table}</table></div>")
    return subject, text, html_body


REPORT_COLUMNS = ["week", "jobs_completed", "revenue", "change_pct", "average_job", "emails", "complaints",
                  "median_reply_hours", "unanswered_past_deadline", "overdue_invoices", "overdue_total",
                  "issues", "summary_checked"]


def sheet_row(m: dict[str, Any], issues: list[str], summary_ok: bool) -> dict[str, Any]:
    return {"week": m["week_start"], "jobs_completed": m["jobs_completed"], "revenue": m["revenue"],
            "change_pct": "" if m["revenue_change_pct"] is None else m["revenue_change_pct"],
            "average_job": m["average_job_value"], "emails": m["emails_received"], "complaints": m["complaints"],
            "median_reply_hours": "" if m["median_reply_hours"] is None else m["median_reply_hours"],
            "unanswered_past_deadline": m["unanswered_past_deadline"],
            "overdue_invoices": len(m["unpaid_overdue_invoices"]), "overdue_total": m["unpaid_overdue_total"],
            "issues": " | ".join(issues), "summary_checked": "yes" if summary_ok else "replaced (number mismatch)"}


# ---------- scheduling ----------

def due_week(now_local: dt.datetime, rules: Rules) -> dt.date | None:
    """The week whose report is due now (the previous Monday-Sunday), or None before send time."""
    days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    send_day = days.index(rules.report.send_on)
    h, m = map(int, rules.report.send_at.split(":"))
    this_week = week_start(now_local.date())
    send_time = dt.datetime.combine(this_week + dt.timedelta(days=send_day), dt.time(h, m), now_local.tzinfo)
    if now_local < send_time:
        return None
    return this_week - dt.timedelta(days=7)
