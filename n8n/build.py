"""Build the n8n workflows from routing.yaml, the Python prompts and n8n/src/logic.js.

    .venv/bin/python n8n/build.py                              # n8n/inbox-triage.json, weekly-report.json, error-alert.json
    .venv/bin/python n8n/build.py --sheet-id <ID> --query "label:Demo"

Rebuild after changing routing.yaml or the prompts, then re-import in n8n.
Checked against n8n 2.46.4 (Schedule Trigger 1.2, Code 2, HTTP Request 4.2, IF 2.2).
All Gmail and Sheets calls use one Google OAuth2 credential; nothing in these workflows sends email.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inbox.classifier import FALLBACK_MODELS, system_prompt  # noqa: E402
from inbox.drafts import GOALS, ClaudeDrafter  # noqa: E402
from inbox.google_api import DEFAULT_QUERY, JOB_COLUMNS, LOG_COLUMNS, PROCESSED_LABEL, REPORT_LABEL  # noqa: E402
from inbox.report import REPORT_COLUMNS, summary_system  # noqa: E402
from inbox.rules import load_rules  # noqa: E402
from inbox.service import LABELS  # noqa: E402

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
ANTHROPIC_HEADERS = {"anthropic-version": "2026-10-21", "anthropic-beta": "server-side-fallback-2026-07-01"}
GOOGLE_CRED = {"googleOAuth2Api": {"id": "googleOAuth2Inbox", "name": "Google (Gmail + Sheets)"}}
ANTHROPIC_CRED = {"httpHeaderAuth": {"id": "anthropicApiKey1", "name": "Anthropic API key"}}
NULLABLE = {"type": ["string", "null"]}
CLASSIFY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["category", "urgency", "urgency_evidence", "extracted", "needs_reply", "language", "summary"],
    "properties": {
        "category": {"type": "string", "enum": ["new_job", "quote_request", "complaint", "invoice_payment", "supplier",
                                                "spam_marketing", "other"]},
        "urgency": {"type": "string", "enum": ["now", "today", "later"]},
        "urgency_evidence": {"type": "string"},
        "extracted": {"type": "object", "additionalProperties": False,
                      "required": ["name", "phone", "address", "job_type", "invoice_number", "amount"],
                      "properties": {k: NULLABLE for k in ("name", "phone", "address", "job_type", "invoice_number",
                                                           "amount")}},
        "needs_reply": {"type": "boolean"}, "language": {"type": "string"}, "summary": {"type": "string"},
    },
}
DRAFT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["subject", "body"],
                "properties": {"subject": {"type": "string"}, "body": {"type": "string"}}}


def config(rules_path: Path, model: str, effort: str) -> dict:
    rules = load_rules(rules_path)
    return {
        "business": rules.business, "timezone": rules.timezone, "model": model, "effort": effort,
        "fallbackModels": sorted(FALLBACK_MODELS),
        "classifySystem": system_prompt(rules), "classifySchema": CLASSIFY_SCHEMA,
        "draftSystem": ClaudeDrafter(model=model, client=object()).system(rules), "draftSchema": DRAFT_SCHEMA,
        "draftGoals": GOALS, "summarySystem": summary_system(rules),
        "rules": [r.model_dump() for r in rules.rules],
        "people": {k: v.model_dump() for k, v in rules.people.items()},
        "reply": rules.reply.model_dump(), "report": rules.report.model_dump(),
        "reportTo": rules.report_recipient(),
        "labels": LABELS, "logColumns": LOG_COLUMNS, "jobColumns": JOB_COLUMNS, "reportColumns": REPORT_COLUMNS,
    }


class Builder:
    def __init__(self, logic: str, google_cred: dict):
        self.logic, self.google_cred, self.nodes, self.connections = logic, google_cred, [], {}
        self.x = 0

    def _add(self, node: dict, y: int = 300) -> str:
        node.setdefault("position", [self.x, y])
        self.x += 220
        self.nodes.append(node)
        return node["name"]

    def code(self, name, snippet, each=True, y=300):
        params = {"jsCode": self.logic + "\n// ---- this node ----\n" + snippet.strip() + "\n"}
        if each:
            params["mode"] = "runOnceForEachItem"
        return self._add({"parameters": params, "name": name, "type": "n8n-nodes-base.code", "typeVersion": 2}, y)

    def http(self, name, method, url, body=None, query=None, google=True, headers=None, y=300, retries=True):
        params: dict = {"method": method, "url": url, "options": {"timeout": 120000}}
        if google and next(iter(self.google_cred)) == "httpHeaderAuth":   # test builds: a bearer token
            params.update(authentication="genericCredentialType", genericAuthType="httpHeaderAuth")
        elif google:
            params.update(authentication="predefinedCredentialType", nodeCredentialType=next(iter(self.google_cred)))
        else:
            params.update(authentication="genericCredentialType", genericAuthType="httpHeaderAuth")
        if headers:
            params["sendHeaders"] = True
            params["headerParameters"] = {"parameters": [{"name": k, "value": v} for k, v in headers.items()]}
        if query:
            params["sendQuery"] = True
            params["queryParameters"] = {"parameters": [{"name": k, "value": v} for k, v in query.items()]}
        if body is not None:
            params.update(sendBody=True, specifyBody="json", jsonBody=body)
        node = {"parameters": params, "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
                "credentials": self.google_cred if google else ANTHROPIC_CRED}
        if retries:  # 429 / 5xx / network: 3 tries; after that the run fails and the error workflow alerts
            node.update(retryOnFail=True, maxTries=3, waitBetweenTries=3000)
        return self._add(node, y)

    def claude(self, name, body_expr, y=300):
        return self.http(name, "POST", "https://api.anthropic.com/v1/messages", body_expr, google=False,
                         headers=ANTHROPIC_HEADERS, y=y)

    def if_true(self, name, expr, y=300):
        cond = {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                               "conditions": [{"id": f"{name}-0", "leftValue": expr, "rightValue": "",
                                               "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                               "combinator": "and"}, "options": {}}
        return self._add({"parameters": cond, "name": name, "type": "n8n-nodes-base.if", "typeVersion": 2.2}, y)

    def link(self, src, dst, output=0):
        outs = self.connections.setdefault(src, {"main": []})["main"]
        while len(outs) <= output:
            outs.append([])
        outs[output].append({"node": dst, "type": "main", "index": 0})

    def chain(self, *names):
        for a, b in zip(names, names[1:]):
            self.link(a, b)


def inbox_triage(logic: str, google_cred: dict, query: str, sheet_id: str, alert_url: str) -> dict:
    b = Builder(logic, google_cred)
    q = f"{query} -label:{PROCESSED_LABEL.replace('/', '-')} -label:{REPORT_LABEL.replace('/', '-')}"
    trigger = b._add({"parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 2}]}},
                      "name": "Every 2 minutes", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2})
    labels = b.http("Gmail: labels", "GET", f"{GMAIL}/labels")
    listing = b.http("Gmail: new messages", "GET", f"{GMAIL}/messages", query={"q": q, "maxResults": "20"})
    split = b.code("One item per email", """
