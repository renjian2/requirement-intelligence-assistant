"""LLM output schemas (what the model returns) and stored records (what the app keeps).

Rules for the LLM schemas (keep them inside xAI / OpenAI / Ollama structured-output support):
* no recursive models, small flat objects
* the LLM never produces final IDs: it cites existing segment/statement IDs and uses
  temporary ``local_key`` values that the application maps to final IDs.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ria.models.enums import (
    CriterionType, DetectedBy, DocumentType, EvidenceStatus, FindingType, InputMode, Priority, PrioritySource,
    RequirementType, ReviewStatus, RunStatus, ScenarioType, Severity, StatementType,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ============================== LLM output schemas ==============================

class GlossaryTerm(BaseModel):
    term: str
    meaning: str | None = Field(None, description="Only if defined in the source; otherwise null")
    defined_in_source: bool


class DocumentUnderstanding(BaseModel):
    domain: str | None = None
    problem_statement: str | None = None
    objectives: list[str] = Field(default_factory=list)
    stakeholders: list[str] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)
    systems: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)
    data_entities: list[str] = Field(default_factory=list)
    glossary: list[GlossaryTerm] = Field(default_factory=list)
    undefined_acronyms: list[str] = Field(default_factory=list)


class ExtractedStatement(BaseModel):
    local_key: str = Field(description="Temporary key unique within this response, e.g. 's1'")
    statement_type: StatementType
    text: str = Field(description="One atomic idea, in plain English")
    evidence_status: EvidenceStatus
    segment_ids: list[str] = Field(min_length=1, description="Existing citable segment IDs only")
    evidence_quote: str = Field(description="Exact quote copied from a cited segment, max 30 words")
    speaker: str | None = None
    possible_transcription_issue: bool = False
    transcription_note: str | None = None
    supersedes_local_key: str | None = Field(
        None, description="For CORRECTION statements: local_key of the replaced statement in this response, "
                          "or the STM-id of an earlier statement listed in previous_statements")
    is_ai_suggestion: bool = False


class StatementExtractionResult(BaseModel):
    chunk_id: str
    statements: list[ExtractedStatement] = Field(default_factory=list)


class RequirementDraft(BaseModel):
    local_key: str = Field(description="Temporary key, e.g. 'r1'")
    title: str = Field(description="Short title, max 10 words")
    description: str = Field(description="'The system shall ...' or a clear business requirement")
    requirement_type: RequirementType
    actors: list[str] = Field(default_factory=list, description="Only actors named in the sources")
    trigger: str | None = Field(None, description="Event that starts the behaviour, if stated")
    preconditions: list[str] = Field(default_factory=list)
    expected_behavior: str | None = None
    exceptions: list[str] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    priority: Priority | None = None
    priority_stated: bool = Field(False, description="True only if a source states the priority")
    evidence_status: EvidenceStatus
    source_statement_ids: list[str] = Field(min_length=1, description="Existing statement IDs only")
    assumptions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    is_ai_suggestion: bool = False


class RequirementSynthesisResult(BaseModel):
    group: str
    requirements: list[RequirementDraft] = Field(default_factory=list)


class IssueDraft(BaseModel):
    local_key: str = Field(description="Temporary key, e.g. 'f1'")
    finding_type: FindingType
    severity: Severity
    title: str = Field(description="Short title, max 12 words")
    description: str
    requirement_ids: list[str] = Field(min_length=1, description="Existing REQ-ids only; for DUPLICATE the "
                                                                 "first id is the one to keep")
    business_impact: str | None = None
    dev_impact: str | None = None
    qa_impact: str | None = None
    clarification_question: str = Field(description="One concrete question a stakeholder can answer")
    ask_whom: str | None = Field(None, description="Role or person named in the sources, if known")
    suggested_resolution: str | None = None


class IssueReviewResult(BaseModel):
    group: str
    findings: list[IssueDraft] = Field(default_factory=list)


class CriterionDraft(BaseModel):
    criterion_type: CriterionType
    given: str
    when: str
    then: str


class ScenarioDraft(BaseModel):
    title: str
    scenario_type: ScenarioType
    priority: Priority
    preconditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    expected_result: str


class StoryDraft(BaseModel):
    local_key: str = Field(description="Temporary key, e.g. 'u1'")
    requirement_ids: list[str] = Field(min_length=1, description="Existing REQ-ids only")
    title: str
    actor: str | None = Field(None, description="Specific role from the requirements; null if unknown")
    capability: str = Field(description="What the actor wants, starting with a verb")
    benefit: str | None = Field(None, description="Business value if stated or clearly implied; else null")
    acceptance_criteria: list[CriterionDraft] = Field(default_factory=list)
    test_scenarios: list[ScenarioDraft] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)


class StoryGenerationResult(BaseModel):
    group: str
    stories: list[StoryDraft] = Field(default_factory=list)


# ================================ Stored records ================================

class StatementRecord(BaseModel):
    statement_id: str                       # STM-0001
    run_id: str
    document_id: str
    document_type: DocumentType
    chunk_id: str
    statement_type: StatementType
    text: str
    evidence_status: EvidenceStatus
    segment_ids: list[str]
    evidence_quote: str
    quote_verified: bool
    quote_score: float
    speaker: str | None = None              # taken from the segment by code, not from the LLM
    possible_transcription_issue: bool = False
    transcription_note: str | None = None
    supersedes_id: str | None = None
    superseded_by_id: str | None = None
    is_ai_suggestion: bool = False
    validation_notes: list[str] = Field(default_factory=list)


class ConfidenceBreakdown(BaseModel):
    evidence_verified: int = 0      # max 30
    completeness: int = 0           # max 20
    multi_source: int = 0           # max 15
    no_conflict: int = 0            # max 15
    source_authority: int = 0       # max 10
    clarity: int = 0                # max 10
    cap: int | None = None          # maximum allowed for weak evidence statuses
    cap_reason: str | None = None

    @property
    def raw_total(self) -> int:
        return (self.evidence_verified + self.completeness + self.multi_source + self.no_conflict
                + self.source_authority + self.clarity)

    @property
    def total(self) -> int:
        return min(self.raw_total, self.cap) if self.cap is not None else self.raw_total


class RequirementRecord(BaseModel):
    requirement_id: str                     # REQ-001
    run_id: str
    group: str
    title: str
    description: str
    requirement_type: RequirementType
    actors: list[str] = Field(default_factory=list)
    trigger: str | None = None
    preconditions: list[str] = Field(default_factory=list)
    expected_behavior: str | None = None
    exceptions: list[str] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    priority: Priority | None = None
    priority_source: PrioritySource = PrioritySource.NONE
    evidence_status: EvidenceStatus
    source_statement_ids: list[str]
    segment_ids: list[str]
    document_ids: list[str]
    assumptions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    is_ai_suggestion: bool = False
    evidence_verified: bool = False
    confidence: int = 0
    confidence_breakdown: ConfidenceBreakdown = Field(default_factory=ConfidenceBreakdown)
    vague_terms: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.PENDING   # set only by a human
    reviewer_note: str | None = None

    @property
    def confidence_band(self) -> str:
        return "High" if self.confidence >= 75 else "Medium" if self.confidence >= 50 else "Low"


class ConflictValue(BaseModel):
    requirement_id: str
    value: str
    document_ids: list[str] = Field(default_factory=list)


class FindingRecord(BaseModel):
    finding_id: str                         # FND-001
    run_id: str
    finding_type: FindingType
    severity: Severity
    title: str
    description: str
    requirement_ids: list[str] = Field(default_factory=list)
    statement_ids: list[str] = Field(default_factory=list)
    segment_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    values: list[ConflictValue] = Field(default_factory=list)
    business_impact: str | None = None
    dev_impact: str | None = None
    qa_impact: str | None = None
    clarification_question: str | None = None
    ask_whom: str | None = None
    suggested_resolution: str | None = None
    detected_by: DetectedBy
    rule: str | None = None
    is_ai_suggestion: bool = False
    validation_notes: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.PENDING
    reviewer_note: str | None = None
    resolution: str | None = None


class AcceptanceCriterion(BaseModel):
    ac_id: str                              # AC-001-01
    criterion_type: CriterionType
    given: str
    when: str
    then: str
    unverified_values: list[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return f"Given {self.given}, when {self.when}, then {self.then}"


class StoryRecord(BaseModel):
    story_id: str                           # US-001
    run_id: str
    group: str
    requirement_ids: list[str]
    title: str
    actor: str
    capability: str
    benefit: str
    priority: Priority | None = None
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    segment_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)
    is_ai_suggestion: bool = False
    review_status: ReviewStatus = ReviewStatus.PENDING
    reviewer_note: str | None = None

    @property
    def story_text(self) -> str:
        return f"As a {self.actor}, I want to {self.capability}, so that {self.benefit}."


class TestScenarioRecord(BaseModel):
    scenario_id: str                        # TS-001
    run_id: str
    story_id: str
    requirement_ids: list[str]
    title: str
    scenario_type: ScenarioType
    priority: Priority
    preconditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    expected_result: str
    unverified_values: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.PENDING
    reviewer_note: str | None = None


class RunSummary(BaseModel):
    documents: list[str] = Field(default_factory=list)
    document_types: dict[str, str] = Field(default_factory=dict)   # type of each document when the run started
    stories_audit_mark: int = 0      # last audit-log id when stories were generated (detects later review changes)
    calls: int = 0
    cached_calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_estimated: bool = False
    statements: int = 0
    statements_verified: int = 0
    requirements: int = 0
    findings: int = 0
    stories: int = 0
    scenarios: int = 0
    stages: list[str] = Field(default_factory=list)
    duration_s: float = 0.0
    errors: list[str] = Field(default_factory=list)


class AnalysisRun(BaseModel):
    run_id: str
    session_id: str
    provider: str
    model: str
    analysis_mode: str
    input_mode: InputMode
    redaction: bool
    status: RunStatus = RunStatus.RUNNING
    is_demo: bool = False
    max_tests_per_story: int = 3
    created_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    summary: RunSummary = Field(default_factory=RunSummary)
