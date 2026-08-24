"""Stage-1 retrieval scoring (spec 2026-07-31 section 4). Per store kind: doc qrels vs
citations[].doc, chunk qrels vs evidence[].provenance.locator, SQL identity vs
citations[].table only (row_ids are result-set indices, never qrels). Consumes the
plain /router/ask response dict; no server imports.

Helpers never mutate a caller's dict or list: each returns its own
(metrics_fragment, failures_fragment) pair and `score_stage1` merges them."""
from __future__ import annotations

_METRIC_KEYS = (
    "routing_hit", "routing_precision", "doc_recall_at_k", "doc_mrr",
    "chunk_recall_at_k", "table_hit", "distractor_cited", "forbidden_store_routed",
)


def recall_at_k(ranked: list, relevant, k: int) -> float:
    rel = set(relevant)
    if not rel:
        return 1.0
    return len(rel & set(ranked[:k])) / len(rel)


def mrr(ranked: list, relevant) -> float:
    rel = set(relevant)
    for i, item in enumerate(ranked):
        if item in rel:
            return 1.0 / (i + 1)
    return 0.0


def routed_stores(result: dict) -> list:
    return [s.get("store_id") for s in result.get("routing", {}).get("stores", [])]


def cited(result: dict, key: str) -> list:
    return [c.get(key) for c in result.get("citations", []) if c.get(key)]


def evidence_locators(result: dict) -> list:
    return [e.get("provenance", {}).get("locator")
            for e in result.get("evidence", []) if e.get("provenance", {}).get("locator")]


def _routing_scores(result: dict, item) -> tuple:
    """Mirrors router_eval.score_item lines 59-70: expect_stores non-empty scores
    hit+precision against routed stores; empty expect_stores only scores (hit iff
    nothing routed) when the item is unanswerable AND not protection="refused";
    otherwise routing is unscored. forbidden_store_routed is always scored regardless
    of the above.

    Amendment 260731a: a refused item's empty expect_stores is intentionally NOT scored
    as a routing check. Design #339's third bucket ("refused for everyone") makes the
    commercial assertion that the ANSWER refuses without confirming existence, not that
    the router fans out to nothing - a refused item can legitimately share real,
    non-collision vocabulary with an unrelated store's description ("company", "system",
    "records") and still route somewhere while the answer correctly stays a refusal.
    Scoring that as a routing failure conflated two different gates. Non-refused
    unanswerable items (empty expect_stores, protection="public"/"restricted") keep the
    original strictness unchanged. forbidden_store_routed still applies unconditionally:
    a refused item whose forbid_stores actually get routed still fails "forbidden-store"."""
    routed = routed_stores(result)
    metrics: dict = {}
    failures: list = []
    if item.expect_stores:
        expected = set(item.expect_stores)
        metrics["routing_hit"] = any(s in expected for s in routed)
        metrics["routing_precision"] = (
            sum(1 for s in routed if s in expected) / len(routed) if routed else 0.0)
        if not metrics["routing_hit"]:
            failures.append("routing")
    elif not item.answerable and item.protection != "refused":
        metrics["routing_hit"] = not routed
        metrics["routing_precision"] = 1.0 if not routed else 0.0
        if routed:
            failures.append("routing")
    metrics["forbidden_store_routed"] = any(s in item.forbid_stores for s in routed)
    if metrics["forbidden_store_routed"]:
        failures.append("forbidden-store")
    return metrics, failures


def _doc_scores(result: dict, item, k: int) -> tuple:
    """doc_qrels against citations[].doc, chunk_qrels against evidence provenance
    locators, negative_qrels (distractors) against citations[].doc."""
    metrics: dict = {}
    failures: list = []
    if item.doc_qrels:
        docs = cited(result, "doc")
        metrics["doc_recall_at_k"] = recall_at_k(docs, item.doc_qrels, k)
        metrics["doc_mrr"] = mrr(docs, item.doc_qrels)
        if metrics["doc_recall_at_k"] == 0.0:
            failures.append("doc-qrels")
    if item.chunk_qrels:
        locs = evidence_locators(result)
        metrics["chunk_recall_at_k"] = recall_at_k(locs, item.chunk_qrels, k)
        if metrics["chunk_recall_at_k"] == 0.0:
            failures.append("chunk-qrels")
    if item.negative_qrels:
        metrics["distractor_cited"] = any(
            d in item.negative_qrels for d in cited(result, "doc"))
        if metrics["distractor_cited"]:
            failures.append("distractor-cited")
    return metrics, failures


def _table_score(result: dict, item) -> tuple:
    """SQL items score gold_table identity against citations[].table only; row_ids
    are result-set indices, never qrels."""
    metrics: dict = {}
    failures: list = []
    if item.gold_table:
        metrics["table_hit"] = item.gold_table in cited(result, "table")
        if not metrics["table_hit"]:
            failures.append("table")
    return metrics, failures


def score_stage1(result: dict, item, k: int = 5) -> dict:
    """Score one golden item's stage-1 retrieval dimensions. Unscored dimensions stay
    None and are never added to failures."""
    metrics = {key: None for key in _METRIC_KEYS}
    failures: list = []
    for frag_metrics, frag_failures in (
        _routing_scores(result, item),
        _doc_scores(result, item, k),
        _table_score(result, item),
    ):
        metrics.update(frag_metrics)
        failures.extend(frag_failures)
    return {"metrics": metrics, "failures": failures}
