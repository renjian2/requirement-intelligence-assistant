"""Deterministic confidence indicator (0-100). It is an indicator, not a statistical guarantee.
The model's self-reported confidence is never used."""
from __future__ import annotations

from ria.models.analysis import ConfidenceBreakdown, RequirementRecord, StatementRecord
from ria.models.enums import DEFAULT_SOURCE_AUTHORITY, EvidenceStatus, RequirementType

_ACTOR_OPTIONAL = {RequirementType.NON_FUNCTIONAL, RequirementType.DATA, RequirementType.COMPLIANCE,
                   RequirementType.SECURITY, RequirementType.ACCESSIBILITY, RequirementType.BUSINESS}


def requirement_confidence(req: RequirementRecord, statements: list[StatementRecord]) -> ConfidenceBreakdown:
    b = ConfidenceBreakdown()
    verified = [s for s in statements if s.quote_verified]
    if verified:
        b.evidence_verified = 30
    # completeness: actor, trigger/precondition, expected behaviour
    checks = [bool(req.expected_behavior or req.description),
              bool(req.trigger or req.preconditions or req.requirement_type in _ACTOR_OPTIONAL),
              bool(req.actors) or req.requirement_type in _ACTOR_OPTIONAL]
    b.completeness = round(20 * sum(checks) / len(checks))
    segs = {sid for s in verified for sid in s.segment_ids}
    docs = {s.document_id for s in verified}
    b.multi_source = 15 if len(docs) >= 2 else 8 if len(segs) >= 2 else 0
    b.no_conflict = 0 if req.evidence_status == EvidenceStatus.CONFLICTING else 15
    if verified:
        b.source_authority = max(DEFAULT_SOURCE_AUTHORITY.get(s.document_type, 4) for s in verified)
    flags = bool(req.vague_terms) or any(s.possible_transcription_issue for s in statements)
    b.clarity = 0 if flags else 10
    # Weak evidence statuses can never reach "High" (>= 75).
    caps = {EvidenceStatus.UNSUPPORTED: 25, EvidenceStatus.CONFLICTING: 45, EvidenceStatus.AMBIGUOUS: 60,
            EvidenceStatus.PROPOSED: 60, EvidenceStatus.INFERRED: 55}
    if req.evidence_status == EvidenceStatus.UNSUPPORTED:
        b.evidence_verified = 0
    if req.evidence_status in caps:
        b.cap = caps[req.evidence_status]
        b.cap_reason = f"Capped at {b.cap} because evidence status is {req.evidence_status.value}"
    return b
