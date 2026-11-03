# AI Inbox Ops

Sorts a service business's inbox: each email gets a category and an urgency (backed by a quote from the email), is assigned to the right person with a reply deadline, gets a suggested reply saved as a draft, and is logged to a sheet. A follow-up check flags emails that are past their deadline with no reply, and every Monday the owner gets a weekly report whose figures are computed in code.

The sample business, Harbor Home Services (home repairs), is fictional. Its rules are in `routing.yaml`.

**Two versions of the same flow:**
- **Python app:** inbox sorting with a measured eval, real Gmail and Google Sheets, follow-ups, the weekly report and a dashboard. All of it runs in one process (`python -m inbox.web`).
- **n8n workflows** (`n8n/`): triage, weekly report and error alert, built from the same rules and prompts, and tested on n8n 2.46.4. See [`n8n/README.md`](n8n/README.md).

Tested live on 2026-11-03 against a demo Gmail account with fictional emails:
- **Python:** 7 emails were labelled, routed and logged, with 4 threaded reply drafts. The urgent and overdue alerts fired. The weekly report was built from 98 fictional jobs, flagged the 3 planted issues, passed the number check, and was delivered once.
- **n8n:** 2 more emails were triaged with drafts, a matching weekly report was delivered, and an error alert fired.
- **Sent folder:** stayed empty throughout.

**Docs:** [`HANDOVER.md`](HANDOVER.md) covers running, rules, models, keys, privacy and troubleshooting. [`docs/demo-video-script.md`](docs/demo-video-script.md) is a 2-minute walkthrough.

## Measured accuracy
30 fictional emails with hand-labelled category, urgency, reply need and key fields (`evals/emails.jsonl`), run on 2026-11-03. They cover:
- emergencies written calmly, in lowercase, in Spanish and forwarded from a tenant
- an angry email about an old bill, which is a complaint but not an emergency
- lead-selling spam dressed up as urgent jobs, an out-of-office auto-reply and a prompt-injection attempt
- a job applicant, a reschedule and supplier mail

| Classifier | Category | Urgency | Emergencies caught | Urgency too low | Reply needed | Fields extracted | Cost per email |
|---|---|---|---|---|---|---|---|
| Claude Opus 5.5 (effort low) | 30/30 | 29/30 | **8/8** | 0 | 30/30 | 10/10 | $0.0104 |
| Claude Sonnet 5.5 (effort low) | 30/30 | 27/30 | 7/8 | 1 | 30/30 | 10/10 | $0.0053 |
| Claude Haiku 5.5 (effort low) | 30/30 | 26/30 | 8/8 | 1 | 30/30 | 10/10 | $0.0003 |
| Keyword rules (no API key) | 17/30 | 19/30 | 4/8 | 9 | 25/30 | 10/10 | free |

- **"Urgency too low" is the number that matters most.** It means a customer waits longer than they should.
  - Opus never did it.
  - Sonnet rated the forwarded no-heat email from a tenant as "today" instead of "now".
  - Haiku caught every emergency but rated a reschedule too low.
  - The other misses are urgency rated too *high*, which costs an earlier reply, not a missed emergency. Example: a neutral refund question marked "today".
- **Results are from the first run.** No prompt was changed after seeing them. Still, 30 cases is a small set: treat the numbers as indicative.
- **The keyword rules are only a fallback.** They missed half the emergencies, including the calm gas smell and the Spanish leak.

Reproduce with `.venv/bin/python -m inbox.eval --classifier claude --model claude-opus-5-5` (about $0.31 per run). Saved results are in `evals/results/`.

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

## Connect Gmail and Google Sheets
Use a Gmail account made for testing, not a personal inbox. The AI reads every email the query matches.

1. In Google Cloud (signed in as the test account): create a project and enable the **Gmail API** and **Google Sheets API**.
2. Under **Google Auth Platform**: set the audience to External, leave the app in Testing, and add the account as a test user.
3. Under **Clients**: create a **Desktop app** client and save its JSON as `google-credentials/client_secret.json`. That folder is git-ignored.
4. In `.env`, set `INBOX_TEST_ACCOUNT` to the address and `INBOX_QUERY=label:Demo`. Then run:
   ```bash
   .venv/bin/python -m inbox.google_setup login         # browser sign-in, once (testing-mode logins last 7 days)
   .venv/bin/python -m inbox.google_setup create-sheet  # creates the log sheet, saves SHEET_ID
   .venv/bin/python -m inbox.google_setup seed          # inserts the fictional emails, labelled Demo (nothing is sent)
   .venv/bin/python -m inbox.run --once                 # one pass; without --once it keeps polling
   ```

**Permissions:**
- `gmail.modify` lets the app read mail, add labels and create drafts. Google has no drafts-only permission, so it could technically send. The code never calls a send method, and a test checks the code for one.
- `spreadsheets` lets it create and edit the log sheet.

**Safeguards:**
- Every Google command checks it's signed in as `INBOX_TEST_ACCOUNT` and refuses any other account.
- `INBOX_QUERY` limits which emails are read.
- Sheet rows are written as plain text, so a formula in an email is never run.

## Dashboard
`python -m inbox.web` runs the dashboard at http://127.0.0.1:8030/dashboard, together with Gmail polling every 2 minutes, hourly follow-ups and the Monday report. It shows:
- emails that need attention, with a Retry button
- emails waiting for a reply, urgent first, with the assignee and deadline (overdue in red), any draft warnings and a link to Gmail
- the latest weekly report
- every email, filterable by type

Outside demo mode it needs `ADMIN_PASSWORD`. Actions reject cross-site requests, everything is shown as text (never as HTML), and a Content Security Policy blocks framing.

## Try it
```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m inbox.demo            # keyword rules, free
.venv/bin/python -m inbox.demo --claude   # Claude: copy .env.example to .env and add ANTHROPIC_API_KEY
.venv/bin/python -m pytest -q
```
