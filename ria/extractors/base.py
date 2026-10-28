"""Extractor interface and shared helpers."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod

from ria.models.enums import SegmentKind, WarningCode, WarningSeverity
from ria.models.segments import ExtractedBlock, ExtractionWarning, ExtractorOutput


class Extractor(ABC):
    extensions: tuple[str, ...] = ()

    @abstractmethod
    def extract(self, data: bytes, file_name: str) -> ExtractorOutput:
        """Convert raw file bytes into ordered text blocks. Must not raise for bad files;
        return a BLOCKING warning instead."""


def decode_text(data: bytes) -> str:
    """Decode bytes trying UTF-8 (with/without BOM), UTF-16 and finally cp1252."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def corrupt(message: str) -> ExtractorOutput:
    return ExtractorOutput(warnings=[ExtractionWarning(
        code=WarningCode.CORRUPT_FILE, severity=WarningSeverity.BLOCKING,
        message=message, action="Re-export or re-save the file and upload it again.")])


# --- Transcript line detection (used by TXT and DOCX extractors) -----------------

_TS = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?"
# "[00:01:23] Priya Shah: text"  /  "00:01:23 Priya Shah: text"  /  "Priya Shah: text"
_SPEAKER_LINE = re.compile(
    rf"^\s*(?:\[?(?P<ts>{_TS})\]?\s*[-–]?\s*)?"
    r"(?P<speaker>[A-Z][\w.'’-]*(?:\s+[A-Z(][\w.'’()-]*){0,3})\s*:\s+(?P<text>\S.*)$"
)
# Words that look like "Speaker:" but are normal document labels.
_NOT_SPEAKERS = {
    "note", "notes", "example", "e.g", "i.e", "status", "priority", "date", "time",
    "owner", "description", "summary", "objective", "scope", "attendees", "agenda",
    "decision", "action", "actions", "question", "answer", "risk", "assumption",
    "as a", "given", "when", "then", "and", "so that", "title", "version", "author",
}


def match_speaker_line(line: str) -> tuple[str | None, str, str] | None:
    """Return (timestamp, speaker, text) if the line looks like a transcript turn."""
    m = _SPEAKER_LINE.match(line)
    if not m:
        return None
    speaker = m.group("speaker").strip()
    if speaker.lower().rstrip(".") in _NOT_SPEAKERS or len(speaker) > 40:
        return None
    return m.group("ts"), speaker, m.group("text").strip()


def lines_to_turns(lines: list[str]) -> list[ExtractedBlock] | None:
    """Parse lines as transcript turns if most non-empty lines are speaker lines.

    Continuation lines (no speaker) are appended to the previous turn.
    Returns None when the content does not look like a transcript.
    """
    non_empty = [ln for ln in lines if ln.strip()]
    if len(non_empty) < 3:
        return None
    matches = [match_speaker_line(ln) for ln in non_empty]
    hit_ratio = sum(1 for m in matches if m) / len(non_empty)
    distinct = {m[1] for m in matches if m}
    if hit_ratio < 0.5 or len(distinct) < 2:
        return None

    blocks: list[ExtractedBlock] = []
    for line, m in zip(non_empty, matches):
        if m:
            ts, speaker, text = m
            blocks.append(ExtractedBlock(text=text, kind=SegmentKind.SPEAKER_TURN,
                                         speaker=speaker, timestamp_start=ts))
        elif blocks:
            blocks[-1].text += " " + line.strip()
        else:
            blocks.append(ExtractedBlock(text=line.strip(), kind=SegmentKind.SPEAKER_TURN))
    return blocks


# --- Generic document helpers ------------------------------------------------------

BULLET = re.compile(r"^\s*(?:[-*•▪◦·]|\(?[a-zA-Z0-9]{1,3}[.)])\s+")
NUMBERED_HEADING = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+[A-Z][^.!?]{1,78}$")


def looks_like_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 80 or s.endswith((".", ",", ";", ":")):
        return False
    if NUMBERED_HEADING.match(s):
        return True
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters) and len(s.split()) <= 8