const known = Object.fromEntries(($('Gmail: labels').first().json.labels || []).map(l => [l.name, l.id]));
const needed = [...Object.values(CONFIG.labels).map(v => 'AI/' + v), 'AI/Urgent', 'AI/Processed'];
const missing = needed.filter(n => !known[n]);
if (missing.length) throw new Error('Create these Gmail labels first (python -m inbox.google_setup labels): ' + missing.join(', '));
return ($input.first().json.messages || []).map(m => ({ json: { id: m.id } }));""", each=False)
    get = b.http("Gmail: get message", "GET", f"={GMAIL}/messages/{{{{ $json.id }}}}", query={"format": "full"})
    parse = b.code("Parse email", """
const email = parseMessage($json);
return { json: { email, request: buildClassifyRequest(email) } };""")
    classify = b.claude("Claude: classify", "={{ JSON.stringify($json.request) }}")
    tri = b.code("Check and route", f"""
const email = $('Parse email').item.json.email;
const t = triage(email, claudeJson($json));
const known = Object.fromEntries($('Gmail: labels').first().json.labels.map(l => [l.name, l.id]));
const alertUrl = {json.dumps(alert_url)};
return {{ json: {{ email, triage: t, label_ids: t.labels.map(n => known[n]), processed_id: known['AI/Processed'],
  row: sheetRow(email, t, t.wantsDraft ? 'saved' : ''), wants_draft: t.wantsDraft,
  draft_request: t.wantsDraft ? buildDraftRequest(email, t.c) : null,
  alert: !!t.rule.alert && alertUrl !== '', alert_url: alertUrl, alert_text: alertText(email, t) }} }};""")
    add_labels = b.http("Gmail: add labels", "POST", f"={GMAIL}/messages/{{{{ $json.email.message_id }}}}/modify",
                        "={{ JSON.stringify({ addLabelIds: $json.label_ids }) }}")
    append = b.http("Sheets: log row", "POST",
                    f"{SHEETS}/{sheet_id}/values/'Inbox log'!A1:append",
                    "={{ JSON.stringify({ values: [$('Check and route').item.json.row] }) }}",
                    query={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"})
    wants = b.if_true("Draft needed?", "={{ $('Check and route').item.json.wants_draft }}")
    draft = b.claude("Claude: draft reply", "={{ JSON.stringify($('Check and route').item.json.draft_request) }}", y=140)
    check = b.code("Check draft", """
