# Outlook and Jira integrations

## Outlook draft (Review & Analysis → 📧 Draft clarification email)
- Content: open **cross-document contradictions** (values side by side with source files) and **open stakeholder
  questions**. Internal wording notes (vague words, transcription typos, duplicates) are always left out.
- Scope follows the requirement filters: no filter = everything open; with a filter = only issues linked to the
  requirements shown. The questions CSV uses the same scope.
- Subject: `[ACTION REQUIRED] Critical Requirements Clarifications & Gaps – {Session Name}`.
- Message: multipart/alternative, UTF-8 **Base64** text + HTML parts, strict **CRLF** line endings, `X-Unsent: 1`
  (Outlook opens it as an editable compose window). Non-ASCII subjects are encoded as one continuous encoded-word,
  so spaces around characters like "–" are kept.
- HTML is written for Outlook's Word engine (table attributes, `bgcolor` headers, fonts on every cell, wrap points
  in long file names). The in-app preview shows the same HTML.
- Files are saved as `data/drafts/Requirements_Critical_Findings_<timestamp>.eml`, so a draft still open in Outlook
  never blocks a new one (WinError 32).
- **🚀 Open Directly in Outlook**, Windows: classic Outlook via COM (`pywin32`) → `outlook.exe /eml` →
  `os.startfile` (default .eml app – may ask which app). macOS: `open`. Linux: `xdg-open`.
  New Outlook does not accept drafts from other programs – use **📥 Download .eml File**.

## Jira (Stories & Tests → 🧩 Jira push only)
- `POST /rest/api/2/issue`, one issue per story; connection test calls `/rest/api/2/myself` and
  `/rest/api/2/project/{KEY}`.
- Cloud: Basic auth (`email:api_token`). Data Center/Server: leave `JIRA_USER_EMAIL` empty and use a personal
  access token (Bearer).
- Description (Jira wiki/Textile): `bq.` user story, linked requirement IDs and titles, Gherkin criteria
  (`* *AC-001-01* (HAPPY_PATH):` / `** *Given* …`), numbered QA steps with expected results, happy path vs edge case.
- Labels sanitised with `[^a-zA-Z0-9_-] → _`, max 30 characters; `RIA` always, `ria-approved` for approved stories.
- **🧪 Simulation** (default): same payloads, no network, keys `EXP-101, EXP-102 …` with browse links; live progress bar.
- Re-clicking create makes new issues (no duplicate protection). Use only a Jira project approved for this tool.
