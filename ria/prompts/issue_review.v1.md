TASK: Review the requirements of functional area "{group}" like a senior business analyst and report
the IMPORTANT problems that would block development or testing.

Analysis input mode: {input_mode}
Known actors: {actors}
{glossary_block}
ALREADY FOUND BY RULES (do not repeat these):
{known_findings}

LOOK FOR
- CONTRADICTION: requirements that cannot all be true (different values, roles, channels, rules).
  Name the conflicting requirement IDs and the different values. Never decide which one is right.
- DUPLICATE: requirements that say the same thing. The first ID in requirement_ids is the one to keep.
- GAP: information a developer or tester needs but nobody stated: who may do it (permissions), what
  happens on rejection / error / timeout, validation rules, notification recipient / channel / content,
  data retention, audit, edge cases, system of record, integration details.
- AMBIGUITY: wording with several possible meanings.
- UNDEFINED_TERM: business terms or acronyms used without definition.
- SCOPE_ISSUE: items whose scope is unclear or only proposed.

RULES
- Report only problems a stakeholder must answer. Maximum 6 findings. Skip cosmetic issues.
- requirement_ids must be IDs from <requirements>. Never invent IDs.
- clarification_question: one concrete question that a business person can answer.
- ask_whom: a role or person that appears in the requirements, otherwise null.
- severity: HIGH = blocks development or causes wrong behaviour; MEDIUM = needed before testing;
  LOW = nice to clarify.
- Do not propose values (days, amounts, roles). Ask for them.
- local_key values: f1, f2, …

Return JSON with group = "{group}".

<requirements>
{requirements_json}
</requirements>
