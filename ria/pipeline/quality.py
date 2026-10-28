"""Deterministic extraction-quality checks (no LLM)."""
from __future__ import annotations

import re

from ria.models.enums import DocumentType, SegmentKind, WarningCode, WarningSeverity
from ria.models.segments import ExtractionWarning, Segment

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s-]{8,14}\d)(?!\w)")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
# Common romanised Hindi/Marathi words seen in Indian-English meetings.
_HINGLISH = {
    "hai", "hain", "nahi", "nahin", "haan", "zaroori", "jaroori", "chahiye", "karna",
    "karo", "kya", "bhi", "toh", "theek", "thik", "accha", "achha", "matlab", "abhi",
    "wala", "wali", "kar", "dena", "lena", "nako", "ahe", "pahije", "bas", "yaar",
}
_TRANSCRIPT_TYPES = {DocumentType.MEETING_TRANSCRIPT}


def run_quality_checks(segments: list[Segment], document_type: DocumentType,
                       detected_transcript: bool) -> list[ExtractionWarning]:
    w: list[ExtractionWarning] = []
    full = " ".join(s.text for s in segments)
    words = full.split()

    if not segments:
        return [ExtractionWarning(
            code=WarningCode.EMPTY_TEXT, severity=WarningSeverity.BLOCKING,
            message="No readable text was extracted.",
            action="Check that the file is not empty, scanned or protected.")]
    if len(words) < 30:
        w.append(ExtractionWarning(
            code=WarningCode.VERY_SHORT_TEXT, severity=WarningSeverity.WARNING,
            message=f"Only {len(words)} words were extracted.",
            action="Confirm this is the complete document."))

    bad = sum(1 for c in full if c == "\ufffd" or (ord(c) < 32 and c not in "\t\n"))
    odd = sum(1 for c in full if not (c.isalnum() or c.isspace() or c in ".,;:!?'\"()[]{}-–—/&%$₹€£@#*+=_<>|~`’‘“”…•"))
    if full and (bad / len(full) > 0.01 or odd / len(full) > 0.15):
        w.append(ExtractionWarning(
            code=WarningCode.GARBLED_TEXT, severity=WarningSeverity.WARNING,
            message="The extracted text contains many unreadable characters.",
            action="Check the preview. If unreadable, upload a DOCX/TXT version instead."))

    turns = [s for s in segments if s.kind == SegmentKind.SPEAKER_TURN]
    if document_type in _TRANSCRIPT_TYPES and not detected_transcript:
        w.append(ExtractionWarning(
            code=WarningCode.TYPE_MISMATCH, severity=WarningSeverity.WARNING,
            message="Marked as a transcript, but no speaker turns were detected.",
            action="Expected formats: VTT/SRT, Teams DOCX, or lines like 'Name: text'. "
                   "Analysis will continue without speaker information."))
    elif detected_transcript and document_type not in (*_TRANSCRIPT_TYPES, DocumentType.MEETING_NOTES):
        w.append(ExtractionWarning(
            code=WarningCode.TYPE_MISMATCH, severity=WarningSeverity.INFO,
            message="This file looks like a meeting transcript.",
            action="Consider changing the document type to 'Meeting transcript'."))
    if detected_transcript and turns:
        unknown = sum(1 for s in turns if not s.speaker)
        if unknown == len(turns):
            w.append(ExtractionWarning(
                code=WarningCode.MISSING_SPEAKERS, severity=WarningSeverity.WARNING,
                message="No speaker names were found in the transcript.",
                action="Decisions cannot be attributed to people. Enable speaker names in the export if possible."))
        elif unknown:
            w.append(ExtractionWarning(
                code=WarningCode.UNKNOWN_SPEAKER_TURNS,
                severity=WarningSeverity.WARNING if unknown / len(turns) > 0.1 else WarningSeverity.INFO,
                message=f"{unknown} of {len(turns)} turns have no speaker name.",
                action="Statements from these turns get lower confidence and cannot be attributed."))

    tokens = re.findall(r"[a-zA-Z]+", full.lower())
    hinglish_hits = sum(1 for t in tokens if t in _HINGLISH)
    if _DEVANAGARI.search(full) or hinglish_hits >= 3:
        w.append(ExtractionWarning(
            code=WarningCode.MIXED_LANGUAGE, severity=WarningSeverity.INFO,
            message="Mixed-language content detected (e.g. Hindi/Marathi words).",
            action="Original wording is kept in evidence quotes; unclear phrases will be flagged."))

    emails = len(set(_EMAIL.findall(full)))
    phones = len(set(_PHONE.findall(full)))
    if emails or phones:
        w.append(ExtractionWarning(
            code=WarningCode.PII_DETECTED, severity=WarningSeverity.INFO,
            message=f"Possible personal data found: {emails} email address(es), {phones} phone number(s).",
            action="Turn on redaction before sending this document to an external AI provider."))
    return w
