"""Code-based validation of LLM output (Build Prompt v2, Stage 8 - applied after every stage).

Nothing the model says is trusted blindly:
* citations must point to segments/statements that were actually given to the model
* evidence quotes must be found in the cited segment text
* hedged wording cannot be STATED; requirements cannot be stronger than their evidence
* speakers come from the segment metadata, not from the model
"""
from __future__ import annotations

import re

from ria.models.analysis import (
    ExtractedStatement, RequirementDraft, RequirementRecord, StatementRecord,
)
from ria.models.enums import DocumentType, EvidenceStatus, PrioritySource, Severity
from ria.models.segments import Segment
from ria.services.redaction import Redactor
from ria.validation.confidence import requirement_confidence
from ria.validation.evidence import verify_quote
from ria.validation.rules import find_vague_terms, is_hedged

_WEAK_ORDER = [EvidenceStatus.PROPOSED, EvidenceStatus.AMBIGUOUS, EvidenceStatus.INFERRED]


def validate_statements(statements: list[ExtractedStatement], *, run_id: str, chunk_id: str,
                        document_id: str, document_type: DocumentType, citable_ids: list[str],
                        sent_text: dict[str, str], segments: dict[str, Segment], start_number: int,
                        redactor: Redactor, threshold: float,
                        corrections: list[tuple[str, str]],
                        earlier: dict[str, StatementRecord] | None = None) -> list[StatementRecord]:
    """Convert one chunk's LLM statements to records with final IDs STM-nnnn."""
    citable = set(citable_ids)
    records: list[StatementRecord] = []
    key_to_id: dict[str, str] = {}
    for i, st in enumerate(statements):
        sid = f"STM-{start_number + i:04d}"
        if st.local_key in key_to_id:
            notes = [f"Duplicate local_key '{st.local_key}' from model"]
        else:
            notes = []
        key_to_id.setdefault(st.local_key, sid)
        cited = list(dict.fromkeys(x for x in st.segment_ids if x in citable))
        dropped = [x for x in st.segment_ids if x not in citable]
        if dropped:
            notes.append(f"Removed citation(s) not given to the model in this chunk: {', '.join(dropped)}")
        status = st.evidence_status
        ai = st.is_ai_suggestion or status == EvidenceStatus.INFERRED
        verified, score, matched = False, 0.0, None
        if not cited:
            status, ai = EvidenceStatus.UNSUPPORTED, True
            notes.append("No valid source segment cited")
        else:
            check = verify_quote(st.evidence_quote, cited, sent_text, threshold)
            verified, score, matched = check.verified, check.score, check.matched_segment_id
            if not verified:
                notes.append(f"Evidence check failed ({check.reason}; best match {check.score:.0f}%). "
                             f"Model said {status.value}.")
                status, ai = EvidenceStatus.UNSUPPORTED, True
        if status == EvidenceStatus.STATED and is_hedged(st.evidence_quote):
            status = EvidenceStatus.PROPOSED
            notes.append("Downgraded STATED → PROPOSED: the quote contains tentative wording")
        quote = redactor.restore(st.evidence_quote) or ""
        transcription = st.possible_transcription_issue
        t_note = redactor.restore(st.transcription_note)
        for original, intended in corrections:
            if re.search(rf"(?<!\w){re.escape(original)}(?!\w)", quote, re.I):
                transcription = True
                extra = f"Glossary correction accepted by reviewer: '{original}' → '{intended}'"
                t_note = f"{t_note} · {extra}" if t_note else extra
        seg = segments.get(matched or (cited[0] if cited else ""), None)
        records.append(StatementRecord(
            statement_id=sid, run_id=run_id, document_id=document_id, document_type=document_type,
            chunk_id=chunk_id, statement_type=st.statement_type, text=redactor.restore(st.text) or st.text,
            evidence_status=status, segment_ids=cited, evidence_quote=quote, quote_verified=verified,
            quote_score=round(score, 1), speaker=seg.speaker if seg else None,
            possible_transcription_issue=transcription, transcription_note=t_note,
            is_ai_suggestion=ai, validation_notes=notes))
    # Link corrections: inside this chunk (local_key) or to an earlier statement (STM-id).
    by_id = {r.statement_id: r for r in records}
    earlier = earlier or {}
    for st, rec in zip(statements, records):
        ref = (st.supersedes_local_key or "").strip()
        target = key_to_id.get(ref)
        if target and target != rec.statement_id:
            rec.supersedes_id = target
            by_id[target].superseded_by_id = rec.statement_id
        elif ref in earlier and earlier[ref].document_id == document_id:
            rec.supersedes_id = ref
            earlier[ref].superseded_by_id = rec.statement_id
        elif ref:
            rec.validation_notes.append(f"Unknown supersedes reference '{ref}' ignored")
    return records


