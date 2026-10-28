"""Extractor registry."""
from __future__ import annotations

from ria.extractors.base import Extractor
from ria.extractors.docx import DocxExtractor
from ria.extractors.pdf import PdfExtractor
from ria.extractors.subtitles import SubtitleExtractor
from ria.extractors.tabular import TabularExtractor
from ria.extractors.text import TextExtractor

_EXTRACTORS: list[Extractor] = [
    TextExtractor(), DocxExtractor(), PdfExtractor(), SubtitleExtractor(), TabularExtractor(),
]


def get_extractor(extension: str) -> Extractor | None:
    ext = extension.lower()
    return next((e for e in _EXTRACTORS if ext in e.extensions), None)
