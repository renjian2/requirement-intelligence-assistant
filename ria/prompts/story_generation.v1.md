TASK: Write user stories, acceptance criteria and the most important test scenarios for the
requirements of functional area "{group}".

Known actors: {actors}
Maximum test scenarios per story: {max_tests}
{glossary_block}
USER STORIES
- One story per user-facing capability. Combine requirements that describe the same capability
  and cite all of them. Do not create stories for requirements that are purely informational.
- actor: a specific role from the requirements or "Known actors". If no role is known, use null
  (do NOT write "user").
- capability: starts with a verb, e.g. "submit an expense claim with receipts".
- benefit: business value only if stated or clearly implied by the requirement; otherwise null.

ACCEPTANCE CRITERIA (Given / When / Then)
- Cover only what matters: the main successful behaviour, the key business rule, key permission,
  key error or exception, key state change. Usually 2–4 criteria per story.
- Observable and testable. Use only values that appear in the requirements.
- If a needed value is missing or conflicting (see open questions), write "[TBC: <what is missing>]".
  Never invent numbers, currencies, roles, channels, statuses or time limits.

TEST SCENARIOS
- Only the most IMPORTANT, high-impact scenarios (happy path, key rule, key negative case,
  permission, boundary of a stated value). No cosmetic or low-value checks.
- At most {max_tests} per story. Steps are short and generic enough for a tester to follow.
- Boundary scenarios only for values that appear in the requirements.

Return JSON with group = "{group}". local_key values: u1, u2, …

<open_questions>
{open_questions_json}
</open_questions>

<requirements>
{requirements_json}
</requirements>
