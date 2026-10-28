"""Find quantities (durations, amounts, percentages) in text - used for code-based contradiction
detection and for checking that generated acceptance criteria do not invent values."""
from __future__ import annotations

import re
from dataclasses import dataclass

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
          "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "million": 1_000_000,
           "crore": 10_000_000}
_WORD = "|".join(list(_UNITS) + list(_SCALES))
_WORD_NUM = re.compile(rf"\b(?:{_WORD})(?:(?:\s+|-|\s+and\s+)(?:{_WORD}))*\b", re.I)

NUM = r"(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?"
_CURRENCY = r"(?:INR|Rs\.?|₹|USD|US\$|\$|EUR|€|GBP|£)"
_CUR_WORDS = r"(?:rupees?|dollars?|euros?|pounds?)"
_DURATION = r"(?:(?:business|working|calendar)\s+)?(?:days?|weeks?|months?|hours?|hrs?|minutes?|mins?|years?)"
_COMPARATOR = (r"(?:above|below|over|under|exceeding|more than|less than|up to|upto|greater than|at least|"
               r"at most|maximum of|minimum of|max(?:imum)?|min(?:imum)?|limit of|threshold of)")

_PATTERNS = [
    ("amount", re.compile(rf"(?P<cur>{_CURRENCY})\s?(?P<num>{NUM})(?P<k>\s?[kK]\b)?")),
    ("amount", re.compile(rf"(?P<num>{NUM})(?P<k>\s?[kK]\b)?\s*(?P<cur>{_CUR_WORDS}|INR|USD|EUR|GBP)\b", re.I)),
    ("duration", re.compile(rf"(?P<num>{NUM})\s*-?\s*(?P<unit>{_DURATION})\b", re.I)),
    ("percent", re.compile(rf"(?P<num>{NUM})\s*(?:%|percent\b)", re.I)),
    ("amount", re.compile(rf"\b{_COMPARATOR}\s+(?P<num>{NUM})(?P<k>\s?[kK]\b)?(?!\s*(?:%|{_DURATION}|[\d.]))", re.I)),
]
_TO_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365, "hour": 1 / 24, "hr": 1 / 24, "minute": 1 / 1440,
            "min": 1 / 1440}
_CUR_NORMAL = {"inr": "INR", "rs": "INR", "rs.": "INR", "₹": "INR", "rupee": "INR", "rupees": "INR",
               "usd": "USD", "us$": "USD", "$": "USD", "dollar": "USD", "dollars": "USD", "eur": "EUR", "€": "EUR",
               "euro": "EUR", "euros": "EUR", "gbp": "GBP", "£": "GBP", "pound": "GBP", "pounds": "GBP"}


@dataclass(frozen=True)
class Quantity:
    kind: str              # amount | duration | percent
    value: float           # durations normalised to days
    display: str           # e.g. "45 days", "INR 1,000", "500 (no currency)"
    currency: str | None = None
    unit: str | None = None
    start: int = 0


def _words_to_number(words: str) -> int:
    total, current = 0, 0
    for w in re.split(r"[\s-]+", words.lower()):
        if w == "and" or not w:
            continue
        if w in _UNITS:
            current += _UNITS[w]
        elif w == "hundred":
            current = max(current, 1) * 100
        elif w in _SCALES:
            total += max(current, 1) * _SCALES[w]
            current = 0
    return total + current


def normalise_number_words(text: str) -> str:
    """'twenty five thousand' -> '25000' (keeps 'one' alone, which is usually not a quantity)."""
    def repl(m: re.Match) -> str:
        if m.group(0).lower() in ("one", "a"):
            return m.group(0)
        return str(_words_to_number(m.group(0)))
    return _WORD_NUM.sub(repl, text)


def _to_float(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k and k.strip() else v


def _fmt(v: float) -> str:
    return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"


def find_quantities(text: str) -> list[Quantity]:
    if not text:
        return []
    norm = normalise_number_words(text)
    taken: list[tuple[int, int]] = []
    out: list[Quantity] = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(norm):
            span = m.span("num")
            if any(a <= span[0] < b for a, b in taken):
                continue
            before = norm[max(0, m.start() - 8):m.start()].lower()
            if re.search(r"\bnot\s*$", before):          # "45 days, not 30 days" -> ignore 30
                continue
            taken.append(span)
            value = _to_float(m.group("num"), m.groupdict().get("k"))
            if kind == "duration":
                unit = m.group("unit").lower()
                base = next((u for u in _TO_DAYS if re.search(rf"\b{u}", unit)), "day")
                out.append(Quantity("duration", value * _TO_DAYS[base], f"{_fmt(value)} {unit}", unit=unit,
                                    start=m.start()))
            elif kind == "percent":
                out.append(Quantity("percent", value, f"{_fmt(value)}%", start=m.start()))
            else:
                cur_raw = (m.groupdict().get("cur") or "").strip().lower()
                cur = _CUR_NORMAL.get(cur_raw) if cur_raw else None
                disp = f"{cur} {_fmt(value)}" if cur else f"{_fmt(value)} (no currency)"
                out.append(Quantity("amount", value, disp, currency=cur, start=m.start()))
    return sorted(out, key=lambda q: q.start)


def numbers_in(text: str) -> set[str]:
    """All numeric values in a text (normalised, e.g. '25,000' and 'twenty five thousand' -> '25000')."""
    norm = normalise_number_words(text or "")
    found = set()
    for m in re.finditer(rf"(?<![\w.]){NUM}(?P<k>\s?[kK]\b)?", norm):
        num = re.match(NUM, m.group(0)).group(0)
        found.add(_fmt(_to_float(num, m.group("k"))).replace(",", ""))
    return found