const r = $('Check and route').item.json;
const d = claudeJson($json);
const draft = { subject: d.subject.trim(), body: d.body.trim() };
return { json: { ...r, draft, draft_warnings: checkDraft(draft, r.email), raw: rawReply(r.email, draft.subject, draft.body) } };""", y=140)
    save = b.http("Gmail: save draft", "POST", f"{GMAIL}/drafts",
                  "={{ JSON.stringify({ message: { raw: $json.raw, threadId: $json.email.thread_id } }) }}", y=140)
    alert_q = b.if_true("Alert?", "={{ $('Check and route').item.json.alert }}")
    slack = b.http("Slack: alert", "POST", "={{ $('Check and route').item.json.alert_url }}",
                   "={{ JSON.stringify({ text: $('Check and route').item.json.alert_text }) }}", y=140, retries=False)
    slack_node = b.nodes[-1]
    slack_node["parameters"].pop("authentication", None)
    slack_node["parameters"].pop("genericAuthType", None)
    slack_node.pop("credentials", None)
    slack_node["onError"] = "continueRegularOutput"   # a failed alert never blocks the email
    done = b.http("Gmail: mark processed", "POST",
                  f"={GMAIL}/messages/{{{{ $('Check and route').item.json.email.message_id }}}}/modify",
                  "={{ JSON.stringify({ addLabelIds: [$('Check and route').item.json.processed_id] }) }}")
    b.chain(trigger, labels, listing, split, get, parse, classify, tri, add_labels, append, wants)
    b.link(wants, draft, 0)
    b.chain(draft, check, save, alert_q)
    b.link(wants, alert_q, 1)
    b.link(alert_q, slack, 0)
    b.link(slack, done)
    b.link(alert_q, done, 1)
    return {"name": "AI inbox ops: triage", "nodes": b.nodes, "connections": b.connections,
            "settings": {"executionOrder": "v1", "timezone": json.loads(logic_config_tz(logic))},
            "pinData": {}, "active": False, "tags": []}


def weekly_report(logic: str, google_cred: dict, sheet_id: str) -> dict:
    b = Builder(logic, google_cred)
    trigger = b._add({"parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": "0 8 * * 1"}]}},
                      "name": "Mondays 08:00", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2})
    labels = b.http("Gmail: labels", "GET", f"{GMAIL}/labels")
    week = b.code("Week to report", """
const today = isoLocal(Date.now() / 1000).slice(0, 10);       // today in the business's timezone
const lastWeek = new Date(weekStartOf(today) + 'T00:00:00Z');
lastWeek.setUTCDate(lastWeek.getUTCDate() - 7);               // the previous Monday-Sunday week
const reportLabel = ($input.first().json.labels || []).find(l => l.name === 'AI/Report');
if (!reportLabel) throw new Error('Create the Gmail label AI/Report first (python -m inbox.google_setup labels)');
return [{ json: { today, week_start: lastWeek.toISOString().slice(0, 10), report_label_id: reportLabel.id } }];""",
                  each=False)
    jobs = b.http("Sheets: jobs", "GET", f"{SHEETS}/{sheet_id}/values/'Jobs'!A2:H")
    log = b.http("Sheets: inbox log", "GET", f"{SHEETS}/{sheet_id}/values/'Inbox log'!A2:M")
    compute = b.code("Compute figures", """
const w = $('Week to report').first().json;
const toDicts = (rows, cols) => (rows || []).map(r => Object.fromEntries(cols.map((c, i) => [c, r[i] ?? ''])));
const jobs = parseJobs(toDicts($('Sheets: jobs').first().json.values, CONFIG.jobColumns));
const emails = logToEmails(toDicts($input.first().json.values, CONFIG.logColumns));
const metrics = computeMetrics(w.week_start, jobs, emails, w.today);
const issues = findIssues(metrics);
return [{ json: { metrics, issues, request: buildSummaryRequest(metrics, issues) } }];""", each=False)
    summary = b.claude("Claude: summary", "={{ JSON.stringify($json.request) }}")
    summary_node = b.nodes[-1]
    summary_node["onError"] = "continueRegularOutput"   # the figures still go out if the summary fails
    render = b.code("Check numbers and render", """
