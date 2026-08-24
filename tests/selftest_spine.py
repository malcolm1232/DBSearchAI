"""Phase 1 exit test (ROADMAP): prove the spine end-to-end AND that retrieval is
permission-faithful (LAW 2). Dependency-free — run with plain python3:

    python3 tests/selftest_spine.py

Flow: SharePoint(seed) -> queue-driven ingest (parse -> chunk+embed -> index) ->
QueryService (group-expand -> embed -> SECURITY-TRIM -> cited answer).

The decisive assertion: a user gets a cited answer from docs they're allowed to see,
and CANNOT retrieve a confidential doc restricted to a group they're not in — not in
results, not in citations, not in the LLM context.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.ports.base import ReadScope  # noqa: E402

from dbsearch.adapters.local import (  # noqa: E402
    ExtractiveLlm,
    HashingEmbedding,
    InMemoryIdentity,
    InMemoryIndex,
    InMemoryObjectStore,
    InMemoryQueue,
    PlainTextExtractor,
)
from dbsearch.connectors.sharepoint import SharePointConnector  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.query import QueryService  # noqa: E402

TENANT = "acme-001"
FALCON = "falcon-ma-confidential"  # restricted to grp-falcon-team


def build():
    store = InMemoryObjectStore()
    queue = InMemoryQueue()
    index = InMemoryIndex(store)
    embedder = HashingEmbedding()
    connector = SharePointConnector(tenant_id=TENANT)
    run_ingestion(connector, queue, store, PlainTextExtractor(), embedder, index)

    identity = InMemoryIdentity({
        "alice": ["grp-all-consultants", "grp-falcon-team"],  # on the Falcon deal team
        "bob": ["grp-all-consultants"],                        # NOT on the Falcon team
    })
    qs = QueryService(index, identity, embedder, ExtractiveLlm(), store, tenant_id=TENANT)
    return index, qs


def main():
    print("Spine self-test (SKILL.md LAW 2 — permission-faithful retrieval):")
    index, qs = build()

    # 0) Ingestion indexed all three seed docs.
    assert len(index._items) == 3, f"expected 3 chunks indexed, got {len(index._items)}"
    print("  PASS  ingested SharePoint seed end-to-end (3 docs indexed)")

    q = "confidential falcon merger acquisition target valuation"

    # 1) Alice (on the deal team) CAN retrieve the confidential doc, with a citation.
    a = qs.answer("alice", q)
    assert FALCON in a.retrieved_docs, "alice should retrieve the Falcon doc"
    assert any(c["doc"] == FALCON for c in a.citations), "alice's answer should cite Falcon"
    assert a.answer and "couldn't find" not in a.answer
    print(f"  PASS  alice (Falcon team) gets a cited answer  ->  cites {[c['doc'] for c in a.citations]}")

    # 2) Bob (NOT on the deal team) CANNOT — same query, confidential doc must vanish.
    b = qs.answer("bob", q)
    assert FALCON not in b.retrieved_docs, "LAW 2 BREACH: bob retrieved the Falcon doc"
    assert all(c["doc"] != FALCON for c in b.citations), "LAW 2 BREACH: bob's citations leak Falcon"
    print(f"  PASS  bob (no access) is denied the confidential doc  ->  authorized={b.retrieved_docs}")

    # 3) Bob CAN still retrieve what he's allowed to (proves trimming isn't just blanket-deny).
    b2 = qs.answer("bob", "retail bank digital transformation proposal")
    assert "prop-retail-bank-2025" in b2.retrieved_docs, "bob should see the public proposal"
    print(f"  PASS  bob retrieves authorized content  ->  cites {[c['doc'] for c in b2.citations]}")

    # 4) Index-level guarantee: the trim is in search(), not bolted on after.
    qv = qs._embedder.embed([q])[0]
    bob_hits = index.search(qv, ["bob", "grp-all-consultants"], top_k=5, scope=ReadScope(TENANT))
    assert all(h["doc_external_id"] != FALCON for h in bob_hits), "LAW 2 BREACH at index layer"
    print("  PASS  index.search() trims server-side (mandatory filter, not post-hoc)")

    print("\nALL SPINE SELF-TESTS PASSED — end-to-end cited answers + permission-faithful retrieval.")


if __name__ == "__main__":
    main()
