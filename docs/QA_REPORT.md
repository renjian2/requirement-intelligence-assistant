# QA report – v9 (BA simplification + project logger)

Built on v8 (verified: `pages/1–4` and `ria/services/resolution.py` present).

## Changes
| Area | Change |
|---|---|
| Prepare & Run | Removed Input mode, Depth, Glossary & transcript errors, Documents & segments. Mode is detected from the documents; depth is Deep (`RIA_ANALYSIS_DEPTH=quick` in `.env` only). Glossary built automatically from Stage 1 (BRD/policy definitions win, user terms never overwritten). One **Type** dropdown and 🗑️ per document. Advanced = AI provider + Test connection + max tests. Demo provider hidden when a real provider is configured. "Documents changed since the last analysis" warning. |
| Review & Analysis | Stakeholder Resolution Center removed. Each linked issue is shown in the deep-dive with the conflict table, the question, **✔️ Resolve** (answer required) and **❌ Not an issue**. Toolbar: **📧 Draft clarification email** and **Download questions (CSV)** follow the filters; internal wording notes are never included. *Approve all clean requirements in this area*. Run picker only when there is more than one run. |
| Stories & Tests | Requirement text appears once per card (in the narrative); short titles; requirement tags show ID + status icon. **🔁 Regenerate stories & tests** moved here and is highlighted when requirements changed after the stories were generated. Only place with Jira push. |
| Export | BA deliverables only: traceability, Word BRD, Word review report, Excel, Jira CSV. Removed Jira push, provider settings, JSON, run-log tab. Added 🛠️ Diagnostics (log tail, Download log, token totals). |
| Shared | Reviewer name entered once, shared by all pages and remembered in `data/settings.json`. |
| Logger | `ria/logging_setup.py`: `data/logs/ria.log` + `errors.log` (tracebacks), daily rotation, 14 days, `RIA_LOG_LEVEL`. Privacy filter masks keys/tokens, e-mails, phone numbers; only IDs and counts are logged. |
| Removed files | `ria/ui/views/` (3 files), `ria/ui/legacy_loader.py`, `ria/services/glossary.py` (manual glossary), unused review widgets in `ria/ui/review.py`, view-guard code in `ria/ui/common.py` and `Home.py`. |

## Defects found during QA and fixed
| # | Defect | Fix |
|---|---|---|
| 1 | **Document IDs were reused** after removing the newest document (delete DOC-004 → next upload became DOC-004 again), so old runs, evidence links and audit entries pointed to the wrong file. Present since Phase 1. | Highest issued number kept per session in the `meta` table; cleared when the session is deleted |
| 2 | Deep-dive jumped back to REQ-001 after Approve / area approval (the label contains the status, so Streamlit saw a new widget) | Stable widget key |
| 3 | Logger masked timestamps as `[phone]` (mask ran over the whole formatted line; phone pattern too loose) | Mask only message and traceback; stricter phone patterns; regression test |
| 4 | "Requirements changed" check could miss a change made in the same second as story generation | Audit-log id mark stored when stories are generated |
| 5 | "Type changed" detection could never trigger (`created_at` does not change) | Run stores each document's type at start |
| 6 | Demo story narrative "I want the claim submission window is 45 days" | "I want to ensure that …" for statements; verb phrases kept |
| 7 | Excel download label wrapped to two lines; duplicate explanation lines in the email panel | Shorter label; one caption |

## Verification
- Full suite run twice: default environment and NumPy 2 (PyArrow converts every table) – results in the reply.
- New tests: `tests/test_v9.py` (logger, privacy, rotation, idempotency, automatic glossary, demo stories, documents
  changed, filter scope, document-ID reuse) and a rewritten `tests/test_app_smoke.py` (every requested UI behaviour).
- Browser check (Chromium) of Home and all four pages with demo data.
- Live server: health OK, all pages HTTP 200, no tracebacks in the server log, `errors.log` empty, no document text
  in `ria.log`.

## Deliberate deviations
- Transcript-error suggestions with Accept/Reject are gone with the glossary tab; likely errors are still flagged as
  findings (Specification-quality notes in the deep-dive). Existing accepted corrections in old databases still apply.
- Reviewer names and review notes are not written to the log file (privacy); they stay in the audit trail.

## Not testable here
Real Outlook, real Jira and real AI providers (simulations/mocks). The drag-and-drop uploader is covered through the
ingestion service (Streamlit's test tool cannot drive it).
