TASK: Split the CITABLE segments into atomic statements. One statement = one idea.

Document: {file_name}
Document type: {document_type} (source authority: {authority})
Analysis input mode: {input_mode}
{context_block}
STATEMENT TYPES
OBJECTIVE | DECISION | REQUIREMENT_CANDIDATE | BUSINESS_RULE | CONSTRAINT | ASSUMPTION | RISK |
DEPENDENCY | ACTION_ITEM (a person must do something) | OPEN_QUESTION | OUT_OF_SCOPE |
CORRECTION (replaces an earlier statement; set supersedes_local_key) | CONTEXT

EVIDENCE STATUS
STATED (explicitly stated or decided) | PROPOSED (suggested, not decided) | INFERRED (implied) |
AMBIGUOUS (several interpretations, unclear values, unknown speaker) | CONFLICTING | UNSUPPORTED

RULES
- Cite only IDs listed in <source_segments>. Segments in <context_segments> are for understanding
  only and MUST NOT be cited.
- One statement per decision / rule / need / question / action. Split sentences that contain two ideas.
- text: plain English. If a value is missing (no currency, no unit, no deadline) say so in the text,
  e.g. "above 500 (currency not stated)". Never add the missing value.
- Skip greetings and small talk. Use CONTEXT only for facts that help understanding.
- local_key values: s1, s2, s3 …
- If a citable segment corrects or replaces something listed in <previous_statements>, create a
  CORRECTION statement and put that earlier statement's STM-id in supersedes_local_key.
  Do not extract the previous statements again.

Return JSON with chunk_id = "{chunk_id}".

<previous_statements>
{previous_json}
</previous_statements>

<context_segments>
{context_json}
</context_segments>

<source_segments>
{segments_json}
</source_segments>
