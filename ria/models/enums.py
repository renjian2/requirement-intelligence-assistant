"""Enumerations shared across the application (aligned with Build Prompt v2)."""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class DocumentType(StrEnum):
    MEETING_TRANSCRIPT = "MEETING_TRANSCRIPT"
    MEETING_NOTES = "MEETING_NOTES"
    BRD = "BRD"
    REQUIREMENT_DOC = "REQUIREMENT_DOC"
    EXISTING_USER_STORIES = "EXISTING_USER_STORIES"
    BUSINESS_POLICY = "BUSINESS_POLICY"
    TECHNICAL_SPEC = "TECHNICAL_SPEC"
    CLARIFICATIONS = "CLARIFICATIONS"
    OTHER = "OTHER"


DOCUMENT_TYPE_LABELS: dict[DocumentType, str] = {
    DocumentType.MEETING_TRANSCRIPT: "Meeting transcript",
    DocumentType.MEETING_NOTES: "Meeting notes",
    DocumentType.BRD: "BRD",
    DocumentType.REQUIREMENT_DOC: "Requirement document",
    DocumentType.EXISTING_USER_STORIES: "Existing user stories",
    DocumentType.BUSINESS_POLICY: "Business policy",
    DocumentType.TECHNICAL_SPEC: "Technical specification",
    DocumentType.CLARIFICATIONS: "Clarification answers (Q&A log)",
    DocumentType.OTHER: "Other supporting document",
}

# Default source-authority order (higher = more authoritative). Used for
# confidence and conflict display only - never to auto-resolve conflicts.
DEFAULT_SOURCE_AUTHORITY: dict[DocumentType, int] = {
    DocumentType.BRD: 10,
    DocumentType.BUSINESS_POLICY: 10,
    DocumentType.CLARIFICATIONS: 10,
    DocumentType.REQUIREMENT_DOC: 9,
    DocumentType.TECHNICAL_SPEC: 8,
    DocumentType.EXISTING_USER_STORIES: 8,
    DocumentType.MEETING_NOTES: 7,
    DocumentType.MEETING_TRANSCRIPT: 5,
    DocumentType.OTHER: 4,
}


class SegmentKind(StrEnum):
    PARAGRAPH = "PARAGRAPH"
    LIST_ITEM = "LIST_ITEM"
    TABLE_ROW = "TABLE_ROW"
    SPEAKER_TURN = "SPEAKER_TURN"


class WarningSeverity(StrEnum):
    BLOCKING = "BLOCKING"   # document cannot be analysed
    WARNING = "WARNING"     # analysis possible, quality reduced
    INFO = "INFO"


class WarningCode(StrEnum):
    EMPTY_TEXT = "EMPTY_TEXT"
    VERY_SHORT_TEXT = "VERY_SHORT_TEXT"
    ENCRYPTED_PDF = "ENCRYPTED_PDF"
    IMAGE_ONLY_PDF = "IMAGE_ONLY_PDF"
    PARTIAL_IMAGE_PDF = "PARTIAL_IMAGE_PDF"
    CORRUPT_FILE = "CORRUPT_FILE"
    GARBLED_TEXT = "GARBLED_TEXT"
    MISSING_SPEAKERS = "MISSING_SPEAKERS"
    UNKNOWN_SPEAKER_TURNS = "UNKNOWN_SPEAKER_TURNS"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    MIXED_LANGUAGE = "MIXED_LANGUAGE"
    PII_DETECTED = "PII_DETECTED"
    ROWS_TRUNCATED = "ROWS_TRUNCATED"
    HEADERS_FOOTERS_REMOVED = "HEADERS_FOOTERS_REMOVED"
    LONG_BLOCKS_SPLIT = "LONG_BLOCKS_SPLIT"


class ExtractionStatus(StrEnum):
    EXTRACTED = "EXTRACTED"       # waiting for human check
    CONFIRMED = "CONFIRMED"       # human confirmed extraction is usable
    FAILED = "FAILED"             # blocking problem - cannot be analysed


# ---- Analysis enums (used from Phase 2 onwards; defined now so schemas are stable)

class EvidenceStatus(StrEnum):
    STATED = "STATED"
    PROPOSED = "PROPOSED"
    INFERRED = "INFERRED"
    AMBIGUOUS = "AMBIGUOUS"
    CONFLICTING = "CONFLICTING"
    UNSUPPORTED = "UNSUPPORTED"


class ReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EDITED = "EDITED"
    REJECTED = "REJECTED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    RESOLVED = "RESOLVED"          # findings only: answered / fixed



class StatementType(StrEnum):
    OBJECTIVE = "OBJECTIVE"
    DECISION = "DECISION"
    REQUIREMENT_CANDIDATE = "REQUIREMENT_CANDIDATE"
    BUSINESS_RULE = "BUSINESS_RULE"
    CONSTRAINT = "CONSTRAINT"
    ASSUMPTION = "ASSUMPTION"
    RISK = "RISK"
    DEPENDENCY = "DEPENDENCY"
    ACTION_ITEM = "ACTION_ITEM"
    OPEN_QUESTION = "OPEN_QUESTION"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    CORRECTION = "CORRECTION"
    CONTEXT = "CONTEXT"


