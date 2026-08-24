"""Phase F (card #129) — federated answer-quality eval over RouterQueryService.

Extends the #42 single-QueryService eval to the federated router: same deterministic,
no-LLM-judge philosophy, plus two dimensions the router adds —
  1. ROUTING correctness (did the right store(s) get picked), and
  2. the MOAT as a metric: `leak_score` = 1.0 only if no forbidden store id appears in
     ANY surface (answer / citations / routing / outcomes / disclosure) and no forbidden
     fact appears. Gate #1 (existence) + gate #2 (delegated fail-closed) become a scored,
     aggregated number, so any regression drops a headline metric and fails the gate.

Hermetic and reusable: `run_router_eval(service, items, llm)` routes+asks each item once
and scores every dimension; `RouterEvalReport.passed(thresholds)` is the CI gate. Pure
scoring reuses `dbsearch.eval` (precision_at_k / key_fact_recall / abstained).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dbsearch.eval import abstained, hit_at_k, key_fact_recall, precision_at_k


@dataclass
class RouterGoldenItem:
    question: str
    as_user: str
    # store ids that SHOULD route: a list to score against, [] to expect NO store
    # (unanswerable / no-access), or None to skip routing scoring (e.g. a pure leak
    # item, where routing to an allowed store is fine and only leak_score matters).
    expected_stores: "list | None" = None
    relevant_docs: list = field(default_factory=list)     # evidence doc/row ids that should surface
    key_facts: list = field(default_factory=list)         # substrings a correct answer must contain
    answerable: bool = True                               # False -> a faithful answer abstains
    forbidden_stores: list = field(default_factory=list)  # ids that MUST NOT appear (gate #1)
    forbidden_facts: list = field(default_factory=list)   # substrings that MUST NOT appear


def _routed_ids(result: dict) -> list:
    return [s.get("store_id") for s in result.get("routing", {}).get("stores", [])]


def _citation_ids(result: dict) -> list:
    """Evidence identity for retrieval scoring: a citation's doc (indexed) or table (SQL)."""
    out = []
    for c in result.get("citations", []):
        out.append(c.get("doc") or c.get("table") or c.get("store_id"))
    return out


def _surface_blob(result: dict) -> str:
    """Every place a store id or fact could leak — answer, citations, routing, outcomes,
    disclosure. score_item asserts NOTHING forbidden appears anywhere in here (gate #1)."""
    import json

    return json.dumps(result, default=str).lower()


def score_item(result: dict, item: RouterGoldenItem, k: int = 5) -> dict:
    routed = _routed_ids(result)
    if item.expected_stores is None:
        routing_precision = None                          # routing not scored for this item
        routing_hit = None
    elif item.expected_stores:
        expected = set(item.expected_stores)
        routing_precision = (sum(1 for s in routed if s in expected) / len(routed)
                             if routed else 0.0)
        routing_hit = any(s in expected for s in routed)
    else:
        # expected NO store (unanswerable / no-access): hit iff nothing routed
        routing_precision = 1.0 if not routed else 0.0
        routing_hit = not routed

    cite_ids = _citation_ids(result)
    answer = result.get("answer", "")

    blob = _surface_blob(result)
    leaked = any(str(s).lower() in blob for s in item.forbidden_stores) or \
        any(str(f).lower() in blob for f in item.forbidden_facts)

    return {
        "routing_precision": routing_precision,
        "routing_hit": routing_hit,
        "precision_at_k": precision_at_k(cite_ids, item.relevant_docs, k)
        if item.relevant_docs else None,
        "hit_at_k": hit_at_k(cite_ids, item.relevant_docs, k)
        if item.relevant_docs else None,
        "key_fact_recall": key_fact_recall(answer, item.key_facts)
        if item.key_facts else None,
        "abstained": abstained(answer) if not item.answerable else None,
        "leak_score": 0.0 if leaked else 1.0,
    }


@dataclass
class RouterEvalReport:
    items: list                                   # [(RouterGoldenItem, scores_dict)]

    def summary(self) -> dict:
        """Mean of each dimension over the items where it applies (None = not scored)."""
        def mean(key, transform=lambda v: v):
            vals = [transform(s[key]) for _, s in self.items if s.get(key) is not None]
            return round(sum(vals) / len(vals), 4) if vals else None

        return {
            "n": len(self.items),
            "routing_hit": mean("routing_hit", lambda b: 1.0 if b else 0.0),
            "routing_precision": mean("routing_precision"),
            "hit_at_k": mean("hit_at_k", lambda b: 1.0 if b else 0.0),
            "precision_at_k": mean("precision_at_k"),
            "key_fact_recall": mean("key_fact_recall"),
            "abstention": mean("abstained", lambda b: 1.0 if b else 0.0),
            "leak_score": mean("leak_score"),
        }

    def passed(self, thresholds: dict) -> bool:
        """CI gate: every named metric must meet its floor. A metric that wasn't scored
        (None) fails a threshold placed on it — you can't pass a gate on evidence you
        never produced."""
        summ = self.summary()
        for metric, floor in thresholds.items():
            val = summ.get(metric)
            if val is None or val < floor:
                return False
        return True


def run_router_eval(service, items: list, llm) -> RouterEvalReport:
    """Route+ask each golden item ONCE through the real RouterQueryService (so gate-#1/#2
    trimming is exercised for real) and score every dimension."""
    scored = []
    for item in items:
        result = service.ask(item.as_user, item.question, llm).to_dict()
        scored.append((item, score_item(result, item)))
    return RouterEvalReport(items=scored)
