"""Traceability matrix, meeting-vs-BRD comparison and clarification-question list (code only)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ria.models.analysis import FindingRecord, RequirementRecord, StatementRecord, StoryRecord, TestScenarioRecord
from ria.models.enums import (
    EXCLUDED_REVIEW, MEETING_TYPES, REQUIREMENT_DOC_TYPES, SEVERITY_ORDER, FindingType, ReviewStatus, Severity,
)
from ria.models.segments import SourceDocument


@dataclass
class TraceRow:
    requirement_id: str
    title: str
    review_status: str
    evidence_status: str
    confidence: int
    documents: str
    segments: str
    statements: str
    stories: str
    criteria: str
    scenarios: str
    findings: str
    gaps: list[str] = field(default_factory=list)


def traceability(reqs: list[RequirementRecord], stories: list[StoryRecord], scenarios: list[TestScenarioRecord],
                 findings: list[FindingRecord], docs: dict[str, SourceDocument]) -> list[TraceRow]:
    rows = []
    for r in reqs:
        st = [s for s in stories if r.requirement_id in s.requirement_ids and s.review_status not in EXCLUDED_REVIEW]
        sc = [t for t in scenarios if t.story_id in {s.story_id for s in st} and t.review_status not in EXCLUDED_REVIEW]
        fi = [f for f in findings if r.requirement_id in f.requirement_ids and f.review_status != ReviewStatus.REJECTED]
        gaps = []
        if r.review_status in EXCLUDED_REVIEW:
            gaps.append("Requirement rejected / out of scope")
        else:
            if not r.evidence_verified:
                gaps.append("No verified evidence")
            if not st:
                gaps.append("No user story")
            elif not sc:
                gaps.append("No test scenario")
            if any(f.finding_type == FindingType.CONTRADICTION and f.review_status != ReviewStatus.RESOLVED for f in fi):
                gaps.append("Unresolved contradiction")
        rows.append(TraceRow(
            requirement_id=r.requirement_id, title=r.title, review_status=r.review_status.value,
            evidence_status=r.evidence_status.value, confidence=r.confidence,
            documents=", ".join(docs[d].file_name if d in docs else d for d in r.document_ids),
            segments=", ".join(r.segment_ids), statements=", ".join(r.source_statement_ids),
            stories=", ".join(s.story_id for s in st),
            criteria=", ".join(a.ac_id for s in st for a in s.acceptance_criteria),
            scenarios=", ".join(t.scenario_id for t in sc), findings=", ".join(f.finding_id for f in fi), gaps=gaps))
    return rows


# ---------------------------------------------------------------- meeting vs BRD
def _tok(text: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[a-z]{4,}", text.lower())}


@dataclass
class ComparisonRow:
    category: str
    requirement_id: str
    title: str
    detail: str


def compare_meeting_vs_brd(reqs: list[RequirementRecord], findings: list[FindingRecord],
                           docs: dict[str, SourceDocument]) -> list[ComparisonRow]:
    """Classify requirements by where they come from. Only meaningful when both meeting and BRD exist."""
    def kinds(r: RequirementRecord) -> tuple[bool, bool]:
        types = {docs[d].document_type for d in r.document_ids if d in docs}
        return bool(types & MEETING_TYPES), bool(types & REQUIREMENT_DOC_TYPES)

    conflict_ids = {rid: f.finding_id for f in findings if f.finding_type == FindingType.CONTRADICTION
                    and f.review_status != ReviewStatus.REJECTED for rid in f.requirement_ids}
    active = [r for r in reqs if r.review_status not in EXCLUDED_REVIEW]
    brd_only = [r for r in active if kinds(r) == (False, True)]
    rows: list[ComparisonRow] = []
    for r in active:
        meeting, brd = kinds(r)
        if r.requirement_id in conflict_ids:
            rows.append(ComparisonRow("Conflict", r.requirement_id, r.title,
                                      f"Values differ between sources – see {conflict_ids[r.requirement_id]}"))
        elif meeting and brd:
            rows.append(ComparisonRow("In both", r.requirement_id, r.title, "Discussed and documented"))
        elif meeting:
            mt = _tok(f"{r.title} {r.description}")
            best, score = None, 0.0
            for b in brd_only:
                bt = _tok(f"{b.title} {b.description}")
                sim = len(mt & bt) / max(1, min(len(mt), len(bt)))
                if sim > score:
                    best, score = b, sim
            if best is not None and score >= 0.5:
                rows.append(ComparisonRow("Possibly covered", r.requirement_id, r.title,
                                          f"Similar to {best.requirement_id} in the BRD – check the wording matches"))
            else:
                rows.append(ComparisonRow("Missing from BRD", r.requirement_id, r.title,
                                          "Discussed in the meeting but not found in the BRD – add it or reject it"))
        elif brd:
            rows.append(ComparisonRow("Not discussed", r.requirement_id, r.title,
                                      "In the BRD but not mentioned in the meeting (may be fine)"))
    order = {"Conflict": 0, "Missing from BRD": 1, "Possibly covered": 2, "Not discussed": 3, "In both": 4}
    return sorted(rows, key=lambda x: (order[x.category], x.requirement_id))


# ---------------------------------------------------------------- clarification questions
@dataclass
class Question:
    question: str
    ask_whom: str
    severity: str
    source: str            # FND-xxx or REQ-xxx
    requirement_ids: list[str]


def clarification_questions(findings: list[FindingRecord], reqs: list[RequirementRecord],
                            statements: dict[str, StatementRecord]) -> list[Question]:
    out: list[Question] = []
    seen: set[str] = set()

    def key(q: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", q.lower()).strip()

    for f in sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity]):
        if f.review_status in (ReviewStatus.REJECTED, ReviewStatus.RESOLVED) or not f.clarification_question:
            continue
        k = key(f.clarification_question)
        if k in seen:
            continue
        seen.add(k)
        out.append(Question(f.clarification_question, f.ask_whom or "Business owner", f.severity.value, f.finding_id,
                            f.requirement_ids))
    for r in reqs:
        if r.review_status in EXCLUDED_REVIEW:
            continue
        speakers = list(dict.fromkeys(statements[s].speaker for s in r.source_statement_ids
                                      if s in statements and statements[s].speaker))
        for q in r.unresolved_questions:
            if q.lower().startswith("demo mode") or key(q) in seen:
                continue
            seen.add(key(q))
            out.append(Question(q, ", ".join(speakers) or "Business owner", Severity.MEDIUM.value, r.requirement_id,
                                [r.requirement_id]))
    return out
