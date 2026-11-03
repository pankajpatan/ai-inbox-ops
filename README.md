# AI Inbox Ops

Sorts a service business's inbox: each email gets a category and an urgency (backed by a quote from the email), is assigned to the right person with a reply deadline, gets a suggested reply saved as a draft, and is logged to a sheet. A follow-up check flags emails that are past their deadline with no reply. A weekly report is coming in a later milestone.

The sample business, Harbor Home Services (home repairs), is fictional. Its rules are in `routing.yaml`.

## Status
Milestone 1 of 5 is done: rules, checks, classifiers, routing, drafts, the pipeline with retries, follow-ups and tests. It runs against an in-memory mailbox and sheet.

Coming next: the accuracy eval, the real Gmail and Google Sheets connections, the weekly report, a dashboard and an n8n version.

## How it works
1. **Fetch** new mail. Each message is stored once by its ID, so polling twice never handles an email twice.
2. **Classify:** Claude (or keyword rules, without an API key) picks one category and an urgency (now, today or later), extracts contact details, and decides whether a reply is needed.
3. **Check in code before acting:**
   - An urgency of "now" or "today" needs a quote that really appears in the email. Without one, it's lowered by one step only, so a real emergency is never buried.
   - Phone numbers, invoice numbers, amounts, names and addresses must appear in the email, or they're removed.
4. **Route** using the first matching rule in `routing.yaml`: assignee, reply deadline and whether to alert. The app refuses to start if any category and urgency combination has no rule.
5. **Act:**
   - Labels such as `AI/Complaint` and `AI/Urgent`.
   - A suggested reply saved as a **draft**, which a person sends. Code flags any draft that mentions a price, promises a visit time, offers a refund or discount, or contains a placeholder.
   - A row in the log sheet.
   - An alert for urgent emails and complaints.

   Each action is saved as it happens, so a retry never repeats one.
6. **Mark processed.** This happens last, so a crash leaves the email safe to pick up again.
7. **Follow up:** emails past their deadline with no reply in the thread are marked overdue and alerted once. When someone replies, the email is marked replied.
8. **Failures** (Claude, mailbox, sheet) leave the email in `needs_attention` with the step to resume from. On restart, unfinished emails are completed automatically. A per-email lock stops two threads handling the same email.

## Try it
```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m inbox.demo            # keyword rules, free
.venv/bin/python -m inbox.demo --claude   # Claude: copy .env.example to .env and add ANTHROPIC_API_KEY
.venv/bin/python -m pytest -q
```