class AnalysisMode(StrEnum):
    QUICK = "QUICK"
    DEEP = "DEEP"


class ProviderName(StrEnum):
    MOCK = "mock"
    GROK = "grok"
    OPENAI_COMPATIBLE = "openai_compatible"
    OLLAMA = "ollama"


class RequirementType(StrEnum):
    BUSINESS = "BUSINESS"
    FUNCTIONAL = "FUNCTIONAL"
    NON_FUNCTIONAL = "NON_FUNCTIONAL"
    DATA = "DATA"
    INTEGRATION = "INTEGRATION"
    REPORTING = "REPORTING"
    SECURITY = "SECURITY"
    COMPLIANCE = "COMPLIANCE"
    ACCESSIBILITY = "ACCESSIBILITY"


class Priority(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class PrioritySource(StrEnum):
    STATED = "STATED"              # priority written in the source document
    AI_SUGGESTED = "AI_SUGGESTED"
    NONE = "NONE"


class InputMode(StrEnum):
    TRANSCRIPT_ONLY = "TRANSCRIPT_ONLY"   # A: meeting -> draft requirements/BRD
    BRD_ONLY = "BRD_ONLY"                 # B: review an existing BRD
    COMPARE = "COMPARE"                   # C: meeting(s) + BRD -> compare


INPUT_MODE_LABELS: dict[InputMode, str] = {
    InputMode.TRANSCRIPT_ONLY: "A · Meeting only → draft requirements",
    InputMode.BRD_ONLY: "B · BRD only → review the BRD",
    InputMode.COMPARE: "C · Meeting + BRD → compare and find differences",
}


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class SuggestionStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


# Statement types that can become requirements in Stage 3.
REQUIREMENT_SOURCE_TYPES = {
    StatementType.REQUIREMENT_CANDIDATE, StatementType.BUSINESS_RULE, StatementType.CONSTRAINT,
    StatementType.DECISION, StatementType.CORRECTION,
}

MEETING_TYPES = {DocumentType.MEETING_TRANSCRIPT, DocumentType.MEETING_NOTES}
REQUIREMENT_DOC_TYPES = {DocumentType.BRD, DocumentType.REQUIREMENT_DOC}


ACCEPTED_REVIEW = {ReviewStatus.APPROVED, ReviewStatus.EDITED}
EXCLUDED_REVIEW = {ReviewStatus.REJECTED, ReviewStatus.OUT_OF_SCOPE}


class FindingType(StrEnum):
    CONTRADICTION = "CONTRADICTION"
    GAP = "GAP"
    AMBIGUITY = "AMBIGUITY"
    UNDEFINED_TERM = "UNDEFINED_TERM"
    DUPLICATE = "DUPLICATE"
    SCOPE_ISSUE = "SCOPE_ISSUE"
    UNSUPPORTED_ASSUMPTION = "UNSUPPORTED_ASSUMPTION"
    LOW_CONFIDENCE_EVIDENCE = "LOW_CONFIDENCE_EVIDENCE"
    POSSIBLE_TRANSCRIPTION_ERROR = "POSSIBLE_TRANSCRIPTION_ERROR"


FINDING_LABELS: dict[FindingType, str] = {
    FindingType.CONTRADICTION: "Contradiction", FindingType.GAP: "Gap", FindingType.AMBIGUITY: "Ambiguity",
    FindingType.UNDEFINED_TERM: "Undefined term", FindingType.DUPLICATE: "Duplicate",
    FindingType.SCOPE_ISSUE: "Scope issue", FindingType.UNSUPPORTED_ASSUMPTION: "Unsupported assumption",
    FindingType.LOW_CONFIDENCE_EVIDENCE: "Weak evidence",
    FindingType.POSSIBLE_TRANSCRIPTION_ERROR: "Possible transcription error",
}


class Severity(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


class DetectedBy(StrEnum):
    RULE = "RULE"
    LLM = "LLM"


class CriterionType(StrEnum):
    HAPPY_PATH = "HAPPY_PATH"
    BUSINESS_RULE = "BUSINESS_RULE"
    PERMISSION = "PERMISSION"
    ERROR = "ERROR"
    EXCEPTION = "EXCEPTION"
    STATE_CHANGE = "STATE_CHANGE"


class ScenarioType(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    PERMISSION = "PERMISSION"
    BOUNDARY = "BOUNDARY"
    INTEGRATION = "INTEGRATION"
    STATE_TRANSITION = "STATE_TRANSITION"


class ItemType(StrEnum):
    REQUIREMENT = "requirement"
    FINDING = "finding"
    STORY = "story"
    SCENARIO = "scenario"
