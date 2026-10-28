"""Turn extractor blocks into citable segments with application-assigned IDs."""
from __future__ import annotations

import re

from ria.models.enums import SegmentKind
from ria.models.segments import ExtractedBlock, Segment

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def normalise(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    return " ".join(text.split())


def split_long_text(text: str, max_chars: int) -> list[str]:
    """Split text at sentence boundaries (then word boundaries) into parts <= max_chars."""
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        while len(sentence) > max_chars:  # very long sentence: hard split on a space
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            if current:
                parts.append(current); current = ""
            parts.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > max_chars:
            parts.append(current); current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return [p for p in parts if p]


def build_segments(document_id: str, blocks: list[ExtractedBlock],
                   max_chars: int) -> tuple[list[Segment], int]:
    """Return (segments, number_of_blocks_split). IDs: DOC-001-S0001, DOC-001-S0002 ..."""
    segments: list[Segment] = []
    split_count = 0
    for block in blocks:
        text = normalise(block.text)
        if not text:
            continue
        pieces = split_long_text(text, max_chars)
        if len(pieces) > 1:
            split_count += 1
        for piece in pieces:
            ordinal = len(segments) + 1
            segments.append(Segment(
                segment_id=f"{document_id}-S{ordinal:04d}",
                document_id=document_id,
                ordinal=ordinal,
                kind=block.kind if block.kind else SegmentKind.PARAGRAPH,
                text=piece,
                heading=normalise(block.heading) if block.heading else None,
                page_number=block.page_number,
                speaker=normalise(block.speaker) if block.speaker else None,
                timestamp_start=block.timestamp_start,
                timestamp_end=block.timestamp_end,
                is_split_part=len(pieces) > 1,
            ))
    return segments, split_count
