# Improvements made during Phase 1 (apply to Build Prompt v2 → v2.1)

| # | Improvement | Why |
|---|---|---|
| 1 | **Mock fixtures are per document, not per chunk.** Segment refs are relative (`S0002`) and filtered to the chunk's citable segments | Fixtures were breaking whenever the chunk-size setting changed |
| 2 | **`RequestMeta.citable_segment_ids`** passed with every call | The pipeline (and Mock) can reject citations outside the chunk |
| 3 | **Human "Confirm extraction" gate** + `analysis_readiness()` | A bad extraction (wrong reading order, missing speakers) would silently corrupt every AI output |
| 4 | **Duplicate upload detection** (SHA-256 per session) | Avoids double-counting the same requirement as two sources |
| 5 | **Teams VTT cue merging** (same speaker, up to 900 characters) | Teams splits one sentence into many cues, which made segments too small to cite meaningfully |
| 6 | **Unknown-speaker turns always flagged** (not only above 10%) | A single unattributed question can hide an open decision |
| 7 | **Document IDs are never reused** after deletion | Keeps old evidence links and audit logs unambiguous |
| 8 | **Raw files are not stored**, only extracted text, plus a "Delete session" action | Smaller privacy footprint |
| 9 | **Active session kept in the URL** (`?s=`) | A browser refresh no longer loses the user's place |
| 10 | **Evidence quote verification built early** (`ria/validation/evidence.py`) | Used to prove demo data is grounded; reused as Stage 8 |
| 11 | **Table rows rendered as `Column: value \| Column: value`** | `.;` separators made quotes hard to read and match |
| 12 | **Third conflicting source (policy PDF)** added to sample data | Tests 3-way contradictions (45 / 60 / 90 days), which is more realistic |
| 13 | **The type-dependent warnings are recalculated** when the user changes the document type | Warnings stay accurate after reclassification |

Carry-forward items for Phase 2:
- Name redaction (currently emails and phones are only detected).
- Show estimated calls and tokens per provider before the run (the chunk preview already exists).

# Improvements made during Phase 2

| # | Improvement | Why |
|---|---|---|
| 14 | **GitHub Models removed.** Generic OpenAI-compatible connector kept | GitHub retired the service in July 2026 |
| 15 | **Input modes A/B/C** decide which documents are analysed, and the mode is checked before the run | Prevents running "compare" without a BRD, for example |
| 16 | **Glossary-based transcript suggestions** with accept/reject. Source text is never altered | Addresses inaccurate transcripts without breaking evidence |
| 17 | **Cross-chunk corrections:** earlier transcript statements (with IDs) are sent to the model, and code validates the link | "Actually it's 45 days, not 30" often comes much later in a meeting |
| 18 | **Speaker taken from segment metadata**, not from the model | The model can mislabel speakers |
| 19 | **Requirement cannot be stronger than its evidence:** no STATED requirement without a STATED source | Stops "maybe SMS" becoming a confirmed requirement |
| 20 | **Actor grounding:** actors not found in the sources move to assumptions | Prevents invented roles |
| 21 | **Priority grounding:** "stated" only if a cited source mentions it | Prevents invented priorities |
| 22 | **Confidence caps** for weak statuses, e.g. AMBIGUOUS ≤ 60 | An ambiguous requirement scored 80 ("High") in testing |
| 23 | **IDF-weighted grouping** with stricter word matching | The domain noun "claim" pulled everything into one group, and "receive" matched "receipt" |
| 24 | **Automatic structured-output fallback** (json_schema → json_object → schema in prompt), remembered per provider | Not every model or endpoint supports strict JSON Schema |
| 25 | **Automatic chunk split on context-too-long** | Small models have small context windows |
| 26 | **Response cache:** re-runs cost no tokens | Saves quota and money |
| 27 | **Consent checkbox and redaction on by default** for external providers | Data protection |
| 28 | **Throttling setting** (`RIA_MIN_SECONDS_BETWEEN_CALLS`) | Helps with rate-limited free tiers |
| 29 | **Automatic Phase 1 → Phase 2 database upgrade** | Your existing sessions keep working |
| 30 | **Test that every UI table has consistent column types** | Catches the Arrow error you hit in Phase 1 |

# Improvements made during Phases 3–4

| # | Improvement | Why |
|---|---|---|
| 31 | **Code-based value contradiction detector** (durations, amounts, %, spoken numbers such as "twenty five thousand") across documents | Catches 45/60/90-day conflicts even with weak or no AI; it's deterministic and explainable |
| 32 | Contradiction rules **ignore values inside one document and different roles/currencies** | Avoids false conflicts (metro vs other-city limits, manager vs finance thresholds) |
| 33 | Contradictions **mark requirements CONFLICTING and lower confidence (≤45)** | Status stays consistent with the findings |
| 34 | **One AI issue-review call per functional area**, told which rule findings already exist | Fewer calls and no duplicate findings |
| 35 | **Stories, criteria and tests in one call per area**; tests capped per story (default 3), highest priority first | Saves quota; matches the "important test cases only" preference |
| 36 | **Number grounding:** any value in a criterion or test that isn't in the sources is flagged | Stops invented SLAs, amounts and limits |
| 37 | **Actor grounding for stories:** generic or unknown actors → `[TBC: actor]` | No "As a user…" stories |
| 38 | **Continue-run:** re-check issues or regenerate stories from the reviewed requirements without re-extracting | Matches the real BA flow: review first, then write stories |
| 39 | **Clarification answers** document type (authority 10), always included | Closes the clarification loop with evidence |
| 40 | **Optional parallel calls** with results in a deterministic order | Faster with Grok; IDs stay stable |
| 41 | **Bulk-approve** only safe items (STATED, ≥75, verified, no vague words) and **merge** duplicates | Saves review time without hiding risk |
| 42 | **BRD built by code** with DRAFT/DEMO watermark, Final mode = approved only, audit log and sign-off table | Audit-ready and no AI free text in the deliverable |
| 43 | **Excel summary uses live formulas** | Counts update if reviewers change statuses in Excel |
| 44 | **Evaluation script** with planted-issue recall and "must not happen" checks | Gives numbers for the AI challenge pitch |
| 45 | Fixed: duplicate widget keys when one finding shows in two tabs (found by UI tests) | Page crashed when a contradiction was open in both tabs |
| 46 | Warning when the Demo provider is used on your own documents | Previously it silently returned empty results |
