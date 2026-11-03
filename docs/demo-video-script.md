# Demo video script (about 2 minutes)

For an Upwork portfolio item or a proposal. Record the screen at 1080p with your voice, no music. Use only the demo Gmail account and fictional emails.

## Before recording
- Run `python -m inbox.google_setup login` (testing-mode logins last 7 days), then `python -m inbox.google_setup seed` to insert fresh fictional emails.
- Start `python -m inbox.web --no-poll` and open `/dashboard`. Open Gmail (demo account) and the Google Sheet in other tabs.
- Zoom the browser to 125%.

## Shots
| Time | Screen | Say |
|---|---|---|
| 0:00 | Gmail inbox full of unread demo emails | "A home-repair company gets this every morning: leaks, quotes, complaints, invoices, suppliers and spam, all in one inbox." |
| 0:10 | Terminal: `python -m inbox.run --once` | "This reads each email with Claude, then code checks the answer before anything happens." |
| 0:20 | Gmail: labels appear; open the ceiling-leak email | "It's labelled and sorted. The leak is marked urgent, but only because the email really says water is coming through the ceiling. If the AI can't quote the email, the code lowers the urgency instead of trusting it." |
| 0:40 | Gmail Drafts: the leak reply | "A suggested reply is waiting in Drafts, in the customer's thread, with safety advice and the phone number. Nothing is sent automatically; a person sends it." |
| 0:55 | Dashboard: *Waiting for a reply*, the overdue item in red | "The dashboard shows who owns each email and when it's due. Anything overdue is flagged and alerted once." |
| 1:10 | Google Sheet: *Inbox log* | "Everything is logged to a sheet the team already uses." |
| 1:20 | Gmail: open the weekly report | "Every Monday the owner gets a report. All the figures are calculated in code. Claude only writes the summary, and every number in it is checked against the data. If it ever makes one up, the plain version goes out instead." |
| 1:40 | README eval table | "I tested it on 30 tricky emails: calm gas leaks, Spanish, disguised spam, prompt injection. Opus caught all 8 emergencies, for about a cent per email." |
| 1:50 | n8n canvas | "The same flow also comes as n8n workflows, built from the same rules." |
| 1:55 | HANDOVER.md | "It comes with tests, a handover guide, and retries that never lose an email. Happy to set it up for your inbox." |

## After recording
- Cut waiting time. Upload unlisted to YouTube or Loom, and link it at the top of the README and in the Upwork portfolio item.
- Upwork portfolio title: "AI inbox triage + weekly report for a service business (Gmail, Sheets, Claude, n8n)".