def validate_requirement(draft: RequirementDraft, *, run_id: str, group: str,
                         group_statements: dict[str, StatementRecord], segments: dict[str, Segment],
                         known_actors: list[str], redactor: Redactor) -> RequirementRecord:
    notes: list[str] = []
    ids = list(dict.fromkeys(i for i in draft.source_statement_ids if i in group_statements))
    unknown = [i for i in draft.source_statement_ids if i not in group_statements]
    if unknown:
        notes.append(f"Removed unknown statement reference(s): {', '.join(unknown)}")
    sources = [group_statements[i] for i in ids]
    verified = [s for s in sources if s.quote_verified]
    status = draft.evidence_status
    ai = draft.is_ai_suggestion or status == EvidenceStatus.INFERRED

    if not verified:
        status, ai = EvidenceStatus.UNSUPPORTED, True
        notes.append("No verified source statement – shown as an AI suggestion only")
    elif status == EvidenceStatus.STATED and not any(s.evidence_status == EvidenceStatus.STATED for s in verified):
        weakest = next((w for w in _WEAK_ORDER if any(s.evidence_status == w for s in verified)),
                       EvidenceStatus.INFERRED)
        notes.append(f"Downgraded STATED → {weakest.value}: no source statement is STATED")
        status = weakest
    if sources and all(s.superseded_by_id for s in sources):
        notes.append("Based only on statements that were later corrected")
        if status == EvidenceStatus.STATED:
            status = EvidenceStatus.AMBIGUOUS

    # Actors must appear in the sources or in the known actor list (grounding check).
    source_text = " ".join(f"{s.text} {s.evidence_quote}" for s in sources).lower()
    known = {a.lower() for a in known_actors}
    actors, assumptions = [], list(draft.assumptions)
    for a in draft.actors:
        a = redactor.restore(a) or a
        if a.lower() in known or a.lower() in source_text or a.lower().rstrip("s") in source_text:
            actors.append(a)
        else:
            assumptions.append(f"AI-inferred actor (not in sources): {a}")
            notes.append(f"Actor '{a}' moved to assumptions – not found in sources")

    # Priority: STATED only if a cited segment actually mentions priority.
    priority_source = PrioritySource.NONE
    if draft.priority is not None:
        cited_text = " ".join(segments[sid].text.lower() for s in sources for sid in s.segment_ids if sid in segments)
        if draft.priority_stated and "priority" in cited_text and draft.priority.value.lower() in cited_text:
            priority_source = PrioritySource.STATED
        else:
            priority_source = PrioritySource.AI_SUGGESTED
            if draft.priority_stated:
                notes.append("Priority marked as stated, but no source mentions it – shown as AI-suggested")

    r = redactor.restore
    record = RequirementRecord(
        requirement_id="", run_id=run_id, group=group, title=r(draft.title) or draft.title,
        description=r(draft.description) or draft.description, requirement_type=draft.requirement_type,
        actors=actors, trigger=r(draft.trigger), preconditions=[r(x) for x in draft.preconditions],
        expected_behavior=r(draft.expected_behavior), exceptions=[r(x) for x in draft.exceptions],
        business_rules=[r(x) for x in draft.business_rules], priority=draft.priority,
        priority_source=priority_source, evidence_status=status, source_statement_ids=ids,
        segment_ids=list(dict.fromkeys(sid for s in sources for sid in s.segment_ids)),
        document_ids=list(dict.fromkeys(s.document_id for s in sources)),
        assumptions=[r(x) for x in assumptions], unresolved_questions=[r(x) for x in draft.unresolved_questions],
        is_ai_suggestion=ai, evidence_verified=bool(verified), validation_notes=notes,
    )
    record.vague_terms = find_vague_terms(record.description, record.expected_behavior,
                                          *[s.evidence_quote for s in sources])
    breakdown = requirement_confidence(record, sources)
    record.confidence_breakdown = breakdown
    record.confidence = breakdown.total
    return record


