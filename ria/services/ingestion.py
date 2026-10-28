"""Document ingestion: validate -> extract -> segment -> quality-check -> persist.

Fully deterministic. No LLM is called in this step.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from ria.config import PROJECT_ROOT, SUPPORTED_EXTENSIONS, Settings
from ria.extractors import get_extractor
from ria.models.enums import DocumentType, ExtractionStatus, WarningCode, WarningSeverity
from ria.models.segments import (
    Chunk, DocumentStats, ExtractionWarning, Segment, SourceDocument, estimate_tokens,
)
from ria.persistence.repositories import Repository
from ria.pipeline.chunker import build_chunks
from ria.pipeline.quality import run_quality_checks
from ria.pipeline.segmenter import build_segments
from ria.logging_setup import ctx, get_logger

log = get_logger(__name__)

# Warnings produced by extractors/segmenter (kept when the document type changes).
_EXTRACTION_CODES = {
    WarningCode.CORRUPT_FILE, WarningCode.ENCRYPTED_PDF, WarningCode.IMAGE_ONLY_PDF,
    WarningCode.PARTIAL_IMAGE_PDF, WarningCode.ROWS_TRUNCATED,
    WarningCode.HEADERS_FOOTERS_REMOVED, WarningCode.LONG_BLOCKS_SPLIT,
}

SAMPLE_DIR = PROJECT_ROOT / "sample_data"
SAMPLE_FILES: dict[str, DocumentType] = {
    "expense_portal_meeting.vtt": DocumentType.MEETING_TRANSCRIPT,
    "expense_portal_brd.docx": DocumentType.BRD,
    "travel_expense_policy.pdf": DocumentType.BUSINESS_POLICY,
    "existing_user_stories.csv": DocumentType.EXISTING_USER_STORIES,
}


class IngestionError(ValueError):
    """Raised for files that must be rejected before extraction (user-facing message)."""


@dataclass
class IngestResult:
    document: SourceDocument | None
    segments: list[Segment] = field(default_factory=list)
    duplicate_of: SourceDocument | None = None


def sanitise_file_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name  # strip any path
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .")
    return name[:150] or "unnamed"


def suggest_document_type(file_name: str) -> DocumentType:
    """Best-effort default for the type dropdown. The user always confirms."""
    n = file_name.lower()
    if n.endswith((".vtt", ".srt")) or "transcript" in n or "recording" in n:
        return DocumentType.MEETING_TRANSCRIPT
    if "brd" in n or "business requirement" in n:
        return DocumentType.BRD
    if "clarification" in n or "answers" in n or "q&a" in n or "qna" in n:
        return DocumentType.CLARIFICATIONS
    if "minutes" in n or "notes" in n or "mom" in n.split("_"):
        return DocumentType.MEETING_NOTES
    if "stor" in n or "backlog" in n:
        return DocumentType.EXISTING_USER_STORIES
    if "policy" in n:
        return DocumentType.BUSINESS_POLICY
    if "spec" in n or "design" in n:
        return DocumentType.TECHNICAL_SPEC
    if n.endswith((".csv", ".xlsx")):
        return DocumentType.EXISTING_USER_STORIES
    return DocumentType.REQUIREMENT_DOC


class IngestionService:
    def __init__(self, repo: Repository, settings: Settings):
        self.repo = repo
        self.settings = settings

    def ingest(self, session_id: str, file_name: str, data: bytes,
               document_type: DocumentType) -> IngestResult:
        file_name = sanitise_file_name(file_name)
        ext = Path(file_name).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise IngestionError(f"'{file_name}': unsupported file type '{ext or 'none'}'. "
                                 f"Supported: {', '.join(SUPPORTED_EXTENSIONS)}")
        if not data:
            raise IngestionError(f"'{file_name}' is empty.")
        if len(data) > self.settings.max_upload_bytes:
            raise IngestionError(f"'{file_name}' is larger than {self.settings.max_upload_mb} MB.")

        sha = hashlib.sha256(data).hexdigest()
        existing = self.repo.find_by_hash(session_id, sha)
        if existing:
            self.repo.log(session_id, "INFO", "ingestion",
                          f"Skipped '{file_name}': identical to {existing.document_id} ({existing.file_name})")
            return IngestResult(document=None, duplicate_of=existing)

        extractor = get_extractor(ext)
        assert extractor is not None  # guaranteed by SUPPORTED_EXTENSIONS
        output = extractor.extract(data, file_name)

        document_id = self.repo.next_document_id(session_id)
        segments, split_count = build_segments(document_id, output.blocks, self.settings.max_segment_chars)

        warnings = list(output.warnings)
        if split_count:
            warnings.append(ExtractionWarning(
                code=WarningCode.LONG_BLOCKS_SPLIT, severity=WarningSeverity.INFO,
                message=f"{split_count} long paragraph(s)/turn(s) were split at sentence boundaries."))
        blocking_from_extractor = any(w.severity == WarningSeverity.BLOCKING for w in warnings)
        if not blocking_from_extractor:
            warnings += run_quality_checks(segments, document_type, output.detected_transcript)

        doc = SourceDocument(
            document_id=document_id, session_id=session_id, file_name=file_name, extension=ext,
            document_type=document_type, sha256=sha, size_bytes=len(data),
            detected_transcript=output.detected_transcript,
            stats=_stats(segments, output.page_count), warnings=warnings,
        )
        if doc.has_blocking_warning:
            doc.status = ExtractionStatus.FAILED
        self.repo.save_document(doc, segments)
        level = "ERROR" if doc.status == ExtractionStatus.FAILED else "INFO"
        self.repo.log(session_id, level, "ingestion",
                      f"{document_id} '{file_name}' as {document_type.value}: {len(segments)} segments, "
                      f"{len(warnings)} warning(s), status {doc.status.value}")
        (log.warning if doc.status == ExtractionStatus.FAILED else log.info)(
            "Document %s ingested: type=%s ext=%s bytes=%d segments=%d warnings=%d status=%s", document_id,
            document_type.value, ext, len(data), len(segments), len(warnings), doc.status.value, extra=ctx(session_id))
        return IngestResult(document=doc, segments=segments)

    def change_document_type(self, session_id: str, document_id: str, new_type: DocumentType) -> SourceDocument:
        doc = self.repo.get_document(session_id, document_id)
        if doc is None:
            raise IngestionError(f"{document_id} not found")
        segments = self.repo.get_segments(session_id, document_id)
        kept = [w for w in doc.warnings if w.code in _EXTRACTION_CODES]
        if doc.status != ExtractionStatus.FAILED:
            kept += run_quality_checks(segments, new_type, doc.detected_transcript)
        self.repo.update_document(session_id, document_id, document_type=new_type, warnings=kept)
        self.repo.log(session_id, "INFO", "review",
                      f"{document_id} type changed {doc.document_type.value} -> {new_type.value}")
        log.info("Document %s type changed to %s", document_id, new_type.value, extra=ctx(session_id))
        return self.repo.get_document(session_id, document_id)  # type: ignore[return-value]

    def confirm_extraction(self, session_id: str, document_id: str, confirmed: bool = True) -> None:
        doc = self.repo.get_document(session_id, document_id)
        if doc is None or doc.status == ExtractionStatus.FAILED:
            raise IngestionError("A failed extraction cannot be confirmed. Fix and re-upload the file.")
        status = ExtractionStatus.CONFIRMED if confirmed else ExtractionStatus.EXTRACTED
        self.repo.update_document(session_id, document_id, status=status)
        self.repo.log(session_id, "INFO", "review", f"{document_id} extraction marked {status.value}")

    def confirm_all(self, session_id: str) -> list[str]:
        """Confirm every successfully extracted document still waiting for human review."""
        pending = [d for d in self.repo.list_documents(session_id) if d.status == ExtractionStatus.EXTRACTED]
        confirmed = []
        for document in pending:
            self.confirm_extraction(session_id, document.document_id)
            confirmed.append(document.document_id)
        if confirmed:
            log.info("Confirm all: %d document(s)", len(confirmed), extra=ctx(session_id))
            self.repo.log(session_id, "INFO", "review",
                          f"Confirm all: {len(confirmed)} document(s) confirmed ({', '.join(confirmed)})")
        return confirmed

    def chunks_for(self, session_id: str, document_id: str) -> list[Chunk]:
        doc = self.repo.get_document(session_id, document_id)
        if doc is None:
            return []
        return build_chunks(document_id, self.repo.get_segments(session_id, document_id),
                            self.settings.chunk_token_budget, doc.detected_transcript,
                            self.settings.transcript_overlap_segments)

    def load_samples(self, session_id: str) -> list[IngestResult]:
        results = []
        for name, dtype in SAMPLE_FILES.items():
            path = SAMPLE_DIR / name
            if path.exists():
                results.append(self.ingest(session_id, name, path.read_bytes(), dtype))
        return results


def _stats(segments: list[Segment], page_count: int | None) -> DocumentStats:
    text = " ".join(s.text for s in segments)
    speakers = sorted({s.speaker for s in segments if s.speaker})
    return DocumentStats(characters=len(text), words=len(text.split()), segments=len(segments),
                         estimated_tokens=estimate_tokens(text), speakers=speakers, page_count=page_count)


def analysis_readiness(repo: Repository, session_id: str) -> tuple[bool, list[str]]:
    """Gate for Phase 2: analysis runs only on human-confirmed extractions."""
    docs = repo.list_documents(session_id)
    issues: list[str] = []
    if not docs:
        issues.append("Upload at least one document.")
    confirmed = [d for d in docs if d.status == ExtractionStatus.CONFIRMED]
    pending = [d for d in docs if d.status == ExtractionStatus.EXTRACTED]
    if docs and not confirmed:
        issues.append("Confirm the extraction of at least one document in Extraction Review.")
    if pending:
        issues.append(f"{len(pending)} document(s) still waiting for extraction review "
                      f"({', '.join(d.document_id for d in pending)}). They will be excluded.")
    return (bool(confirmed), issues)
