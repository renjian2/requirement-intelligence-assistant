"""Business-facing views of the analysis.

* Technical evidence codes (STATED, CONFLICTING, …) are translated into five actionable statuses.
* Findings are classified (conflict / business question / internal wording note) so the clarification
  e-mail and the questions CSV only contain what a stakeholder has to decide.
"""
from __future__ import annotations

from collections import defaultdict
from enum import Enum

from ria.models.analysis import FindingRecord, RequirementRecord
from ria.models.enums import ACCEPTED_REVIEW, EXCLUDED_REVIEW, EvidenceStatus, FindingType, ReviewStatus

_CLOSED = (ReviewStatus.RESOLVED, ReviewStatus.REJECTED)


class ReqStatus(str, Enum):
    CONFLICT = "🔴 Conflict Detected"
    CLARIFY = "🟡 Needs Clarification"
    REVIEW = "⚪ Confirm Requirement"
    APPROVED = "🟢 Approved (Ready for Stories)"
    EXCLUDED = "❌ Out of Scope / Rejected"


STATUS_ORDER = [ReqStatus.CONFLICT, ReqStatus.CLARIFY, ReqStatus.REVIEW, ReqStatus.APPROVED, ReqStatus.EXCLUDED]


def open_findings_by_requirement(findings: list[FindingRecord]) -> dict[str, list[FindingRecord]]:
    out: dict[str, list[FindingRecord]] = defaultdict(list)
    for f in findings:
        if f.review_status in _CLOSED:
            continue
        for rid in f.requirement_ids:
            out[rid].append(f)
    return out


def requirement_status(r: RequirementRecord, open_findings: list[FindingRecord],
                       conflict_resolved: bool = False) -> ReqStatus:
    """Human decisions win; otherwise open conflicts > questions/vagueness > clean.
    `conflict_resolved`: every contradiction on this requirement was resolved/dismissed by a reviewer."""
    if r.review_status in EXCLUDED_REVIEW:
        return ReqStatus.EXCLUDED
    if r.review_status in ACCEPTED_REVIEW:
        return ReqStatus.APPROVED
    if any(f.finding_type == FindingType.CONTRADICTION for f in open_findings) or (
            r.evidence_status == EvidenceStatus.CONFLICTING and not conflict_resolved):
        return ReqStatus.CONFLICT
    if (r.review_status == ReviewStatus.NEEDS_CLARIFICATION or open_findings or r.vague_terms
            or r.unresolved_questions and not all(q.lower().startswith("demo mode") for q in r.unresolved_questions)
            or r.evidence_status in (EvidenceStatus.AMBIGUOUS, EvidenceStatus.PROPOSED, EvidenceStatus.UNSUPPORTED)):
        return ReqStatus.CLARIFY
    return ReqStatus.REVIEW


def requirement_statuses(reqs: list[RequirementRecord], findings: list[FindingRecord]) -> dict[str, ReqStatus]:
    by_req = open_findings_by_requirement(findings)
    closed = {rid for f in findings if f.finding_type == FindingType.CONTRADICTION and f.review_status in _CLOSED
              for rid in f.requirement_ids}
    return {r.requirement_id: requirement_status(r, by_req.get(r.requirement_id, []),
                                                 conflict_resolved=r.requirement_id in closed)
            for r in reqs}


# ---------------------------------------------------------------- finding classification
class Bucket(str, Enum):
    CRITICAL = "🚨 Critical Discrepancies"
    QUESTIONS = "❓ Open Stakeholder Questions"
    QUALITY = "✏️ Specification Quality"


# Rules whose findings are internal BA wording work, not questions for the client.
_QUALITY_RULES = {"vague_terms", "transcription"}
_QUALITY_TYPES = {FindingType.POSSIBLE_TRANSCRIPTION_ERROR, FindingType.DUPLICATE}


def finding_bucket(f: FindingRecord) -> Bucket:
    if f.finding_type == FindingType.CONTRADICTION:
        return Bucket.CRITICAL
    if f.finding_type in _QUALITY_TYPES or f.rule in _QUALITY_RULES:
        return Bucket.QUALITY
    return Bucket.QUESTIONS


def bucket_findings(findings: list[FindingRecord], include_closed: bool = False) -> dict[Bucket, list[FindingRecord]]:
    out: dict[Bucket, list[FindingRecord]] = {b: [] for b in Bucket}
    for f in findings:
        if include_closed or f.review_status not in _CLOSED:
            out[finding_bucket(f)].append(f)
    return out


def stakeholder_findings(findings: list[FindingRecord]) -> list[FindingRecord]:
    """Conflicts and business questions – everything except internal BA wording notes."""
    return [f for f in findings if finding_bucket(f) != Bucket.QUALITY]


def findings_in_scope(findings: list[FindingRecord], requirement_ids: set[str] | None) -> list[FindingRecord]:
    """Findings that belong to the requirements currently shown.

    ``None`` = no filter active: every finding, including session-level ones without requirements (e.g. an
    open meeting question). With a filter, only findings linked to at least one shown requirement."""
    if requirement_ids is None:
        return list(findings)
    return [f for f in findings if set(f.requirement_ids) & requirement_ids]
