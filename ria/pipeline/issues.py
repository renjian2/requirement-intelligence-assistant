"""Rule-based issue detection (Stage 4/5, no LLM) and merging with LLM findings.

Rules are deterministic, explainable and work with any provider (including small local models
and the demo provider). The LLM adds what rules cannot see (missing permissions, exceptions,
non-numeric contradictions, duplicates).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from ria.models.analysis import (
    ConflictValue, DocumentUnderstanding, FindingRecord, RequirementRecord, StatementRecord,
)
from ria.models.enums import (
    DOCUMENT_TYPE_LABELS, MEETING_TYPES, REQUIREMENT_DOC_TYPES, SEVERITY_ORDER, DetectedBy, DocumentType,
    EvidenceStatus, FindingType, RequirementType, Severity, StatementType,
)
from ria.models.segments import SourceDocument
from ria.validation.values import Quantity, find_quantities

DEFAULT_ROLE_WORDS = {
    "employee", "manager", "finance", "approver", "admin", "administrator", "member", "customer", "analyst",
    "supervisor", "reviewer", "auditor", "agent", "representative", "broker", "client", "user", "owner", "hr",
    "payroll", "accountant", "director", "head", "officer", "vendor", "supplier", "patient", "doctor",
}
COMMON_ACRONYMS = {"INR", "USD", "EUR", "GBP", "PDF", "SMS", "ID", "IT", "HR", "API", "UI", "URL", "CSV", "XLSX",
                   "OK", "FAQ", "BRD", "MOM", "Q&A", "JPG", "PNG", "DOCX", "TXT", "AM", "PM"}
_STOP = {"system", "shall", "must", "should", "with", "from", "that", "this", "will", "have", "need", "needs",
         "also", "each", "every", "when", "then", "into", "after", "before", "able", "more", "only", "than",
         "other", "require", "required", "requires", "within", "above", "below", "over", "under", "days", "hours",
         "approval", "approve", "approved"}


@dataclass
class IssueContext:
    run_id: str
    docs: dict[str, SourceDocument]
    statements: dict[str, StatementRecord]
    requirements: list[RequirementRecord]
    understandings: dict[str, DocumentUnderstanding]
    project_glossary: list[tuple[str, str | None]]
    accepted_corrections: list[tuple[str, str]]


def _tokens(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-zA-Z]{4,}", (text or "").lower()):
        w = w[:-1] if w.endswith("s") and len(w) > 4 else w
        if w not in _STOP:
            out.add(w)
    return out


def _share_topic(a: set[str], b: set[str]) -> bool:
    return any(x == y or (len(x) >= 5 and len(y) >= 5 and x[:5] == y[:5]) for x in a for y in b)


def role_words(ctx: IssueContext) -> set[str]:
    words = set(DEFAULT_ROLE_WORDS)
    for u in ctx.understandings.values():
        for actor in u.actors:
            words.update(w.lower() for w in re.findall(r"[A-Za-z]{3,}", actor))
    for r in ctx.requirements:
        for actor in r.actors:
            words.update(w.lower() for w in re.findall(r"[A-Za-z]{3,}", actor))
    return words - {"the", "and", "all", "line", "senior", "team"}


def _roles_in(text: str, vocab: set[str]) -> set[str]:
    toks = {w.lower().rstrip("s") if len(w) > 4 else w.lower() for w in re.findall(r"[A-Za-z]{2,}", text or "")}
    return {w for w in toks if w in vocab}


def requirement_text(r: RequirementRecord) -> str:
    return " ".join(x for x in [r.description, r.expected_behavior or "", *r.business_rules] if x)


def _doc_label(doc: SourceDocument | None, fallback: str) -> str:
    if doc is None:
        return fallback
    return f"{doc.file_name} ({DOCUMENT_TYPE_LABELS[doc.document_type]})"


def _speakers(ctx: IssueContext, r: RequirementRecord | None = None, stmts: list[StatementRecord] | None = None) -> list[str]:
    items = stmts if stmts is not None else [ctx.statements[s] for s in (r.source_statement_ids if r else [])
                                             if s in ctx.statements]
    return list(dict.fromkeys(s.speaker for s in items if s.speaker))


def _ask_whom(ctx: IssueContext, reqs: list[RequirementRecord]) -> str:
    people: list[str] = []
    for r in reqs:
        people += _speakers(ctx, r)
        for d in r.document_ids:
            doc = ctx.docs.get(d)
            if doc and doc.document_type not in MEETING_TYPES:
                people.append(f"Owner of {doc.file_name}")
    return ", ".join(dict.fromkeys(people)) or "Business owner"


def _finding(ctx: IssueContext, ftype: FindingType, severity: Severity, title: str, description: str,
             reqs: list[RequirementRecord], question: str | None, rule: str, *,
             statements: list[StatementRecord] | None = None, values: list[ConflictValue] | None = None,
             resolution: str | None = None, ask: str | None = None, qa: str | None = None) -> FindingRecord:
    stmts = statements if statements is not None else [ctx.statements[s] for r in reqs for s in r.source_statement_ids
                                                       if s in ctx.statements]
    seg_ids = list(dict.fromkeys(sid for s in stmts for sid in s.segment_ids)) or \
        list(dict.fromkeys(sid for r in reqs for sid in r.segment_ids))
    return FindingRecord(
        finding_id="", run_id=ctx.run_id, finding_type=ftype, severity=severity, title=title,
        description=description, requirement_ids=[r.requirement_id for r in reqs],
        statement_ids=list(dict.fromkeys(s.statement_id for s in stmts)), segment_ids=seg_ids,
        document_ids=list(dict.fromkeys([d for r in reqs for d in r.document_ids] + [s.document_id for s in stmts])),
        values=values or [], clarification_question=question, ask_whom=ask or _ask_whom(ctx, reqs),
        suggested_resolution=resolution, qa_impact=qa, detected_by=DetectedBy.RULE, rule=rule)


# ---------------------------------------------------------------- value contradictions
def detect_value_conflicts(ctx: IssueContext) -> list[FindingRecord]:
    """Cross-document value differences for the same topic (functional area + unit + compatible roles).

    Values inside one document are not compared (tiered values like 'metro vs other cities' are
    intentional); within-document contradictions are left to the LLM."""
    vocab = role_words(ctx)
    items: dict[tuple[str, str], list[tuple[RequirementRecord, Quantity, set[str], set[str]]]] = defaultdict(list)
    for r in ctx.requirements:
        if r.evidence_status in (EvidenceStatus.UNSUPPORTED, EvidenceStatus.PROPOSED):
            continue
        text = requirement_text(r)
        for q in find_quantities(text):
            items[(r.group.split(" (part")[0], q.kind)].append((r, q, _roles_in(text, vocab), _tokens(text)))

    findings: list[FindingRecord] = []
    for (group, kind), entries in items.items():
        parent = list(range(len(entries)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        conflict_pairs = 0
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                ri, qi, roles_i, tok_i = entries[i]
                rj, qj, roles_j, tok_j = entries[j]
                if ri.requirement_id == rj.requirement_id or set(ri.document_ids) & set(rj.document_ids):
                    continue
                if roles_i and roles_j and roles_i != roles_j:
                    continue
                if kind == "amount" and qi.currency and qj.currency and qi.currency != qj.currency:
                    continue
                if abs(qi.value - qj.value) < 1e-9 or not _share_topic(tok_i - roles_i, tok_j - roles_j):
                    continue
                parent[find(i)] = find(j)
                conflict_pairs += 1
        if not conflict_pairs:
            continue
        clusters: dict[int, list[int]] = defaultdict(list)
        for i in range(len(entries)):
            clusters[find(i)].append(i)
        for members in clusters.values():
            if len(members) < 2:
                continue
            reqs = list({entries[i][0].requirement_id: entries[i][0] for i in members}.values())
            if len({round(entries[i][1].value, 6) for i in members}) < 2:
                continue
            values = [ConflictValue(requirement_id=entries[i][0].requirement_id, value=entries[i][1].display,
                                    document_ids=entries[i][0].document_ids) for i in members]
            doc_types = {ctx.docs[d].document_type for r in reqs for d in r.document_ids if d in ctx.docs}
            severity = Severity.HIGH if doc_types & (REQUIREMENT_DOC_TYPES | {DocumentType.BUSINESS_POLICY}) \
                else Severity.MEDIUM
            listing = "; ".join(
                f"{v.value} in {', '.join(_doc_label(ctx.docs.get(d), d) for d in v.document_ids)} ({v.requirement_id})"
                for v in values)
            label = {"duration": "time limit", "amount": "amount/threshold", "percent": "percentage"}[kind]
            findings.append(_finding(
                ctx, FindingType.CONTRADICTION, severity, f"Different {label} for '{group}'",
                f"The sources give different values for the same rule: {listing}.", reqs,
                f"Which {label} is correct for '{group}'? Sources say: {listing}.", "value_conflict",
                values=values, resolution="Confirm one value, update the other documents, then approve one "
                                          "requirement and reject the others.",
                qa="Test cases cannot be written until one value is confirmed."))
    return findings


# ---------------------------------------------------------------- requirement-level rules
def _question(text: str) -> str:
    text = text.strip()
    if "?" in text:
        return text[:text.index("?") + 1]
    return text.rstrip(". ") + "?"


def detect_requirement_rules(ctx: IssueContext) -> list[FindingRecord]:
    vocab = role_words(ctx)
    out: list[FindingRecord] = []
    # Amounts stated WITH a currency per functional area: a spoken "twenty five thousand" next to
    # "INR 25,000" elsewhere is not worth a separate question.
    with_currency: dict[str, set[float]] = defaultdict(set)
    for r in ctx.requirements:
        for q in find_quantities(requirement_text(r)):
            if q.kind == "amount" and q.currency:
                with_currency[r.group.split(" (part")[0]].add(q.value)
    for r in ctx.requirements:
        if r.evidence_status == EvidenceStatus.UNSUPPORTED:
            continue
        text = requirement_text(r)
        if r.vague_terms:
            terms = ", ".join(f"'{t}'" for t in r.vague_terms)
            out.append(_finding(ctx, FindingType.AMBIGUITY, Severity.MEDIUM,
                                f"Not measurable: {terms} in {r.requirement_id}",
                                f"{r.requirement_id} uses vague wording ({terms}) that cannot be tested.", [r],
                                f"What measurable target should replace {terms} in '{r.title}' "
                                f"(e.g. time, count, percentage)?", "vague_terms",
                                qa="Cannot write a pass/fail test without a measurable target."))
        for q in find_quantities(text):
            if q.kind == "amount" and q.currency is None \
                    and q.value not in with_currency.get(r.group.split(" (part")[0], set()):
                out.append(_finding(ctx, FindingType.AMBIGUITY, Severity.MEDIUM,
                                    f"Amount without currency in {r.requirement_id}",
                                    f"The amount {q.display.replace(' (no currency)', '')} in {r.requirement_id} has "
                                    f"no currency or unit.", [r],
                                    f"Which currency applies to the amount {q.display.replace(' (no currency)', '')} "
                                    f"in '{r.title}'?", "amount_without_currency"))
                break
        if r.evidence_status == EvidenceStatus.PROPOSED:
            out.append(_finding(ctx, FindingType.SCOPE_ISSUE, Severity.LOW,
                                f"Only proposed, not decided: {r.title}",
                                f"{r.requirement_id} was suggested but not agreed. It should stay out of scope "
                                f"until someone confirms it.", [r],
                                f"Is '{r.title}' in scope for this release?", "proposed_only"))
        elif (r.requirement_type == RequirementType.FUNCTIONAL and not r.actors and not r.vague_terms
              and not _roles_in(text, vocab)):
            out.append(_finding(ctx, FindingType.GAP, Severity.LOW, f"No actor for {r.requirement_id}",
                                f"{r.requirement_id} does not say who performs or receives this behaviour.", [r],
                                f"Which role performs or receives '{r.title}'?", "missing_actor"))
    return out


# ---------------------------------------------------------------- statement-level rules
def detect_statement_rules(ctx: IssueContext) -> list[FindingRecord]:
    out: list[FindingRecord] = []
    req_by_stmt: dict[str, list[RequirementRecord]] = defaultdict(list)
    for r in ctx.requirements:
        for s in r.source_statement_ids:
            req_by_stmt[s].append(r)
    for st in ctx.statements.values():
        reqs = req_by_stmt.get(st.statement_id, [])
        if st.statement_type == StatementType.OPEN_QUESTION and st.evidence_status != EvidenceStatus.UNSUPPORTED:
            out.append(_finding(ctx, FindingType.GAP, Severity.MEDIUM, "Open question not answered",
                                f"Raised in {_doc_label(ctx.docs.get(st.document_id), st.document_id)} and not "
                                f"answered: {st.text}", reqs, _question(st.text),
                                "open_question", statements=[st],
                                ask=st.speaker or "Meeting participants",
                                qa="The behaviour for this case cannot be tested until it is decided."))
        if st.possible_transcription_issue and "accepted by reviewer" not in (st.transcription_note or ""):
            out.append(_finding(ctx, FindingType.POSSIBLE_TRANSCRIPTION_ERROR, Severity.LOW,
                                f"Possible transcription error in {st.statement_id}",
                                f"Quote: “{st.evidence_quote}”. {st.transcription_note or ''}".strip(), reqs,
                                st.transcription_note or "Is this word transcribed correctly?",
                                "transcription", statements=[st], ask=st.speaker or "Meeting participants",
                                resolution="Accept the correction on the Glossary page and re-run, or edit "
                                           "the requirement."))
    # Unknown speakers: one finding per transcript document.
    by_doc: dict[str, list[StatementRecord]] = defaultdict(list)
    for st in ctx.statements.values():
        doc = ctx.docs.get(st.document_id)
        if doc and (doc.document_type in MEETING_TYPES or doc.detected_transcript) and not st.speaker \
                and st.statement_type not in (StatementType.CONTEXT,):
            by_doc[st.document_id].append(st)
    for doc_id, stmts in by_doc.items():
        reqs = list({r.requirement_id: r for s in stmts for r in req_by_stmt.get(s.statement_id, [])}.values())
        ids = ", ".join(s.statement_id for s in stmts)
        out.append(_finding(ctx, FindingType.LOW_CONFIDENCE_EVIDENCE, Severity.LOW,
                            f"{len(stmts)} statement(s) from an unknown speaker",
                            f"In {_doc_label(ctx.docs.get(doc_id), doc_id)} these statements cannot be attributed "
                            f"to a person: {ids}.", reqs, "Who said this, and is it an agreed decision?",
                            "unknown_speaker", statements=stmts, ask="Meeting organiser"))
    return out


# ---------------------------------------------------------------- undefined terms
def detect_undefined_terms(ctx: IssueContext) -> list[FindingRecord]:
    defined = {t.lower() for t, m in ctx.project_glossary if m}
    terms: dict[str, str] = {}
    for u in ctx.understandings.values():
        for g in u.glossary:
            if not g.defined_in_source and g.term.lower() not in defined:
                terms.setdefault(g.term.lower(), g.term)
        for a in u.undefined_acronyms:
            if a.upper() not in COMMON_ACRONYMS and a.lower() not in defined:
                terms.setdefault(a.lower(), a)
    # 'GL' and 'GL code' are the same issue - keep the longer term.
    keys = list(terms)
    for k in keys:
        if any(other != k and re.search(rf"\b{re.escape(k)}\b", other) for other in keys):
            terms.pop(k, None)
    out: list[FindingRecord] = []
    for term in terms.values():
        pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.I)
        stmts = [s for s in ctx.statements.values() if pattern.search(f"{s.text} {s.evidence_quote}")]
        reqs = [r for r in ctx.requirements if pattern.search(requirement_text(r))]
        if not stmts and not reqs:
            continue
        out.append(_finding(ctx, FindingType.UNDEFINED_TERM, Severity.MEDIUM, f"Undefined term: {term}",
                            f"'{term}' is used but never defined in the sources or the project glossary.", reqs,
                            f"What does '{term}' mean, and where does its list of values come from?",
                            "undefined_term", statements=stmts,
                            resolution="Add the definition to the project glossary or the BRD glossary."))
    return out


# ---------------------------------------------------------------- merge
def _overlap(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def merge_findings(rule: list[FindingRecord], llm: list[FindingRecord], max_llm_per_group: int = 8) -> list[FindingRecord]:
    """Rules first; LLM findings that repeat a rule finding (same type, overlapping requirements) are dropped."""
    out = list(rule)
    for f in llm:
        if f.finding_type in (FindingType.AMBIGUITY, FindingType.CONTRADICTION, FindingType.SCOPE_ISSUE,
                              FindingType.UNDEFINED_TERM):
            if any(r.finding_type == f.finding_type and _overlap(r.requirement_ids, f.requirement_ids) >= 0.5
                   for r in rule):
                continue
        out.append(f)
    out.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.finding_type.value, f.requirement_ids[:1]))
    for n, f in enumerate(out, start=1):
        f.finding_id = f"FND-{n:03d}"
    return out


def run_rules(ctx: IssueContext) -> list[FindingRecord]:
    return (detect_value_conflicts(ctx) + detect_requirement_rules(ctx) + detect_statement_rules(ctx)
            + detect_undefined_terms(ctx))
