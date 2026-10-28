"""Reversible redaction applied BEFORE text is sent to an external AI provider.

* emails  -> [EMAIL_1]   * phone numbers -> [PHONE_1]
* speaker names (full and first names) -> [PERSON_1] (optional)

Role titles ("Claims Manager") are kept. The mapping stays on this machine and is used to
restore AI output for display. Evidence quotes are verified against the redacted text that
the model actually saw.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<![\w-])(?:\+?\d[\d\s-]{8,14}\d)(?![\w-])")
_PLACEHOLDER = re.compile(r"\[(?:EMAIL|PHONE|PERSON)_\d+\]")


@dataclass
class Redactor:
    redact_names: bool = True
    _forward: dict[str, str] = field(default_factory=dict)   # original -> placeholder
    _reverse: dict[str, str] = field(default_factory=dict)   # placeholder -> original
    _counts: dict[str, int] = field(default_factory=dict)
    _name_patterns: list[tuple[re.Pattern, str]] = field(default_factory=list)

    def _placeholder(self, kind: str, original: str) -> str:
        key = original.strip()
        if key not in self._forward:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            ph = f"[{kind}_{self._counts[kind]}]"
            self._forward[key] = ph
            self._reverse[ph] = key
        return self._forward[key]

    def register_speakers(self, speakers: list[str]) -> None:
        if not self.redact_names:
            return
        patterns: list[tuple[str, str]] = []
        for full in sorted({s for s in speakers if s}, key=len, reverse=True):
            ph = self._placeholder("PERSON", full)
            patterns.append((full, ph))
            first = full.split()[0]
            if len(full.split()) > 1 and len(first) >= 3 and first not in self._forward:
                self._forward[first] = ph  # first name maps to the same person
                patterns.append((first, ph))
        self._name_patterns = [(re.compile(rf"(?<!\w){re.escape(n)}(?!\w)"), ph) for n, ph in patterns]

    def redact(self, text: str | None) -> str | None:
        if not text:
            return text
        text = _EMAIL.sub(lambda m: self._placeholder("EMAIL", m.group()), text)
        text = _PHONE.sub(lambda m: self._placeholder("PHONE", m.group()), text)
        for pattern, ph in self._name_patterns:
            text = pattern.sub(ph, text)
        return text

    def restore(self, text: str | None) -> str | None:
        if not text:
            return text
        return _PLACEHOLDER.sub(lambda m: self._reverse.get(m.group(), m.group()), text)

    @property
    def replacements(self) -> int:
        return len(self._reverse)


class NoRedaction(Redactor):
    def register_speakers(self, speakers: list[str]) -> None:  # noqa: D401
        return

    def redact(self, text):  # type: ignore[override]
        return text

    def restore(self, text):  # type: ignore[override]
        return text