# ============================== Phase 3: findings and stories ==============================
from ria.models.analysis import (  # noqa: E402
    AcceptanceCriterion, FindingRecord, IssueDraft, StoryDraft, StoryRecord, TestScenarioRecord,
)
from ria.models.enums import SEVERITY_ORDER, DetectedBy, Priority  # noqa: E402
from ria.validation.values import numbers_in  # noqa: E402

_PLACEHOLDER = re.compile(r"\[TBC[^\]]*\]", re.I)
_GENERIC_ACTORS = {"user", "users", "the user", "system", "the system", "someone", "person", "stakeholder"}


def validate_issue(draft: IssueDraft, *, run_id: str, group_reqs: dict[str, RequirementRecord],
                   redactor: Redactor) -> FindingRecord | None:
    ids = list(dict.fromkeys(i for i in draft.requirement_ids if i in group_reqs))
    if not ids:
        return None
    notes = []
    unknown = [i for i in draft.requirement_ids if i not in group_reqs]
    if unknown:
        notes.append(f"Removed unknown requirement reference(s): {', '.join(unknown)}")
    from ria.models.enums import FindingType
    if draft.finding_type in (FindingType.CONTRADICTION, FindingType.DUPLICATE) and len(ids) < 2:
        return None  # a contradiction/duplicate needs two requirements
    reqs = [group_reqs[i] for i in ids]
    r = redactor.restore
    return FindingRecord(
        finding_id="", run_id=run_id, finding_type=draft.finding_type, severity=draft.severity,
        title=r(draft.title) or draft.title, description=r(draft.description) or draft.description,
        requirement_ids=ids, statement_ids=list(dict.fromkeys(s for q in reqs for s in q.source_statement_ids)),
        segment_ids=list(dict.fromkeys(s for q in reqs for s in q.segment_ids)),
        document_ids=list(dict.fromkeys(d for q in reqs for d in q.document_ids)),
        business_impact=r(draft.business_impact), dev_impact=r(draft.dev_impact), qa_impact=r(draft.qa_impact),
        clarification_question=r(draft.clarification_question), ask_whom=r(draft.ask_whom),
        suggested_resolution=r(draft.suggested_resolution), detected_by=DetectedBy.LLM, is_ai_suggestion=True,
        validation_notes=notes)


def _unverified(text: str, allowed: set[str]) -> list[str]:
    found = numbers_in(_PLACEHOLDER.sub(" ", text or ""))
    return sorted(n for n in found - allowed if n not in {"0", "1"})


def _ground_actor(actor: str | None, reqs: list[RequirementRecord], known: list[str],
                  source_text: str) -> tuple[str, str | None]:
    if not actor or actor.strip().lower() in _GENERIC_ACTORS:
        return "[TBC: actor]", "No specific actor known – clarify who uses this"
    a = actor.strip()
    pool = {x.lower() for r in reqs for x in r.actors} | {k.lower() for k in known}
    if a.lower() in pool or a.lower() in source_text or a.lower().rstrip("s") in source_text:
        return a, None
    return "[TBC: actor]", f"AI-proposed actor '{a}' not found in sources – replaced with a placeholder"


