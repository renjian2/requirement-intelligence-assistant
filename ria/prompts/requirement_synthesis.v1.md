TASK: Turn the statements of functional area "{group}" into clear, testable requirements.

Analysis input mode: {input_mode}
Known actors: {actors}
{glossary_block}
RULES
- Every requirement must list the statement IDs it is based on in source_statement_ids
  (only IDs from <statements>). Combine statements that express the same need and cite ALL of them.
- When statements disagree on a value (days, amounts, roles, channels), create ONE requirement with
  evidence_status = CONFLICTING, cite all disagreeing statements, and add an unresolved_question that
  names the different values and their source documents. Never choose a value yourself.
- A statement marked superseded_by was corrected later: use the correction and cite both.
- If every source statement is PROPOSED, the requirement is PROPOSED.
- If a needed detail is missing (actor, trigger, value, currency, SLA, channel), leave the field empty
  and add an unresolved_question. Do not fill it from general knowledge.
- actors: only roles named in the statements or in "Known actors".
- priority: set priority_stated = true only if a statement states the priority; otherwise you may
  suggest one with priority_stated = false, or leave it null.
- requirement_type: BUSINESS | FUNCTIONAL | NON_FUNCTIONAL | DATA | INTEGRATION | REPORTING |
  SECURITY | COMPLIANCE | ACCESSIBILITY
- Do not create requirements from ACTION_ITEM, OPEN_QUESTION, OUT_OF_SCOPE or CONTEXT statements.
- local_key values: r1, r2, r3 …

Return JSON with group = "{group}".

<statements>
{statements_json}
</statements>
