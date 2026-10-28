"""WebVTT (incl. Microsoft Teams exports) and SRT transcript extraction.

Teams splits a single speaker turn into many short cues; consecutive cues from the
same speaker are merged into one turn (up to a length limit) so segments stay
meaningful and citable.
"""
from __future__ import annotations

import re

from ria.extractors.base import Extractor, decode_text, match_speaker_line
from ria.models.enums import SegmentKind
from ria.models.segments import ExtractedBlock, ExtractorOutput

_TIMING = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})"
)
_VOICE = re.compile(r"<v\s+([^>]+)>(.*?)(?:</v>|$)", re.S)
_TAGS = re.compile(r"</?[^>]+>")
MERGE_MAX_CHARS = 900


def _clean_ts(ts: str) -> str:
    """'00:01:02.345' -> '00:01:02'; '01:02.3' -> '00:01:02'."""
    core = re.split(r"[.,]", ts)[0]
    parts = core.split(":")
    if len(parts) == 2:
        parts = ["00", *parts]
    return ":".join(p.zfill(2) for p in parts)


class SubtitleExtractor(Extractor):
    extensions = (".vtt", ".srt")

    def extract(self, data: bytes, file_name: str) -> ExtractorOutput:
        text = decode_text(data).replace("\r\n", "\n").replace("\r", "\n")
        cues = _parse_cues(text)
        turns: list[ExtractedBlock] = []
        for start, end, speaker, body in cues:
            prev = turns[-1] if turns else None
            if (prev and prev.speaker == speaker
                    and len(prev.text) + len(body) < MERGE_MAX_CHARS):
                prev.text += " " + body
                prev.timestamp_end = end
            else:
                turns.append(ExtractedBlock(text=body, kind=SegmentKind.SPEAKER_TURN,
                                            speaker=speaker, timestamp_start=start,
                                            timestamp_end=end))
        return ExtractorOutput(blocks=turns, detected_transcript=True)


def _parse_cues(text: str) -> list[tuple[str, str, str | None, str]]:
    cues: list[tuple[str, str, str | None, str]] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        timing_idx = next((i for i, ln in enumerate(lines) if _TIMING.search(ln)), None)
        if timing_idx is None:
            continue  # WEBVTT header, NOTE, STYLE blocks
        m = _TIMING.search(lines[timing_idx])
        body_raw = "\n".join(lines[timing_idx + 1:])
        if not body_raw.strip():
            continue
        speaker: str | None = None
        voice = _VOICE.search(body_raw)
        if voice:
            speaker = voice.group(1).strip()
            body = voice.group(2)
        else:
            body = body_raw
        body = " ".join(_TAGS.sub("", body).split())
        if speaker is None:
            sp = match_speaker_line(body)
            if sp:
                _, speaker, body = sp
        if body:
            cues.append((_clean_ts(m.group("start")), _clean_ts(m.group("end")), speaker, body))
    return cues
