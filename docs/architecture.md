# Architecture (after Phase 2)

```
Upload ─► IngestionService: validate → extract → segment (DOC-xxx-Snnnn) → quality checks → SQLite
Review ─► extraction auto-confirmed (type editable) · glossary built automatically from Stage 1
Run    ─► AnalysisOrchestrator(input mode A/B/C, depth Quick/Deep, redaction, cache)
           Stage 1  DocumentUnderstanding   (Deep only, 1 call/document)
           Stage 2  StatementExtraction     (1 call/chunk; earlier statements sent for cross-chunk corrections;
                                             auto-split on context-too-long)
           Stage 3  RequirementSynthesis    (1 call per functional-area group, IDF-weighted grouping)
           after each stage → validators.py (citations, quote match, hedge downgrade, evidence ceiling,
                                             actor/priority grounding, confidence + caps)
LLMCaller ─► cache (SQLite) · usage · per-call log ─► LLMProvider
              ├─ GrokProvider / OpenAICompatibleProvider  (chat/completions, json_schema → json_object → prompt)
              ├─ OllamaProvider                          (/api/chat, format = JSON Schema)
              └─ MockProvider                            (fixtures + rule-based Stage 3, demo only)
```

Rules enforced by code (not by the model): the app assigns all IDs. The model may cite only text it
was given. Quotes are fuzzy-matched (≥ 85%). Tentative wording cannot be STATED. A requirement is never
stronger than its evidence. Speakers come from segment metadata. Actors and priorities must be grounded
in the sources. Fatal provider errors stop the run, other failures are logged, and partial results are kept.

Logging: every module logs through ria/logging_setup.py (data/logs/ria.log + errors.log, daily rotation,
privacy filter masking keys, e-mails and phone numbers; only IDs and counts are logged).
