"""#44 — hybrid retrieval reranking (vector + lexical via RRF).

Proves: lexical_score behaves; hybrid_rerank rescues an exact-keyword match that pure vector
ranks too low; and reranking, applied after the trim, never breaks LAW 2 (a reranked
QueryService still hides docs the user can't see).

    python3 tests/selftest_rerank.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIdentity, InMemoryIndex, InMemoryObjectStore,
    InMemoryQueue, PlainTextExtractor,
)
from dbsearch.connectors.upload import UploadConnector  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.query import QueryService  # noqa: E402
from dbsearch.query.rerank import (  # noqa: E402
    content_overlap, hybrid_rerank, lexical_score, relevance_floor, shared_content_count,
)


class Hit:
    def __init__(self, score, text):
        self.score = score
        self.text = text


class _Noop:
    def answer(self, q, ctx):
        return {"answer": "", "citations": []}


def test_lexical_score():
    assert lexical_score("quantum encryption", "a quantum encryption scheme") == 1.0
    assert lexical_score("quantum encryption", "quantum mechanics") == 0.5
    assert lexical_score("anything", "") == 0.0
    assert lexical_score("", "text") == 0.0
    print("  PASS  lexical_score (term-overlap fraction)")


def test_rerank_rescues_keyword_match():
    q = "quantum encryption protocol"
    hits = [
        Hit(0.90, "alpha beta gamma"),                 # high vector, zero keyword overlap
        Hit(0.80, "delta epsilon zeta"),               # mid vector, zero keyword overlap
        Hit(0.10, "the quantum encryption protocol spec"),  # low vector, EXACT keyword match
    ]
    # pure vector top-2 would be hits[0], hits[1] — the exact match is missed.
    vec_top2 = sorted(hits, key=lambda h: h.score, reverse=True)[:2]
    assert hits[2] not in vec_top2, "precondition: pure vector misses the keyword doc"
    # hybrid rerank top-2 rescues the exact keyword doc.
    reranked = hybrid_rerank(q, hits, top_k=2)
    assert hits[2] in reranked, "hybrid rerank should rescue the exact-keyword match"
    print(f"  PASS  hybrid_rerank rescues keyword match (vector missed it; RRF top-2 includes it)")


def test_rerank_preserves_law2():
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    index = InMemoryIndex(store)
    embedder = HashingEmbedding()
    for ext, text, acl in [
        ("deal-falcon", "confidential falcon merger acquisition valuation deal team", ["deal-team"]),
        ("handbook", "general staff handbook holidays expenses onboarding all staff", ["all-staff"]),
    ]:
        conn = UploadConnector("t", ext, ext, text.encode(), "text/plain", acl, "")
        run_ingestion(conn, queue, store, PlainTextExtractor(), embedder, index)
    identity = InMemoryIdentity({"alice": ["all-staff", "deal-team"], "bob": ["all-staff"]})
    qs = QueryService(index, identity, embedder, _Noop(), store, rerank=True, tenant_id="t")
    q = "confidential falcon merger valuation"
    a = [c.doc_external_id for c in qs.retrieve("alice", q)]
    b = [c.doc_external_id for c in qs.retrieve("bob", q)]
    assert "deal-falcon" in a, "alice should retrieve the deal doc (positive control)"
    assert "deal-falcon" not in b, "LAW 2 BREACH: rerank let bob see a deal-team doc"
    print(f"  PASS  reranked QueryService still trims  ->  alice={a}  bob={b}")


def test_content_overlap_ignores_stopwords():
    # the question's only CONTENT terms are holiday/expenses/policy
    q = "what is our holiday and expenses policy"
    # a doc sharing only stopwords ("what is our ... and") must score 0, not 4/7
    assert content_overlap(q, "what love is and what our heart wants") == 0.0
    # a doc with a real content term scores > 0
    assert content_overlap(q, "annual holiday leave for all staff") > 0.0
    print("  PASS  content_overlap ignores stopwords (filler-word match -> 0)")


def test_relevance_floor_drops_filler():
    q = "what is our holiday and expenses policy"
    hits = [
        Hit(0.90, "all staff receive annual holiday leave and expenses are reimbursed"),  # shares 2
        Hit(0.30, "what love is: our heart and its eight rules by jay shetty"),           # shares 0
    ]
    kept = relevance_floor(q, hits)  # default: lexical-only (vector rescue off)
    assert hits[0] in kept, "the on-topic handbook chunk (shares holiday+expenses) must survive"
    assert hits[1] not in kept, "off-topic filler (shares no content term) must be dropped"
    # vector rescue (opt-in) keeps a strong SEMANTIC match that shares no keywords
    semantic = [Hit(0.95, "time off and reimbursement entitlements"), Hit(0.10, "jay shetty love")]
    assert semantic[0] in relevance_floor(q, semantic, rel_score=0.6), "high-vector match kept w/ rescue"
    assert semantic[0] not in relevance_floor(q, semantic), "but NOT kept when rescue is off (noisy embedder)"
    # enabled=False disables the floor entirely (reversible config knob)
    assert relevance_floor(q, hits, enabled=False) == hits
    print("  PASS  relevance_floor: count-based lexical drops filler; vector rescue is opt-in")


def test_floor_keeps_subtopic_of_long_query():
    # Regression for the proposal case: a relevant doc that answers ONE part of a long brief
    # shares few of the query's many terms — must NOT be dropped (count-relative, not fraction).
    q = "advise a retail bank on a confidential acquisition and staff onboarding"
    hits = [
        Hit(0.50, "confidential project falcon merger acquisition valuation deal team"),  # shares 2
        Hit(0.50, "general staff handbook holidays expenses onboarding for all staff"),   # shares 2
        Hit(0.20, "tonight's basketball scores and weather forecast for the weekend"),    # shares 0
    ]
    kept = relevance_floor(q, hits)
    assert hits[0] in kept and hits[1] in kept, "both on-topic docs (each answers part of the brief) kept"
    assert hits[2] not in kept, "the off-topic sports/weather chunk is dropped"
    assert shared_content_count(q, hits[0].text) == 2
    print("  PASS  floor keeps sub-topic matches of a long multi-topic query (proposal regression)")


def test_floor_fixes_irrelevant_source_e2e():
    # Reproduces the reported bug end-to-end: an HR query must NOT cite an off-topic book.
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    index = InMemoryIndex(store)
    embedder = HashingEmbedding()
    for ext, text in [
        ("staff-handbook", "all staff receive annual holiday leave and expenses are reimbursed monthly against receipts"),
        ("jay-shetty-love", "eight rules of love by jay shetty about relationships breakups and heart"),
    ]:
        conn = UploadConnector("t", ext, ext, text.encode(), "text/plain", ["all-staff"], "")
        run_ingestion(conn, queue, store, PlainTextExtractor(), embedder, index)
    identity = InMemoryIdentity({"alice": ["all-staff"]})
    q = "what is our holiday and expenses policy"
    # floor OFF: the book leaks into the cited sources (the bug)
    leaky = QueryService(index, identity, embedder, _Noop(), store, rerank=True, relevance_floor=0.0, tenant_id="t")
    leaked = [c.doc_external_id for c in leaky.retrieve("alice", q)]
    # floor ON (default): only the on-topic handbook survives
    fixed = QueryService(index, identity, embedder, _Noop(), store, rerank=True, tenant_id="t")
    cited = [c.doc_external_id for c in fixed.retrieve("alice", q)]
    assert "staff-handbook" in cited, "the handbook must still be retrieved+cited"
    assert "jay-shetty-love" not in cited, "BUG: off-topic book leaked into cited sources"
    print(f"  PASS  e2e floor fixes irrelevant source  ->  off={leaked}  on={cited}")


def main():
    print("Hybrid-rerank self-test (#44):")
    test_lexical_score()
    test_rerank_rescues_keyword_match()
    test_rerank_preserves_law2()
    print("Relevance-floor self-test (#53):")
    test_content_overlap_ignores_stopwords()
    test_relevance_floor_drops_filler()
    test_floor_keeps_subtopic_of_long_query()
    test_floor_fixes_irrelevant_source_e2e()
    print("\nALL RERANK TESTS PASSED — hybrid retrieval improves ranking; trim stays faithful; filler isn't cited.")


if __name__ == "__main__":
    main()
