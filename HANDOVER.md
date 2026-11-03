# Handover guide

How to run, change and look after AI Inbox Ops. Written for the business owner or whoever maintains it. The README explains how it works.

## Day to day
- **Gmail is where the work happens.**
  - Labels such as `AI/Complaint` and `AI/Urgent` sort the inbox.
  - Suggested replies wait in **Drafts**, in the right thread. Read, edit and send them yourself; the app never sends anything.
  - The weekly report arrives in the inbox every Monday, labelled `AI/Report`.
- **The dashboard** (`/dashboard`):
  - **Needs attention:** emails that hit an error (Claude, Gmail or Sheets down). Each shows the error and a **Retry** button. Finished steps aren't repeated.
  - **Waiting for a reply:** urgent emails first, each with who it's assigned to and its deadline. Overdue deadlines show in red.
  - **Weekly report:** the latest report, and whether it was delivered.
  - **All emails:** filter by type.
- **The Google Sheet:**
  - *Inbox log*: one row per email, with its status (open, replied, overdue, no reply needed).
  - *Weekly report*: one row per week.
  - *Jobs*: where you, or your job system, record jobs and invoices.
- **Alerts** (with `ALERT_WEBHOOK_URL` set): urgent emails, complaints, overdue replies, and anything needing attention.

## Change the rules (`routing.yaml`)
| To change | Edit |
|---|---|
| What each type and urgency means | `categories`, `urgency`. Claude reads these descriptions. |
| Who gets what, and how fast | `rules`: first match wins; `assign`, `reply_within_hours`, `alert`. The app refuses to start if any combination has no rule. |
| People | `people` (name and email) |
| Which types get a draft, the sign-off, and the phone number in drafts | `reply` |
| Report day, time, recipient and issue thresholds | `report` |
| Timezone | `timezone` |

After changing categories or urgency descriptions:
1. Run `.venv/bin/python -m inbox.eval --classifier claude`. It costs about $0.31 on Opus. Compare with the README table.
2. Restart the app.
3. If you use n8n, rebuild with `n8n/build.py` and re-import.

## Model and cost
Set `INBOX_MODEL` in `.env`. Measured on the 30-email eval:

| Model | Emergencies caught | Cost per email |
|---|---|---|
| `claude-opus-5-5` (default) | 8/8 | about $0.010 |
| `claude-sonnet-5-5` | 7/8 | about $0.005 |
| `claude-haiku-5-5` | 8/8 (more mistakes on routine mail) | about $0.0003 |

Emails that get a draft cost one more call. The weekly summary is one call per week.

## Keys and logins
Secrets live in `.env` and `google-credentials/`, which are both git-ignored. Never commit or email them.

| What | Where | Then |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com, API keys | Update `.env`, restart |
| Google login (`google-credentials/token.json`) | Run `python -m inbox.google_setup login` | Restart. In Google's "Testing" mode this expires after 7 days. For long-term use, publish the OAuth app or use a Google Workspace internal app. |
| Google OAuth client (`client_secret.json`) | Google Cloud → Google Auth Platform → Clients | Replace the file, then log in again |
| `ADMIN_PASSWORD` | You choose it (12+ characters) | Update `.env`, restart |
| `ALERT_WEBHOOK_URL` | Slack app settings → Incoming Webhooks | Update `.env`, restart |

The Google permission lets the app read mail, label it, create drafts and edit the sheet. Google has no drafts-only permission, so it could technically send. The code never calls a send method, and a test checks the code for one. To revoke access, go to the Google account → Security → Third-party connections.

## Which emails are read
`INBOX_QUERY` (Gmail search syntax) decides which emails are read. The demo uses `label:Demo`; a real setup uses `in:inbox -from:me newer_than:14d`. Emails already labelled `AI/Processed`, and the app's own reports, are always skipped. **Before pointing it at an inbox, check what's in it.** Every matching email is sent to Claude.

## Running it for real
```bash
.venv/bin/python -m inbox.web          # dashboard + Gmail polling + follow-ups + Monday report, one process
```
- Put it behind HTTPS (for example Caddy or nginx), and set `DEMO_MODE=false`, `ADMIN_PASSWORD` and `PUBLIC_URL`.
- Run **one** process. Restarts are safe: unfinished emails are completed on startup, and each week's report is delivered once.
- Check `GET /health` to see what's connected.

## Records and privacy
- `inbox.db` (SQLite) holds every processed email with its classification and history, plus the weekly reports.
- The Google Sheet holds the inbox log, so anyone you share it with sees senders and summaries.
- **Backup:** copy `inbox.db` while the app is stopped, or run `sqlite3 inbox.db ".backup backup.db"`.
- **To delete a person's data:** remove their rows from the sheet, delete their rows from `emails` and `events` in `inbox.db`, and delete the emails in Gmail if wanted.

## When something goes wrong
- **Many emails in *Needs attention* at once** usually means a service is down. Check the Anthropic, Google or Slack status pages, then retry.
- **"Google login expired":** run `python -m inbox.google_setup login`.
- **A report says "the AI summary was replaced":** the figures are still correct. The summary contained a number that wasn't in the data, so the plain summary was used instead.
- **Tests:** run `.venv/bin/python -m pytest -q`. They need no keys or accounts.
