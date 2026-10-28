"""DOCX extraction: headings, paragraphs, lists and tables in document order.

Also detects Microsoft Teams transcript DOCX exports, e.g.::

    Priya Shah   0:05
    Let's review the reimbursement flow.
"""
from __future__ import annotations

import io
import re

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from ria.extractors.base import Extractor, corrupt, lines_to_turns
from ria.models.enums import SegmentKind
from ria.models.segments import ExtractedBlock, ExtractorOutput

_TEAMS_HEADER = re.compile(
    r"^(?P<speaker>[^\d\s][^\n]{0,60}?)\s+(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\s*$"
)
_TEAMS_RANGE = re.compile(r"^\d{1,2}:\d{1,2}:\d{1,2}(?:\.\d+)?\s*-->\s*\d{1,2}:\d{1,2}:\d{1,2}(?:\.\d+)?$")


class DocxExtractor(Extractor):
    extensions = (".docx",)

    def extract(self, data: bytes, file_name: str) -> ExtractorOutput:
        try:
            doc = Document(io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001 - python-docx raises many types
            return corrupt(f"The DOCX file could not be opened ({type(exc).__name__}).")

        lines: list[str] = []
        for p in doc.paragraphs:
            lines.extend(p.text.split("\n"))
        turns = _teams_turns(lines) or lines_to_turns(lines)
        if turns:
            return ExtractorOutput(blocks=turns, detected_transcript=True)

        blocks: list[ExtractedBlock] = []
        heading: str | None = None
        for item in doc.iter_inner_content():
            if isinstance(item, Paragraph):
                text = " ".join(item.text.split())
                if not text:
                    continue
                style = (item.style.name or "").lower() if item.style is not None else ""
                if style.startswith(("heading", "title")):
                    heading = text
                    continue
                kind = SegmentKind.LIST_ITEM if ("list" in style or _has_numbering(item)) else SegmentKind.PARAGRAPH
                blocks.append(ExtractedBlock(text=text, kind=kind, heading=heading))
            elif isinstance(item, Table):
                blocks.extend(_table_blocks(item, heading))
        return ExtractorOutput(blocks=blocks)


def _has_numbering(p: Paragraph) -> bool:
    ppr = p._p.pPr  # noqa: SLF001 - python-docx has no public API for numbering
    return ppr is not None and ppr.numPr is not None


def _row_cells(row) -> list[str]:
    cells: list[str] = []
    prev_tc = None
    for cell in row.cells:
        if cell._tc is prev_tc:  # noqa: SLF001 - horizontally merged cell repeats
            continue
        prev_tc = cell._tc  # noqa: SLF001
        cells.append(" ".join(cell.text.split()))
    return cells


def _table_blocks(table: Table, heading: str | None) -> list[ExtractedBlock]:
    rows = [_row_cells(r) for r in table.rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    if len(rows) == 1:
        return [ExtractedBlock(text=" | ".join(c for c in rows[0] if c),
                               kind=SegmentKind.TABLE_ROW, heading=heading)]
    header = rows[0]
    out: list[ExtractedBlock] = []
    for r in rows[1:]:
        pairs = [f"{h or f'Column {i + 1}'}: {c}" for i, (h, c) in enumerate(zip(header, r)) if c]
        if pairs:
            out.append(ExtractedBlock(text=" | ".join(pairs), kind=SegmentKind.TABLE_ROW, heading=heading))
    return out


def _teams_turns(lines: list[str]) -> list[ExtractedBlock] | None:
    """Parse Teams 'Speaker   0:05' / '0:0:0.0 --> 0:0:5.0' style transcripts."""
    clean = [ln.strip() for ln in lines if ln.strip()]
    headers = sum(1 for ln in clean if _TEAMS_HEADER.match(ln))
    ranges = sum(1 for ln in clean if _TEAMS_RANGE.match(ln))
    if max(headers, ranges) < 3:
        return None

    turns: list[ExtractedBlock] = []
    i = 0
    while i < len(clean):
        ln = clean[i]
        if ranges >= 3 and _TEAMS_RANGE.match(ln):
            ts = ln.split("-->")[0].strip().split(".")[0]
            speaker = clean[i + 1] if i + 1 < len(clean) else None
            i += 2
            body: list[str] = []
            while i < len(clean) and not _TEAMS_RANGE.match(clean[i]):
                body.append(clean[i]); i += 1
            if body:
                turns.append(ExtractedBlock(text=" ".join(body), kind=SegmentKind.SPEAKER_TURN,
                                            speaker=speaker, timestamp_start=ts))
            continue
        m = _TEAMS_HEADER.match(ln)
        if headers >= 3 and m:
            i += 1
            body = []
            while i < len(clean) and not _TEAMS_HEADER.match(clean[i]):
                body.append(clean[i]); i += 1
            if body:
                turns.append(ExtractedBlock(text=" ".join(body), kind=SegmentKind.SPEAKER_TURN,
                                            speaker=m.group("speaker").strip(),
                                            timestamp_start=m.group("ts")))
            continue
        i += 1
    return turns if len(turns) >= 3 else None
