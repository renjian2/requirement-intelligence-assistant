"""Group consecutive segments into token-budgeted chunks (one document at a time).

Rules (Build Prompt v2):
* never split a segment
* prefer breaking at heading changes
* small overlap only for transcripts - overlap segments are *context only* and
  cannot be cited from that chunk (avoids duplicate statements)
"""
from __future__ import annotations

from ria.models.segments import Chunk, Segment, estimate_tokens

PER_SEGMENT_OVERHEAD = 12  # JSON keys / ids added when segments are serialised


def segment_tokens(seg: Segment) -> int:
    return estimate_tokens(seg.text) + PER_SEGMENT_OVERHEAD


def build_chunks(document_id: str, segments: list[Segment], token_budget: int,
                 is_transcript: bool, overlap_segments: int = 2) -> list[Chunk]:
    if not segments:
        return []
    groups: list[list[Segment]] = []
    current: list[Segment] = []
    used = 0
    for seg in segments:
        t = segment_tokens(seg)
        heading_change = bool(current) and seg.heading != current[-1].heading
        over_budget = current and used + t > token_budget
        soft_break = heading_change and used >= token_budget * 0.4
        if over_budget or soft_break:
            groups.append(current)
            current, used = [], 0
        current.append(seg)
        used += t
    if current:
        groups.append(current)

    chunks: list[Chunk] = []
    for i, group in enumerate(groups, start=1):
        context: list[Segment] = []
        if is_transcript and overlap_segments > 0 and i > 1:
            context = groups[i - 2][-overlap_segments:]
        tokens = sum(segment_tokens(s) for s in group + context)
        headings = list(dict.fromkeys(s.heading for s in group if s.heading))
        chunks.append(Chunk(
            chunk_id=f"{document_id}-C{i:02d}",
            document_id=document_id,
            segment_ids=[s.segment_id for s in group],
            context_segment_ids=[s.segment_id for s in context],
            estimated_tokens=tokens,
            headings=headings,
        ))
    return chunks
