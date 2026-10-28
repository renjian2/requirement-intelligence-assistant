from __future__ import annotations

from ria.models.enums import DocumentType, SegmentKind, WarningCode
from ria.models.segments import ExtractedBlock
from ria.pipeline.chunker import build_chunks
from ria.pipeline.quality import run_quality_checks
from ria.pipeline.segmenter import build_segments, split_long_text


def test_segment_ids_are_sequential_and_whitespace_normalised():
    blocks = [ExtractedBlock(text="  Hello\u00a0  world "), ExtractedBlock(text="   "), ExtractedBlock(text="Second")]
    segs, _ = build_segments("DOC-007", blocks, 1000)
    assert [s.segment_id for s in segs] == ["DOC-007-S0001", "DOC-007-S0002"]
    assert segs[0].text == "Hello world"


def test_long_text_split_at_sentence_boundaries():
    text = " ".join(f"Sentence number {i} is here." for i in range(40))
    parts = split_long_text(text, 200)
    assert all(len(p) <= 200 for p in parts)
    assert all(p.endswith(".") for p in parts)
    assert " ".join(parts) == text


def test_single_huge_sentence_hard_split():
    parts = split_long_text("word " * 300, 100)
    assert all(len(p) <= 100 for p in parts)


def test_split_parts_flagged():
    segs, split = build_segments("DOC-001", [ExtractedBlock(text="A sentence here. " * 50)], 120)
    assert split == 1 and len(segs) > 1 and all(s.is_split_part for s in segs)


def _segs(n, kind=SegmentKind.PARAGRAPH, heading=None, size=200):
    blocks = [ExtractedBlock(text=f"Segment {i} " + "x" * size, kind=kind, heading=heading) for i in range(n)]
    return build_segments("DOC-001", blocks, 2000)[0]


def test_chunks_respect_budget_and_never_split_segments():
    segs = _segs(20)
    chunks = build_chunks("DOC-001", segs, token_budget=300, is_transcript=False)
    flat = [sid for c in chunks for sid in c.segment_ids]
    assert flat == [s.segment_id for s in segs]
    assert all(c.estimated_tokens <= 300 for c in chunks)
    assert all(not c.context_segment_ids for c in chunks)


def test_transcript_chunks_have_read_only_overlap():
    segs = _segs(20, SegmentKind.SPEAKER_TURN)
    chunks = build_chunks("DOC-001", segs, 300, is_transcript=True, overlap_segments=2)
    assert len(chunks) > 1
    assert chunks[1].context_segment_ids == chunks[0].segment_ids[-2:]
    assert not set(chunks[1].context_segment_ids) & set(chunks[1].segment_ids)


def test_chunks_prefer_heading_boundaries():
    segs = _segs(3, heading="A") + build_segments("DOC-001", [ExtractedBlock(text="y " * 100, heading="B")], 2000)[0]
    chunks = build_chunks("DOC-001", segs, token_budget=400, is_transcript=False)
    assert chunks[-1].headings == ["B"]


def test_quality_flags():
    blocks = [ExtractedBlock(text="Haan theek hai, approval zaroori hai. Mail a@b.com or call +91 98765 43210",
                             kind=SegmentKind.SPEAKER_TURN)]
    segs, _ = build_segments("DOC-001", blocks, 1000)
    codes = {w.code for w in run_quality_checks(segs, DocumentType.MEETING_TRANSCRIPT, True)}
    assert {WarningCode.MIXED_LANGUAGE, WarningCode.PII_DETECTED, WarningCode.MISSING_SPEAKERS,
            WarningCode.VERY_SHORT_TEXT} <= codes


def test_type_mismatch_when_transcript_not_detected():
    segs, _ = build_segments("DOC-001", [ExtractedBlock(text="Plain text " * 20)], 1000)
    codes = {w.code for w in run_quality_checks(segs, DocumentType.MEETING_TRANSCRIPT, False)}
    assert WarningCode.TYPE_MISMATCH in codes
