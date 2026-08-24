"""RoutingDecision — the router's explainable output (Phase E E2, card #99).

`stores` is the SELECTED set (one, or a fan-out) the executor (E3) will query. `candidates`
is every VISIBLE store the router scored — transparency for the UI/advisor, and never
includes a store the caller can't see (gate #1). `reason` is the human 'which DB is
suitable' explanation; it names only visible stores. `method` records how the pick was
made: prefilter (embedding only) | llm (tiebreak used) | fallback (no signal).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RoutedStore:
    store_id: str
    business_unit: str
    score: float
    why: str = ""                # E7: per-store advisor explanation (match + shared terms)

    def to_dict(self) -> dict:
        return {"store_id": self.store_id, "business_unit": self.business_unit,
                "score": round(self.score, 4), "why": self.why}


@dataclass
class RoutingDecision:
    query_type: str                       # semantic | analytical | exact | compound
    stores: list[RoutedStore] = field(default_factory=list)      # selected (fan-out) set
    candidates: list[RoutedStore] = field(default_factory=list)  # all visible, scored
    confidence: float = 0.0
    reason: str = ""
    method: str = "prefilter"             # prefilter | llm | fallback | decompose (E6)
    sub_queries: list["SubQuery"] = field(default_factory=list)  # E6: compound only

    def to_dict(self) -> dict:
        return {
            "query_type": self.query_type,
            "stores": [s.to_dict() for s in self.stores],
            "candidates": [s.to_dict() for s in self.candidates],
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "method": self.method,
            "sub_queries": [sq.to_dict() for sq in self.sub_queries],
        }


@dataclass
class SubQuery:
    """One decomposed half of a compound question with its OWN routing decision (E6).
    Each sub-decision was made over the caller's visible catalog — gate #1 per sub —
    so serializing it can never name a store the caller isn't cleared to see."""
    question: str
    decision: RoutingDecision

    def to_dict(self) -> dict:
        return {"question": self.question, **self.decision.to_dict()}


def qualify(store_id: str, business_unit: "str | None", extra: str = "") -> str:
    """`store_id (business unit)` — with the parentheses dropped when they would say nothing.

    #729, second review. "unassigned" is the placeholder a store carries when nobody has given
    it a business unit; it names nothing, and printing it spends the reader's attention on a
    word that cannot help them. The first pass at this fixed the two canvas surfaces and the
    routed-reason string, and the commit message called that "the third and last surface". That
    was FALSE: an independent review found three more — the manually-pinned reason here, and
    both disclosure strings in the synthesizer — each with its own copy of the same f-string.
    The live capture missed all three because the asks routed cleanly, with no pin, no dropped
    store and no budget cap.

    So the rule now lives in ONE function that every site calls, because six copies of a rule is
    six chances to fix five of them. It also handles the empty case the pinned reason produced
    on its own: `target.profile` may be None, its fallback supplies "", and the old f-string
    rendered a bare `()` — punctuation around nothing."""
    inner = (business_unit or "").strip()
    if inner == "unassigned":
        inner = ""
    if extra:
        inner = f"{inner}: {extra}" if inner else extra
    return f"{store_id} ({inner})" if inner else store_id
