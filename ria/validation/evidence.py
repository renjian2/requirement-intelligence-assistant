"""Code-based evidence verification (Build Prompt v2, Stage 8).

The LLM's word is never trusted: a quote counts as verified only if it fuzzy-matches
the text of a segment that it cites.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

DEFAULT_THRESHOLD = 85.0
MAX_QUOTE_WORDS = 30


def _norm(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^\w\s']", " ", text)
    return " ".join(text.split())


@dataclass
class QuoteCheck:
    verified: bool
    score: float
    matched_segment_id: str | None
    reason: str


def verify_quote(quote: str, cited_segment_ids: list[str], segment_text: dict[str, str],
                 threshold: float = DEFAULT_THRESHOLD) -> QuoteCheck:
    if not quote or not quote.strip():
        return QuoteCheck(False, 0.0, None, "Empty evidence quote")
    missing = [sid for sid in cited_segment_ids if sid not in segment_text]
    if missing:
        return QuoteCheck(False, 0.0, None, f"Cited segment(s) do not exist: {', '.join(missing)}")
    if len(quote.split()) > MAX_QUOTE_WORDS:
        return QuoteCheck(False, 0.0, None, f"Quote longer than {MAX_QUOTE_WORDS} words")
    q = _norm(quote)
    best_score, best_id = 0.0, None
    for sid in cited_segment_ids:
        seg = _norm(segment_text[sid])
        score = 100.0 if q in seg else fuzz.partial_ratio(q, seg)
        if score > best_score:
            best_score, best_id = score, sid
    if best_score >= threshold:
        return QuoteCheck(True, best_score, best_id, "Quote found in cited segment")
    return QuoteCheck(False, best_score, best_id, "Quote not found in the cited segment(s)")