def validate_story(draft: StoryDraft, *, run_id: str, group: str, group_reqs: dict[str, RequirementRecord],
                   statements: dict[str, StatementRecord], known_actors: list[str], redactor: Redactor,
                   max_tests: int) -> tuple[StoryRecord, list[TestScenarioRecord]] | None:
    ids = list(dict.fromkeys(i for i in draft.requirement_ids if i in group_reqs))
    if not ids:
        return None
    reqs = [group_reqs[i] for i in ids]
    notes: list[str] = []
    unknown = [i for i in draft.requirement_ids if i not in group_reqs]
    if unknown:
        notes.append(f"Removed unknown requirement reference(s): {', '.join(unknown)}")
    r = redactor.restore
    src_stmts = [statements[s] for q in reqs for s in q.source_statement_ids if s in statements]
    source_text = " ".join([*(f"{q.title} {q.description} {q.expected_behavior or ''} {' '.join(q.business_rules)}"
                              for q in reqs), *(f"{s.text} {s.evidence_quote}" for s in src_stmts)]).lower()
    allowed = numbers_in(source_text)
    actor, actor_note = _ground_actor(r(draft.actor), reqs, known_actors, source_text)
    if actor_note:
        notes.append(actor_note)
    criteria = []
    for n, c in enumerate(draft.acceptance_criteria, start=1):
        given, when, then = (r(c.given) or "", r(c.when) or "", r(c.then) or "")
        bad = _unverified(f"{given} {when} {then}", allowed)
        criteria.append(AcceptanceCriterion(ac_id="", criterion_type=c.criterion_type, given=given, when=when,
                                            then=then, unverified_values=bad))
    if any(c.unverified_values for c in criteria):
        notes.append("Some acceptance criteria contain values not found in the sources – check before use")
    conflicting = [q.requirement_id for q in reqs if q.evidence_status == EvidenceStatus.CONFLICTING]
    if conflicting:
        notes.append(f"Based on conflicting requirement(s) {', '.join(conflicting)} – confirm the value first")
    priorities = [q.priority for q in reqs if q.priority]
    story = StoryRecord(
        story_id="", run_id=run_id, group=group, requirement_ids=ids, title=r(draft.title) or draft.title,
        actor=actor, capability=(r(draft.capability) or draft.capability).rstrip(". "),
        benefit=(r(draft.benefit) or "[TBC: business value]").rstrip(". "),
        priority=min(priorities, key=lambda p: SEVERITY_ORDER[Severity(p.value)]) if priorities else None,
        acceptance_criteria=criteria, segment_ids=list(dict.fromkeys(s for q in reqs for s in q.segment_ids)),
        assumptions=[r(x) for x in draft.assumptions], clarification_questions=[r(x) for x in draft.clarification_questions],
        validation_notes=notes, is_ai_suggestion=any(q.is_ai_suggestion for q in reqs))
    order = {Priority.HIGH: 0, Priority.MEDIUM: 1, Priority.LOW: 2}
    scenarios = []
    drafts = sorted(draft.test_scenarios, key=lambda t: order[t.priority])
    if len(drafts) > max_tests:
        story.validation_notes.append(f"{len(drafts) - max_tests} lower-priority test scenario(s) dropped "
                                      f"(limit {max_tests} per story)")
    for t in drafts[:max_tests]:
        steps = [r(x) or "" for x in t.steps]
        pre = [r(x) or "" for x in t.preconditions]
        expected = r(t.expected_result) or ""
        bad = _unverified(" ".join([t.title, *pre, *steps, expected]), allowed)
        scenarios.append(TestScenarioRecord(
            scenario_id="", run_id=run_id, story_id="", requirement_ids=ids, title=r(t.title) or t.title,
            scenario_type=t.scenario_type, priority=t.priority, preconditions=pre, steps=steps,
            expected_result=expected, unverified_values=bad,
            validation_notes=["Contains values not found in the sources"] if bad else []))
    return story, scenarios
