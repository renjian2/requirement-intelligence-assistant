"""Plain text and Markdown extraction (with transcript auto-detection)."""
from __future__ import annotations

import re

from ria.extractors.base import (
    BULLET, Extractor, decode_text, lines_to_turns, looks_like_heading,
)
from ria.models.enums import SegmentKind
from ria.models.segments import ExtractedBlock, ExtractorOutput

_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


class TextExtractor(Extractor):
    extensions = (".txt", ".md")

    def extract(self, data: bytes, file_name: str) -> ExtractorOutput:
        text = decode_text(data).replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")

        turns = lines_to_turns(lines)
        if turns:
            return ExtractorOutput(blocks=turns, detected_transcript=True)

        is_md = file_name.lower().endswith(".md")
        return ExtractorOutput(blocks=_document_blocks(lines, markdown=is_md))


def _document_blocks(lines: list[str], markdown: bool) -> list[ExtractedBlock]:
    blocks: list[ExtractedBlock] = []
    heading: str | None = None
    buffer: list[str] = []
    table_header: list[str] | None = None

    def flush() -> None:
        if buffer:
            joined = " ".join(s.strip() for s in buffer).strip()
            if joined:
                blocks.append(ExtractedBlock(text=joined, heading=heading))
            buffer.clear()

    for raw in lines:
        line = raw.rstrip()
        md = _MD_HEADING.match(line) if markdown else None
        if md:
            flush()
            heading = md.group(2).strip()
            table_header = None
            continue
        if not line.strip():
            flush()
            table_header = None
            continue
        if markdown and line.strip().startswith("|"):
            flush()
            if _MD_TABLE_SEP.match(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if table_header is None:
                table_header = cells
            else:
                pairs = [f"{h}: {c}" for h, c in zip(table_header, cells) if c]
                blocks.append(ExtractedBlock(text=" | ".join(pairs), kind=SegmentKind.TABLE_ROW,
                                             heading=heading))
            continue
        if BULLET.match(line):
            flush()
            blocks.append(ExtractedBlock(text=BULLET.sub("", line).strip(),
                                         kind=SegmentKind.LIST_ITEM, heading=heading))
            continue
        if not markdown and looks_like_heading(line) and not buffer:
            heading = line.strip()
            continue
        buffer.append(line)
    flush()
    return blocks
