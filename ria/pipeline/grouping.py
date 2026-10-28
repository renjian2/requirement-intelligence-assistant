"""Group candidate statements by functional area before requirement synthesis (Stage 3).

Code does the grouping so each LLM call stays small enough for small models, and corrections
always travel with the statements they correct.
"""
from __future__ import annotations

import math
import re
from collections import OrderedDict

from ria.models.analysis import StatementRecord

GENERAL = "General"
_STOP = {
    "system", "shall", "must", "should", "with", "from", "that", "this", "will", "user", "users", "have",
    "need", "needs", "also", "each", "every", "when", "then", "into", "after", "before", "able", "more",
    "only", "than", "other", "process", "management", "module", "portal", "page", "data", "workflow",
}


def _tokens(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-zA-Z]{4,}", text.lower()):
        w = w[:-1] if w.endswith("s") and len(w) > 4 else w
        if w not in _STOP:
            out.add(w)
    return out


def _same(x: str, y: str) -> bool:
    """Exact match or a long shared prefix: approval ~ approved, notify ~ notification,
    but NOT receive ~ receipt."""
    if x == y:
        return True
    common = 0
    for a, b in zip(x, y):
        if a != b:
            break
        common += 1
    return common >= 5 and common >= min(6, min(len(x), len(y)) - 1)


def _weights(statements: list[StatementRecord]) -> dict[str, float]:
    """Inverse document frequency: words used in most statements (e.g. the domain noun 'claim')
    say little about the functional area, so they count less."""
    n = max(1, len(statements))
    df: dict[str, int] = {}
    for st in statements:
        for t in _tokens(f"{st.text} {st.evidence_quote}"):
            df[t] = df.get(t, 0) + 1
    return {t: math.log(1 + n / c) for t, c in df.items()}


def assign_group(statement: StatementRecord, modules: list[str], weights: dict[str, float] | None = None) -> str:
    weights = weights or {}
    st_tokens = _tokens(f"{statement.text} {statement.evidence_quote}")
    best, best_score = GENERAL, 0.0
    for m in modules:
        score = sum(max((weights.get(y, 1.0) for y in st_tokens if _same(x, y)), default=0.0)
                    for x in _tokens(m))
        if score > best_score + 1e-9:
            best, best_score = m, score
    return best


def build_groups(candidates: list[StatementRecord], all_statements: dict[str, StatementRecord],
                 modules: list[str], max_size: int) -> "OrderedDict[str, list[StatementRecord]]":
    modules = list(OrderedDict((m.strip().lower(), m.strip()) for m in modules if m and m.strip()).values())
    groups: "OrderedDict[str, list[StatementRecord]]" = OrderedDict()
    placed: dict[str, str] = {}
    weights = _weights(candidates)
    # Corrections first decide the group of the statement they supersede.
    for st in candidates:
        if st.statement_id in placed:
            continue
        group = assign_group(st, modules, weights)
        groups.setdefault(group, []).append(st)
        placed[st.statement_id] = group
    for st in list(candidates):
        if st.supersedes_id and st.supersedes_id in all_statements:
            target = all_statements[st.supersedes_id]
            current = placed.get(target.statement_id)
            if current and current != placed[st.statement_id]:
                groups[current] = [s for s in groups[current] if s.statement_id != target.statement_id]
            if placed.get(target.statement_id) != placed[st.statement_id]:
                groups[placed[st.statement_id]].append(target)
                placed[target.statement_id] = placed[st.statement_id]
    groups = OrderedDict((k, v) for k, v in groups.items() if v)
    # Split very large groups so each call stays within small-model limits.
    out: "OrderedDict[str, list[StatementRecord]]" = OrderedDict()
    for name, items in groups.items():
        items = sorted(items, key=lambda s: s.statement_id)
        if len(items) <= max_size:
            out[name] = items
            continue
        for i in range(0, len(items), max_size):
            out[f"{name} (part {i // max_size + 1})"] = items[i:i + max_size]
    return out
