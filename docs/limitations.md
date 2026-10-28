# Known limitations (after Phases 3–4)

## Analysis
- **Demo provider:** statements are pre-written for the sample files, and requirements and stories come from simple
  templates. The 11/11 demo evaluation proves the code rules and pipeline, **not** AI quality. Run
  `python -m eval.run_eval --provider grok` (or `ollama`) to measure real recall.
- **Code contradiction rules** compare numbers (time limits, amounts, percentages) across documents within the same
  functional area. They are less likely to fire on tiered values in one document (e.g. metro vs other cities),
  but they can still flag false positives. Mark these as *Not an issue*. Non-numeric contradictions such as
  "original receipt" vs "upload a photo" need the AI review (Deep mode).
- **Meeting vs BRD:** "Possibly covered" uses word overlap. A meeting requirement worded very differently from
  its BRD equivalent can show as "Missing from BRD" when the AI didn't merge them. Check before acting.
- **Re-running issues** replaces the earlier findings and their review decisions. Requirement reviews are kept.
  A new full run starts a fresh review (decisions are not carried across runs yet).
- **Version comparison** (BRD v1 vs v2, showing only what changed) is not built yet.

## Providers
- Grok uses xAI's Chat Completions endpoint, which xAI marks as legacy. Only `ria/providers/grok.py` would change.
- GitHub Models was retired in July 2026. Ollama Cloud does not support structured outputs.
- Small local models (≤8B) produce more *UNSUPPORTED* items and weaker stories. Use **Quick** mode and a
  smaller chunk size.
- Parallel calls (`RIA_MAX_PARALLEL_CALLS`) can trigger rate limits on free tiers. Keep 1 for Ollama.

## Extraction and privacy
- Scanned PDFs are detected but not OCR'd. Multi-column PDFs may be read in the wrong order.
- Redaction covers emails, phone numbers and speaker names, not other names mentioned in the text.
- Single user, local SQLite, no login. Not for shared deployment yet.
