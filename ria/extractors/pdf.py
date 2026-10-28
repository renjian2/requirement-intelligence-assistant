"""Text-based PDF extraction with header/footer removal.

Image-only (scanned) and encrypted PDFs are detected and reported as BLOCKING:
OCR/decryption is out of MVP scope and we never pretend extraction succeeded.
"""
from __future__ import annotations

import io
import re
from collections import Counter

from pypdf import PdfReader

from ria.extractors.base import BULLET, Extractor, corrupt, looks_like_heading
from ria.models.enums import SegmentKind, WarningCode, WarningSeverity
from ria.models.segments import ExtractedBlock, ExtractionWarning, ExtractorOutput

MIN_CHARS_PER_PAGE = 25
_PAGE_NO = re.compile(r"^\s*(?:page\s*)?\d+\s*(?:(?:of|/)\s*\d+)?\s*$", re.I)


class PdfExtractor(Extractor):
    extensions = (".pdf",)

    def extract(self, data: bytes, file_name: str) -> ExtractorOutput:
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                return ExtractorOutput(warnings=[ExtractionWarning(
                    code=WarningCode.ENCRYPTED_PDF, severity=WarningSeverity.BLOCKING,
                    message="The PDF is password-protected.",
                    action="Remove the password (with permission) and upload again.")])
            pages = [(p.extract_text() or "") for p in reader.pages]
        except Exception as exc:  # noqa: BLE001
            return corrupt(f"The PDF could not be read ({type(exc).__name__}).")

        warnings: list[ExtractionWarning] = []
        page_lines = [[ln.rstrip() for ln in t.split("\n")] for t in pages]
        empty_pages = [i + 1 for i, t in enumerate(pages) if len(t.strip()) < MIN_CHARS_PER_PAGE]
        if pages and len(empty_pages) == len(pages):
            return ExtractorOutput(page_count=len(pages), warnings=[ExtractionWarning(
                code=WarningCode.IMAGE_ONLY_PDF, severity=WarningSeverity.BLOCKING,
                message="No readable text found. The PDF appears to be scanned images.",
                action="Run OCR (e.g. 'Save as searchable PDF') or upload the original DOCX.")])
        if empty_pages:
            warnings.append(ExtractionWarning(
                code=WarningCode.PARTIAL_IMAGE_PDF, severity=WarningSeverity.WARNING,
                message=f"Pages without readable text (possibly images): {_fmt_pages(empty_pages)}.",
                action="Content on these pages will NOT be analysed. Provide a text version if it matters."))

        removed = _strip_repeated_lines(page_lines)
        if removed:
            warnings.append(ExtractionWarning(
                code=WarningCode.HEADERS_FOOTERS_REMOVED, severity=WarningSeverity.INFO,
                message=f"Removed {removed} repeated header/footer/page-number lines."))

        blocks: list[ExtractedBlock] = []
        heading: str | None = None
        for page_no, lines in enumerate(page_lines, start=1):
            heading = _page_blocks(lines, page_no, heading, blocks)
        return ExtractorOutput(blocks=blocks, warnings=warnings, page_count=len(pages))


def _fmt_pages(pages: list[int]) -> str:
    return ", ".join(map(str, pages[:15])) + (" …" if len(pages) > 15 else "")


def _norm(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip().lower())


def _strip_repeated_lines(page_lines: list[list[str]]) -> int:
    """Remove page numbers and lines repeated at the top/bottom of most pages."""
    removed = 0
    n = len(page_lines)
    repeated: set[str] = set()
    if n >= 3:
        counter: Counter[str] = Counter()
        for lines in page_lines:
            content = [ln for ln in lines if ln.strip()]
            edge = {_norm(ln) for ln in content[:2] + content[-2:]}
            counter.update(edge)
        repeated = {k for k, c in counter.items() if c >= max(3, int(n * 0.6)) and k}
    for lines in page_lines:
        content_idx = [i for i, ln in enumerate(lines) if ln.strip()]
        edge_idx = set(content_idx[:2] + content_idx[-2:])
        keep: list[str] = []
        for i, ln in enumerate(lines):
            if i in edge_idx and (_PAGE_NO.match(ln) or _norm(ln) in repeated):
                removed += 1
                continue
            keep.append(ln)
        lines[:] = keep
    return removed


def _page_blocks(lines: list[str], page_no: int, heading: str | None,
                 out: list[ExtractedBlock]) -> str | None:
    buffer: list[str] = []
    kind = SegmentKind.PARAGRAPH

    def flush() -> None:
        nonlocal kind
        if buffer:
            out.append(ExtractedBlock(text=" ".join(s.strip() for s in buffer),
                                      kind=kind, heading=heading, page_number=page_no))
            buffer.clear()
        kind = SegmentKind.PARAGRAPH

    for ln in lines:
        s = ln.strip()
        if not s:
            flush(); continue
        if looks_like_heading(s):
            flush(); heading = s; continue
        if BULLET.match(s):
            flush(); kind = SegmentKind.LIST_ITEM
            buffer.append(BULLET.sub("", s)); continue
        if buffer and buffer[-1].rstrip().endswith((".", "?", "!", ":")) and s[:1].isupper():
            flush()
        buffer.append(s)
    flush()
    return heading
