# n8n version

The same inbox sorting and weekly report as the Python app, as importable n8n workflows. It's for clients who already run n8n, or who want to change steps visually.

**Tested on n8n 2.46.4 (2026-11-03)** in Docker, against a demo Gmail account, the demo Google Sheet and Claude:
- **Triage:** a calm "faint gas smell" email came out *New job / now* with the `AI/Urgent` label, and its threaded draft gave safety advice. A kitchen-remodel quote came out *Quote / later*, and its draft asked for drawings with no price. Both were logged to the sheet and marked processed.
- **Weekly report:** same figures as the Python version ($2,040, down 50%). The summary passed the number check, and the report was placed in the owner's inbox with the `AI/Report` label.
- **Error alert:** a deliberately failing workflow produced an alert in the owner's inbox naming the failed step and the error.
- **Sent folder:** stayed empty throughout.

## Files
- `inbox-triage.json`: every 2 minutes. Fetch new mail → Claude classifies → code checks and routes → labels → sheet row → Claude drafts a reply (saved as a Gmail draft) → optional Slack alert → mark processed (always last).
- `weekly-report.json`: Mondays 08:00 in the business's timezone. Jobs tab and inbox log → figures and issues computed in code → Claude writes the summary → number check → placed in the owner's inbox → report row.
- `error-alert.json`: places an alert in the owner's inbox when a run fails.
- `src/logic.js` and `build.py`: the source. The JSON files are built from these, using `routing.yaml` and the Python app's prompts. `tests/test_n8n.py` runs the JavaScript under Node and checks it matches Python case by case: checks, routing, deadlines and times, draft warnings, every report figure, the number check, and the rendered report text.

## Setup
1. Build with your sheet and inbox query:
   ```bash
   .venv/bin/python n8n/build.py --sheet-id <SPREADSHEET_ID> --query "in:inbox -from:me newer_than:14d" \
       [--alert-url https://hooks.slack.com/services/...]
   ```
   For the demo, use `--query "label:Demo"`.
2. Create the Gmail labels once: `python -m inbox.google_setup labels`, or create `AI/New job`, `AI/Quote`, `AI/Complaint`, `AI/Billing`, `AI/Supplier`, `AI/Marketing`, `AI/Other`, `AI/Urgent`, `AI/Processed` and `AI/Report` by hand in Gmail. The workflows stop with a clear message if one is missing.
3. In n8n, import `error-alert.json`, then the other two.
4. Create two credentials and select them in the nodes:
   - **Google OAuth2 API**, with the scopes `https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/spreadsheets`. Its OAuth client needs n8n's redirect URL, which n8n shows on the credential screen.
   - **Header Auth** for Anthropic: name `x-api-key`, value your key.
5. In each main workflow's **Settings → Error workflow**, choose *AI inbox ops: error alert*.
6. Publish all three.

## Differences from the Python app
- **No hourly overdue check,** so there are no overdue alerts and no response-time figure. Those need state between runs. The report still counts emails already marked overdue in the sheet.
- **A failed run doesn't resume from the failed step.** HTTP calls retry 3 times, then the run fails and the owner is alerted. Re-run it from n8n's executions list. The "processed" label is applied last, so the email is picked up again on the next run, but steps that already happened, such as labels or a draft, may then repeat.
- **The weekly report doesn't check whether that week was already reported.** It runs once per schedule, so running it by hand adds a second report.
- **No dashboard.** Use the sheet and n8n's executions list instead.
