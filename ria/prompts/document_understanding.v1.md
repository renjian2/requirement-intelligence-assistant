TASK: Build a short factual overview of ONE document so later steps understand its context.

Document: {file_name}
Document type: {document_type}
{digest_note}

Extract only what the document states:
- domain, problem_statement, objectives
- stakeholders, actors (roles that use or act in the process), systems, modules / functional areas
- data_entities (business objects such as claim, receipt, invoice)
- glossary: business terms; give a meaning ONLY if the document defines it, otherwise meaning = null
  and defined_in_source = false
- undefined_acronyms: acronyms used but never expanded

Use null or empty lists when something is not stated. Keep each list item short (max 6 words).

<source_segments>
{segments_json}
</source_segments>
