"""Query classifier — labels a question's shape (Phase E E2, card #99).

A lightweight, deterministic first pass: analytical (aggregate/filter), exact (point
lookup by id), compound (two things compared/joined), else semantic. It only LABELS —
compound DECOMPOSITION is E6, structured execution is E4. Real deployments can swap an
LLM classifier behind the same function signature.
"""
from __future__ import annotations

import re

_ANALYTICAL = re.compile(
    r"\b(how many|count|number of|total|sum|average|avg|mean|median|"
    r"trend|growth|per (month|quarter|year|region|unit)|by (month|quarter|year|region|unit))\b",
    re.I,
)
_EXACT = re.compile(r"(#\s*\d+|\b(id|invoice|record|ticket|order|employee)\b[^?]*\b\d+\b)", re.I)
_COMPOUND = re.compile(r"\b(versus|vs\.?|compare|compared to)\b|\b\w+\s+and\s+\w+.*\?", re.I)


def classify_query(question: str) -> str:
    q = question.strip()
    if _COMPOUND.search(q):
        return "compound"
    if _EXACT.search(q):
        return "exact"
    if _ANALYTICAL.search(q):
        return "analytical"
    return "semantic"