const { metrics, issues } = $('Compute figures').first().json;
let summary = templateSummary(metrics, issues), ok = true, note = null;
try {
  const written = claudeJson($input.first().json).summary.trim();
  const bad = checkNumbers(written, metrics, issues);
  if (bad.length) { ok = false; note = 'the AI summary was replaced because it contained numbers not in the data: ' + bad.join(', '); }
  else summary = written;
} catch (e) { note = 'the AI summary could not be written this week; the figures below are complete'; }
const r = renderReport(metrics, issues, summary, note);
return [{ json: { raw: rawReport(CONFIG.reportTo, r.subject, r.text, r.html), row: reportRow(metrics, issues, ok), subject: r.subject } }];""",
                    each=False)
    deliver = b.http("Gmail: place in owner's inbox", "POST", f"{GMAIL}/messages",
                     "={{ JSON.stringify({ raw: $json.raw, labelIds: ['INBOX', 'UNREAD', $('Week to report').first().json.report_label_id] }) }}",
                     query={"internalDateSource": "dateHeader"})
    row = b.http("Sheets: report row", "POST", f"{SHEETS}/{sheet_id}/values/'Weekly report'!A1:append",
                 "={{ JSON.stringify({ values: [$('Check numbers and render').first().json.row] }) }}",
                 query={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"})
    b.chain(trigger, labels, week, jobs, log, compute, summary, render, deliver, row)
    return {"name": "AI inbox ops: weekly report", "nodes": b.nodes, "connections": b.connections,
            "settings": {"executionOrder": "v1", "timezone": json.loads(logic_config_tz(logic))},
            "pinData": {}, "active": False, "tags": []}


def error_alert(logic: str, google_cred: dict) -> dict:
    b = Builder(logic, google_cred)
    trigger = b._add({"parameters": {}, "name": "When a workflow fails", "type": "n8n-nodes-base.errorTrigger",
                      "typeVersion": 1})
    labels = b.http("Gmail: labels", "GET", f"{GMAIL}/labels", retries=False)
    msg = b.code("Write alert", """
const failure = $('When a workflow fails').first().json;
const e = failure.execution || {};
const label = ($input.first().json.labels || []).find(l => l.name === 'AI/Report');
const text = `A run of "${failure.workflow?.name || 'the inbox workflow'}" failed.\\n\\nStep: ${e.lastNodeExecuted}\\n` +
  `Error: ${(e.error || {}).message}\\n\\nOpen the run to retry it: ${e.url}`;
const raw = rawReport(CONFIG.reportTo, `Inbox Ops failed at ${e.lastNodeExecuted}`, text, `<pre>${text.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}</pre>`);
return [{ json: { raw, labels: ['INBOX', 'UNREAD', ...(label ? [label.id] : [])] } }];""", each=False)
    put = b.http("Gmail: place in owner's inbox", "POST", f"{GMAIL}/messages",
                 "={{ JSON.stringify({ raw: $json.raw, labelIds: $json.labels }) }}",
                 query={"internalDateSource": "dateHeader"}, retries=False)
    b.chain(trigger, labels, msg, put)
    return {"name": "AI inbox ops: error alert", "nodes": b.nodes, "connections": b.connections,
            "settings": {"executionOrder": "v1"}, "pinData": {}, "active": False, "tags": []}


def logic_config_tz(logic: str) -> str:
    cfg = json.loads(logic.split("const CONFIG = ", 1)[1].split(";\n", 1)[0])
    return json.dumps(cfg["timezone"])


def build_all(cfg: dict, google_cred: dict, query: str, sheet_id: str, alert_url: str = "") -> dict[str, dict]:
    logic = (ROOT / "n8n" / "src" / "logic.js").read_text(encoding="utf-8")
    logic = logic.replace("/*CONFIG*/ {}", json.dumps(cfg, ensure_ascii=False))
    return {"inbox-triage.json": inbox_triage(logic, google_cred, query, sheet_id, alert_url),
            "weekly-report.json": weekly_report(logic, google_cred, sheet_id),
            "error-alert.json": error_alert(logic, google_cred)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rules", default=str(ROOT / "routing.yaml"))
    p.add_argument("--model", default="claude-opus-5-5")
    p.add_argument("--effort", default="low")
    p.add_argument("--query", default=DEFAULT_QUERY, help="Gmail search for emails to process")
    p.add_argument("--sheet-id", default="YOUR_SPREADSHEET_ID")
    p.add_argument("--alert-url", default="", help="Slack incoming webhook (optional)")
    p.add_argument("--out", default=str(ROOT / "n8n"))
    args = p.parse_args()
    cfg = config(Path(args.rules), args.model, args.effort)
    out = Path(args.out)
    for name, wf in build_all(cfg, GOOGLE_CRED, args.query, args.sheet_id, args.alert_url).items():
        (out / name).write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {out / name} ({len(wf['nodes'])} nodes)")


if __name__ == "__main__":
    main()
