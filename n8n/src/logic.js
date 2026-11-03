// Shared logic for the n8n workflows. build.py inlines this file into each Code node and fills CONFIG
// from routing.yaml and the Python prompts, so both versions apply the same rules.
// Pure functions only (no n8n globals); tests/test_n8n.py runs them with plain Node against Python.

const CONFIG = /*CONFIG*/ {};

// ---------- small helpers ----------

function roundHalfEven(x, digits = 0) {          // Python's round(): 2.5 -> 2, 3.5 -> 4
  const f = Math.pow(10, digits);
  const v = x * f;
  const r = Math.round(v);
  const isHalf = Math.abs(v % 1) === 0.5;
  const out = isHalf ? (r % 2 === 0 ? r : r - 1) : r;
  return out / f;
}
const pyFloat = (x) => (Number.isInteger(x) ? x.toFixed(1) : String(x));   // Python prints 2.0, not 2
const money = (x) => "$" + roundHalfEven(x).toLocaleString("en-US", { maximumFractionDigits: 0 });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;" }[c]));

const SAME = { "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " " };
function norm(text) {
  let t = String(text || "").replace(/[’‘“”–— ]/g, (c) => SAME[c]);
  return t.replace(/\s+/g, " ").trim().replace(/^["'.,]+|["'.,]+$/g, "").toLowerCase();
}
const digits = (s) => String(s || "").replace(/\D/g, "");
const reEscape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function localParts(ts) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: CONFIG.timezone, weekday: "short", day: "2-digit", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "longOffset",
  }).formatToParts(new Date(ts * 1000));
  return Object.fromEntries(parts.map((p) => [p.type, p.value]));
}
function when(ts) {                               // Python: strftime("%a %d %b %H:%M")
  if (ts === null || ts === undefined) return "";
  const p = localParts(ts);
  return `${p.weekday} ${p.day} ${p.month} ${p.hour === "24" ? "00" : p.hour}:${p.minute}`;
}
function isoLocal(ts) {                           // Python: isoformat(timespec="minutes")
  const p = localParts(ts);
  const months = { Jan: "01", Feb: "02", Mar: "03", Apr: "04", May: "05", Jun: "06", Jul: "07", Aug: "08", Sep: "09", Oct: "10", Nov: "11", Dec: "12" };
  const offset = (p.timeZoneName || "GMT").replace("GMT", "") || "+00:00";
  return `${p.year}-${months[p.month]}-${p.day}T${p.hour === "24" ? "00" : p.hour}:${p.minute}${offset}`;
}
function localDate(ts) { return isoLocal(ts).slice(0, 10); }
function addDays(isoDate, n) {
  const d = new Date(isoDate + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function weekStartOf(isoDate) {
  const day = (new Date(isoDate + "T00:00:00Z").getUTCDay() + 6) % 7;   // Monday = 0
  return addDays(isoDate, -day);
}
const daysBetween = (a, b) => Math.round((Date.parse(b + "T00:00:00Z") - Date.parse(a + "T00:00:00Z")) / 86400000);

// ---------- Gmail ----------

function b64urlDecode(s) { return Buffer.from(String(s).replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8"); }
function b64urlEncode(s) { return Buffer.from(s, "utf8").toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }

function htmlToText(markup) {
  return String(markup).replace(/<(script|style)[\s\S]*?<\/\1>/gi, " ")
    .replace(/<br\s*\/?>|<\/p>|<\/div>|<\/li>/gi, "\n").replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#39;|&#x27;/g, "'");
}

function parseAddress(value) {
  const m = String(value || "").match(/^\s*(?:"?([^"<]*?)"?\s*)?<([^>]+)>\s*$/);
  if (m) return { name: (m[1] || "").trim(), email: m[2].trim() };
  return { name: "", email: String(value || "").trim() };
}

function parseMessage(m) {
  const headers = Object.fromEntries(((m.payload || {}).headers || []).map((h) => [h.name.toLowerCase(), h.value]));
  const plain = [], rich = [];
  (function walk(part) {
    const data = part.body && part.body.data;
    if (data && part.mimeType === "text/plain") plain.push(b64urlDecode(data));
    else if (data && part.mimeType === "text/html") rich.push(htmlToText(b64urlDecode(data)));
    for (const p of part.parts || []) walk(p);
  })(m.payload || {});
  let body = (plain.length ? plain : rich).join("\n").replace(/[ \t ]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
  const from = parseAddress(headers.from);
  return {
    message_id: m.id, thread_id: m.threadId, from_name: from.name, from_email: from.email || "unknown@unknown",
    subject: headers.subject || "", body: body.slice(0, 20000), received_at: Number(m.internalDate || 0) / 1000,
    link: `https://mail.google.com/mail/u/0/#all/${m.id}`, header_message_id: headers["message-id"] || "",
    header_references: headers.references || "",
  };
}

// ---------- classify, check, route ----------

function anthropicBody(system, user, schema) {
  const body = { model: CONFIG.model, max_tokens: 3000, system, messages: [{ role: "user", content: user }],
    output_config: { effort: CONFIG.effort, format: { type: "json_schema", schema } } };
  if (CONFIG.fallbackModels.includes(CONFIG.model)) body.fallbacks = "default";
  return body;
}

function buildClassifyRequest(email) {
  const sender = email.from_name ? `${email.from_name} <${email.from_email}>` : email.from_email;
  return anthropicBody(CONFIG.classifySystem, `From: ${sender}\n<email>\nSubject: ${email.subject}\n\n${email.body}\n</email>`,
    CONFIG.classifySchema);
}

function claudeJson(response) {
  if (!response || response.type === "error") throw new Error("Claude API error: " + JSON.stringify(response && response.error).slice(0, 300));
  if (response.stop_reason === "refusal") throw new Error("Claude declined to answer");
  if (response.stop_reason === "max_tokens") throw new Error("Claude's answer was cut off (max_tokens)");
  const block = (response.content || []).find((b) => b.type === "text");
  if (!block) throw new Error("Claude returned no text");
  return JSON.parse(block.text);
}

const STEP_DOWN = { now: "today", today: "later" };

// Same rules as inbox/checks.py
function checkClassification(c, email) {
  const corrections = [];
  const out = JSON.parse(JSON.stringify(c));
  const text = norm(`${email.subject}\n${email.body}`);
  if (STEP_DOWN[c.urgency]) {
    const ev = norm(c.urgency_evidence || "");
    if (!ev || !text.includes(ev)) {
      corrections.push(`urgency '${c.urgency}' had no quote from the email; lowered to '${STEP_DOWN[c.urgency]}'`);
      out.urgency = STEP_DOWN[c.urgency];
      out.urgency_evidence = null;
    }
  } else if (c.urgency_evidence) out.urgency_evidence = null;
  if (out.urgency_evidence === "") out.urgency_evidence = null;

  const raw = `${email.subject}\n${email.body}`;
  const rawDigits = digits(raw);
  const x = out.extracted = { ...(c.extracted || {}) };
  for (const field of ["phone", "invoice_number", "amount"]) {
    const value = x[field];
    if (value === null || value === undefined) continue;
    let found;
    if (field === "phone") found = digits(value).length >= 7 && rawDigits.includes(digits(value));
    else if (field === "amount") {
      const number = String(value).replace(/[^\d.,]/g, "").replace(/^[.,]+|[.,]+$/g, "");
      found = !!number && new RegExp(`(?<![\\d.,])${reEscape(number)}(?![\\d])`).test(raw);
    } else found = text.includes(norm(value)) || (digits(value).length >= 3 && rawDigits.includes(digits(value)));
    if (!found) { corrections.push(`${field} '${value}' is not in the email; removed`); x[field] = null; }
  }
  for (const field of ["name", "address"]) {
    const value = x[field];
    if (value === null || value === undefined) continue;
    const words = (norm(value).match(/[\w']+/g) || []).filter((w) => w.length > 1);
    const sender = norm(`${email.from_name} ${email.from_email}`);
    if (!words.length || !words.every((w) => text.includes(w) || sender.includes(w))) {
      corrections.push(`${field} '${value}' is not in the email; removed`); x[field] = null;
    }
  }
  if (c.category === "spam_marketing" && c.needs_reply) { out.needs_reply = false; corrections.push("spam needs no reply"); }
  return { classification: out, corrections };
}

function route(category, urgency) {
  return CONFIG.rules.find((r) => (!r.category || r.category.includes(category)) && (!r.urgency || r.urgency.includes(urgency)));
}

function labelsFor(category, urgency) {
  const labels = [`AI/${CONFIG.labels[category]}`];
  if (urgency === "now") labels.push("AI/Urgent");
  return labels;
}

function triage(email, answer) {
  const { classification: c, corrections } = checkClassification(answer, email);
  const rule = route(c.category, c.urgency);
  const person = rule.assign ? CONFIG.people[rule.assign] : null;
  const deadline = rule.reply_within_hours !== null && rule.reply_within_hours !== undefined && c.needs_reply
    ? email.received_at + rule.reply_within_hours * 3600 : null;
  const wantsDraft = c.needs_reply && !!rule.assign && CONFIG.reply.draft_for.includes(c.category);
  return { c, corrections, rule, person, deadline, wantsDraft, labels: labelsFor(c.category, c.urgency) };
}

function sheetRow(email, t, draftNote) {
  const row = {
    received: when(email.received_at), from: `${email.from_name} <${email.from_email}>`.trim(), subject: email.subject,
    category: CONFIG.labels[t.c.category], urgency: t.c.urgency, assigned_to: t.person ? t.person.name : "",
    reply_by: when(t.deadline), summary: t.c.summary || "", phone: (t.c.extracted || {}).phone || "",
    draft: draftNote || "", status: t.deadline ? "open" : "no reply needed", link: email.link || "",
    received_iso: isoLocal(email.received_at),
  };
  return CONFIG.logColumns.map((k) => String(row[k] ?? ""));
}

// ---------- drafts ----------

const DRAFT_AMOUNT = "\\d(?:[\\d,.]*\\d)?";
const MONEY_RE = new RegExp(`[$€£₹]\\s?${DRAFT_AMOUNT}|\\b${DRAFT_AMOUNT}\\s?(?:usd|dollars|eur|euros|gbp|pounds)\\b`, "gi");
const TIME_PROMISE = /\b(?:we will|we'll|we can|someone will|a technician will|our team will|i will|i'll)\b[^.!?\n]{0,50}?\b(?:today|tonight|tomorrow|right away|immediately|within \d+|in \d+ (?:minutes?|hours?))\b/i;
const CONCESSION = /\b(refund|discount|free of charge|no charge|compensat\w*|waive\w*)\b/gi;

// Same rules as inbox/drafts.py check_draft()
function checkDraft(d, email) {
  const warnings = [];
  const text = `${d.subject}\n${d.body}`;
  const source = `${email.subject}\n${email.body}`.toLowerCase();
  if (!CONFIG.reply.allow_prices) {
    const quoted = (text.match(MONEY_RE) || []).filter((m) => !source.includes(m.toLowerCase()));
    if (quoted.length) warnings.push(`Mentions a price the customer did not give: ${quoted.join(", ")}.`);
  }
  const promise = text.match(TIME_PROMISE);
  if (promise) warnings.push(`Promises a time: "${promise[0]}". Check someone can really do that.`);
  const concessions = [...new Set((text.match(CONCESSION) || []).map((m) => m.toLowerCase()))]
    .filter((m) => !source.includes(m)).sort();
  if (concessions.length) warnings.push(`Offers something the business may not agree to: ${concessions.join(", ")}.`);
  if (/\[[^\]]{2,40}\]|\{[^}]{2,40}\}/.test(text)) warnings.push("Contains a placeholder in brackets.");
  const words = d.body.split(/\s+/).filter(Boolean).length;
  if (words > 200) warnings.push(`Long for a reply (${words} words).`);
  return warnings;
}

function buildDraftRequest(email, c) {
  let goal = CONFIG.draftGoals[c.category] || "Thank them and say the office will follow up.";
  if (c.urgency === "now") goal += " It is urgent: say we are treating it as urgent and give the phone number.";
  const user = `Goal: ${goal}\nSummary: ${c.summary}\nFrom: ${email.from_name || email.from_email}\n` +
    `<email>\nSubject: ${email.subject}\n\n${email.body}\n</email>`;
  return anthropicBody(CONFIG.draftSystem, user, CONFIG.draftSchema);
}

const oneLine = (s) => String(s || "").split(/\s+/).filter(Boolean).join(" ");

function rawReply(email, subject, body) {
  const headers = [`To: ${oneLine(email.from_email)}`, `Subject: ${oneLine(subject)}`];
  if (email.header_message_id) {
    headers.push(`In-Reply-To: ${email.header_message_id}`,
      `References: ${(email.header_references + " " + email.header_message_id).trim()}`);
  }
  headers.push("MIME-Version: 1.0", 'Content-Type: text/plain; charset="utf-8"', "Content-Transfer-Encoding: 8bit");
  return b64urlEncode(headers.join("\r\n") + "\r\n\r\n" + body);
}

function plainAlert(text, limit = 160) {
  let t = oneLine(text).replace(/</g, "‹").replace(/>/g, "›").replace(/@/g, "(at)");
  return t.length <= limit ? t : t.slice(0, limit - 1) + "…";
}

function alertText(email, t) {
  const label = t.c.category === "complaint" ? "Complaint" : `Urgent (${t.c.urgency})`;
  return `${label} from ${plainAlert(email.from_name || email.from_email, 60)}, for ${t.person ? t.person.name : "nobody"}: ` +
    `${plainAlert(t.c.summary)}\nReply by ${when(t.deadline) || "soon"} · ${email.link}`;
}

// ---------- weekly report (same as inbox/report.py) ----------

const CATEGORY_NAMES = { new_job: "New jobs", quote_request: "Quote requests", complaint: "Complaints",
  invoice_payment: "Billing", supplier: "Supplier", spam_marketing: "Marketing", other: "Other" };

function logToEmails(rows) {                     // inbox log rows (dicts by column) -> what computeMetrics needs
  const byLabel = Object.fromEntries(Object.entries(CONFIG.labels).map(([k, v]) => [v, k]));
  const follow = { open: "open", overdue: "overdue", replied: "replied", "no reply needed": "none" };
  return rows.filter((r) => r.received_iso && byLabel[r.category]).map((r) => {
    const name = (r.from || "").replace(/\s*<[^>]*>\s*$/, "");
    return { received_at: Date.parse(r.received_iso) / 1000, category: byLabel[r.category],
      follow_status: follow[r.status] || "open", replied_at: null, from_name: name, from_email: r.from || "",
      subject: r.subject || "" };
  });
}

function parseJobs(rows) {
  const out = [];
  for (const r of rows) {
    const status = String(r.status || "").trim().toLowerCase();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(r.date || "") || !["completed", "scheduled", "cancelled"].includes(status)) continue;
    out.push({ date: r.date, customer: r.customer || "", status, amount: Number(String(r.amount || "0").replace(/[$,]/g, "")) || 0,
      invoice_number: r.invoice_number || "", invoice_status: String(r.invoice_status || "").trim().toLowerCase(),
      due_date: /^\d{4}-\d{2}-\d{2}$/.test(r.due_date || "") ? r.due_date : null });
  }
  return out;
}

function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  const n = s.length;
  return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
}

function computeMetrics(start, jobs, emails, today) {
  const end = addDays(start, 7), prev = addDays(start, -7);
  const done = jobs.filter((j) => j.status === "completed" && j.date >= start && j.date < end);
  const donePrev = jobs.filter((j) => j.status === "completed" && j.date >= prev && j.date < start);
  const revenue = roundHalfEven(done.reduce((a, j) => a + j.amount, 0));
  const revenuePrev = roundHalfEven(donePrev.reduce((a, j) => a + j.amount, 0));
  const change = revenuePrev ? roundHalfEven((revenue - revenuePrev) / revenuePrev * 100) : null;
  const week = emails.filter((e) => e.category && localDate(e.received_at) >= start && localDate(e.received_at) < end);
  const byCat = {};
  for (const c of Object.keys(CATEGORY_NAMES)) {
    const n = week.filter((e) => e.category === c).length;
    if (n) byCat[CATEGORY_NAMES[c]] = n;
  }
  const waits = week.filter((e) => e.replied_at).map((e) => (e.replied_at - e.received_at) / 3600);
  const unanswered = emails.filter((e) => e.follow_status === "overdue" && localDate(e.received_at) < end);
  const overdue = jobs.filter((j) => j.invoice_status === "unpaid" && j.due_date && j.due_date < today)
    .map((j) => ({ customer: j.customer, invoice: j.invoice_number, amount: roundHalfEven(j.amount), days_late: daysBetween(j.due_date, today) }))
    .sort((a, b) => b.days_late - a.days_late);
  return {
    week_start: start, week_end: addDays(end, -1), jobs_completed: done.length, revenue, revenue_previous_week: revenuePrev,
    revenue_change_pct: change, average_job_value: done.length ? roundHalfEven(revenue / done.length) : 0,
    emails_received: week.length, emails_by_category: byCat, complaints: week.filter((e) => e.category === "complaint").length,
    median_reply_hours: waits.length ? roundHalfEven(median(waits), 1) : null, replies_measured: waits.length,
    unanswered_past_deadline: unanswered.length,
    unanswered_list: unanswered.slice(0, 10).map((e) => ({ from: e.from_name || e.from_email, subject: e.subject || "" })),
    unpaid_overdue_invoices: overdue, unpaid_overdue_total: overdue.reduce((a, i) => a + i.amount, 0),
  };
}

function findIssues(m) {
  const cfg = CONFIG.report.issues;
  const issues = [];
  if (m.revenue_change_pct !== null && m.revenue_change_pct <= -cfg.revenue_drop_pct) {
    issues.push(`Revenue fell ${Math.abs(m.revenue_change_pct)}% to ${money(m.revenue)} (from ${money(m.revenue_previous_week)} the week before).`);
  }
  if (m.complaints >= cfg.complaints_at_least) issues.push(`${m.complaints} complaints this week.`);
  for (const i of m.unpaid_overdue_invoices.filter((i) => i.days_late > cfg.invoice_overdue_days)) {
    issues.push(`Invoice ${i.invoice} for ${i.customer} (${money(i.amount)}) is ${i.days_late} days overdue.`);
  }
  const n = m.unanswered_past_deadline;
  if (n >= cfg.unanswered_overdue_at_least) {
    issues.push(`${n} customer email${n !== 1 ? "s are" : " is"} past ${n !== 1 ? "their" : "its"} reply deadline.`);
  }
  return issues;
}

function numbersIn(value, out = new Set()) {
  if (value === null || value === undefined || typeof value === "boolean") return out;
  if (typeof value === "number") { out.add(value); out.add(Math.abs(value)); }
  else if (typeof value === "string") for (const t of value.match(/\d[\d,]*(?:\.\d+)?/g) || []) out.add(Number(t.replace(/,/g, "")));
  else if (Array.isArray(value)) value.forEach((v) => numbersIn(v, out));
  else if (typeof value === "object") Object.values(value).forEach((v) => numbersIn(v, out));
  return out;
}

function checkNumbers(text, m, issues) {
  const allowed = [...numbersIn(m, numbersIn(issues))];
  const bad = [];
  for (const token of text.match(/(?<![\w-])[$€£]?\d[\d,]*(?:\.\d+)?%?/g) || []) {
    const v = Number(token.replace(/^[$€£]|%$/g, "").replace(/,/g, ""));
    if (!allowed.some((a) => Math.abs(v - a) < 0.051)) bad.push(token);
  }
  return bad;
}

function templateSummary(m, issues) {
  let first = `${m.jobs_completed} jobs were completed for ${money(m.revenue)}`;
  if (m.revenue_change_pct !== null) {
    first += `, ${m.revenue_change_pct >= 0 ? "up" : "down"} ${Math.abs(m.revenue_change_pct)}% on the week before`;
  }
  const parts = [first + ".", `The inbox received ${m.emails_received} emails, including ${m.complaints} complaints.`];
  if (m.median_reply_hours !== null) parts.push(`The median first reply took ${pyFloat(m.median_reply_hours)} hours.`);
  parts.push(issues.length ? `${issues.length} issues need attention.` : "Nothing needs attention.");
  return parts.join(" ");
}

function buildSummaryRequest(m, issues) {
  const user = `Data for the week:\n${JSON.stringify(m, null, 1)}\n\nIssues found by the rules:\n` +
    (issues.map((i) => `- ${i}`).join("\n") || "- none");
  return anthropicBody(CONFIG.summarySystem, user, { type: "object", additionalProperties: false, required: ["summary"],
    properties: { summary: { type: "string" } } });
}

function fmtDay(iso, withYear) {
  const d = new Date(iso + "T00:00:00Z");
  const s = d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", timeZone: "UTC" }).replace(/(\d+) (\w+)/, "$1 $2");
  return withYear ? `${s} ${d.getUTCFullYear()}` : s;
}

function renderReport(m, issues, summary, note) {
  const week = `${fmtDay(m.week_start)} to ${fmtDay(m.week_end, true)}`;
  const subject = `Weekly report, ${week}: ` + (issues.length ? `${issues.length} issue${issues.length !== 1 ? "s" : ""}` : "no issues");
  const change = m.revenue_change_pct === null ? "–" : `${m.revenue_change_pct >= 0 ? "+" : ""}${m.revenue_change_pct}%`;
  const rows = [
    ["Jobs completed", String(m.jobs_completed)],
    ["Revenue", `${money(m.revenue)} (${change} vs ${money(m.revenue_previous_week)})`],
    ["Average job value", money(m.average_job_value)],
    ["Emails received", String(m.emails_received)],
    ["Complaints", String(m.complaints)],
    ["Median first reply", m.median_reply_hours === null ? "–" : `${pyFloat(m.median_reply_hours)} hours`],
    ["Unanswered past deadline", String(m.unanswered_past_deadline)],
    ["Unpaid overdue invoices", `${m.unpaid_overdue_invoices.length} (${money(m.unpaid_overdue_total)})`],
  ];
  let lines = [`${CONFIG.business}: week ${week}`, ""];
  if (summary) lines.push(summary, "");
  if (note) lines.push(`Note: ${note}`, "");
  lines = lines.concat(issues.length ? ["Needs attention:", ...issues.map((i) => `- ${i}`), ""] : ["Nothing needs attention.", ""]);
  lines = lines.concat(rows.map(([k, v]) => `${k}: ${v}`));
  const cats = Object.entries(m.emails_by_category);
  if (cats.length) lines.push("", "Emails by type: " + cats.map(([k, v]) => `${k} ${v}`).join(", "));
  const text = lines.join("\n");
  const table = rows.map(([k, v]) => `<tr><td style='padding:4px 12px 4px 0;color:#555'>${esc(k)}</td><td style='padding:4px 0'><b>${esc(v)}</b></td></tr>`).join("");
  const issueHtml = issues.length ? "<p style='margin:16px 0 4px'><b>Needs attention</b></p><ul>" + issues.map((i) => `<li>${esc(i)}</li>`).join("") + "</ul>"
    : "<p>Nothing needs attention.</p>";
  const html = `<div style='font-family:system-ui,sans-serif;font-size:15px;max-width:560px'><p style='color:#555'>${esc(CONFIG.business)} · week ${esc(week)}</p>` +
    (summary ? `<p>${esc(summary)}</p>` : "") + (note ? `<p style='color:#8a5a00'>Note: ${esc(note)}</p>` : "") +
    issueHtml + `<table style='border-collapse:collapse'>${table}</table></div>`;
  return { subject, text, html };
}

function reportRow(m, issues, ok) {
  const row = { week: m.week_start, jobs_completed: m.jobs_completed, revenue: m.revenue,
    change_pct: m.revenue_change_pct === null ? "" : m.revenue_change_pct, average_job: m.average_job_value,
    emails: m.emails_received, complaints: m.complaints, median_reply_hours: m.median_reply_hours === null ? "" : pyFloat(m.median_reply_hours),
    unanswered_past_deadline: m.unanswered_past_deadline, overdue_invoices: m.unpaid_overdue_invoices.length,
    overdue_total: m.unpaid_overdue_total, issues: issues.join(" | "), summary_checked: ok ? "yes" : "replaced (number mismatch)" };
  return CONFIG.reportColumns.map((k) => String(row[k]));
}

function rawReport(to, subject, text, html) {
  const boundary = "inbox-ops-" + Math.random().toString(36).slice(2);
  const msg = [`From: AI Inbox Ops <reports@inbox-ops.local>`, `To: ${oneLine(to)}`, `Subject: ${oneLine(subject)}`,
    `Date: ${new Date().toUTCString()}`, "MIME-Version: 1.0", `Content-Type: multipart/alternative; boundary="${boundary}"`, "",
    `--${boundary}`, 'Content-Type: text/plain; charset="utf-8"', "", text, "",
    `--${boundary}`, 'Content-Type: text/html; charset="utf-8"', "", html, "", `--${boundary}--`, ""].join("\r\n");
  return b64urlEncode(msg);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { CONFIG, roundHalfEven, money, norm, when, isoLocal, weekStartOf, parseAddress, parseMessage,
    buildClassifyRequest, claudeJson, checkClassification, route, labelsFor, triage, sheetRow, checkDraft,
    buildDraftRequest, rawReply, alertText, logToEmails, parseJobs, computeMetrics, findIssues, checkNumbers,
    templateSummary, buildSummaryRequest, renderReport, reportRow, rawReport, b64urlDecode };
}
