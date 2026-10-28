"""Deterministic wording rules (no LLM)."""
from __future__ import annotations

import re

HEDGE = re.compile(
    r"\b(maybe|perhaps|possibly|might|could|we can discuss|discuss (?:it )?later|nice to have|"
    r"if possible|let'?s see|not sure|probably|shayad)\b", re.I)

VAGUE_TERMS = [
    "quickly", "quick", "soon", "fast", "user-friendly", "user friendly", "appropriate", "etc",
    "as needed", "some", "minimal", "robust", "real-time", "real time", "asap", "tbd", "tbc",
    "easy", "simple", "efficient", "flexible", "adequate", "reasonable", "timely", "seamless",
    "intuitive", "as soon as possible", "and/or", "several", "various", "normally", "usually",
]
_VAGUE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(t) for t in sorted(VAGUE_TERMS, key=len, reverse=True))
                    + r")(?![\w-])", re.I)


def is_hedged(text: str) -> bool:
    return bool(HEDGE.search(text or ""))


def find_vague_terms(*texts: str | None) -> list[str]:
    found: list[str] = []
    for t in texts:
        for m in _VAGUE.finditer(t or ""):
            term = m.group(1).lower()
            if term not in found:
                found.append(term)
    return found
