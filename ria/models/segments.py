"""Source documents, citable segments, chunks and extraction warnings."""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ria.models.enums import (
    DocumentType,
    ExtractionStatus,
    SegmentKind,
    WarningCode,
    WarningSeverity,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExtractionWarning(BaseModel):
    code: WarningCode
    severity: WarningSeverity
    message: str
    action: str = ""  # what the user should do next


class ExtractedBlock(BaseModel):
    """Raw block produced by an extractor, before segmentation and ID assignment."""

    text: str
    kind: SegmentKind = SegmentKind.PARAGRAPH
    heading: str | None = None
    page_number: int | None = None
    speaker: str | None = None
    timestamp_start: str | None = None
    timestamp_end: str | None = None


class ExtractorOutput(BaseModel):
    blocks: list[ExtractedBlock] = Field(default_factory=list)
    warnings: list[ExtractionWarning] = Field(default_factory=list)
    detected_transcript: bool = False
    page_count: int | None = None


class Segment(BaseModel):
    """Smallest citable unit. IDs are assigned by the application, never by the LLM."""

    segment_id: str            # e.g. DOC-001-S0001
    document_id: str           # e.g. DOC-001
    ordinal: int               # 1-based position inside the document
    kind: SegmentKind
    text: str
    heading: str | None = None
    page_number: int | None = None
    speaker: str | None = None
    timestamp_start: str | None = None
    timestamp_end: str | None = None
    is_split_part: bool = False

    @property
    def location(self) -> str:
        """Human-readable location used in evidence displays."""
        parts: list[str] = []
        if self.page_number is not None:
            parts.append(f"p.{self.page_number}")
        if self.timestamp_start:
            parts.append(self.timestamp_start)
        if self.speaker:
            parts.append(self.speaker)
        if self.heading:
            parts.append(f"§ {self.heading}")
        return " · ".join(parts)


class DocumentStats(BaseModel):
    characters: int = 0
    words: int = 0
    segments: int = 0
    estimated_tokens: int = 0
    speakers: list[str] = Field(default_factory=list)
    page_count: int | None = None


class SourceDocument(BaseModel):
    document_id: str
    session_id: str
    file_name: str
    extension: str
    document_type: DocumentType
    sha256: str
    size_bytes: int
    status: ExtractionStatus = ExtractionStatus.EXTRACTED
    detected_transcript: bool = False
    stats: DocumentStats = Field(default_factory=DocumentStats)
    warnings: list[ExtractionWarning] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def has_blocking_warning(self) -> bool:
        return any(w.severity == WarningSeverity.BLOCKING for w in self.warnings)


class Chunk(BaseModel):
    """A group of consecutive segments sent to the LLM in one call (Phase 2)."""

    chunk_id: str                       # e.g. DOC-001-C01
    document_id: str
    segment_ids: list[str]              # segments the LLM may cite
    context_segment_ids: list[str] = Field(default_factory=list)  # overlap, context only
    estimated_tokens: int
    headings: list[str] = Field(default_factory=list)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 characters per token)."""
    return max(1, (len(text) + 3) // 4) if text else 0
