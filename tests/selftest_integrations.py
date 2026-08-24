"""Integration-seam self-test: prove permission trimming (LAW 2) still holds at the
seams the four integrations attach to — WITHOUT needing any third-party package.

  - LangChain attaches to QueryService.retrieve()  -> test the trimmed retrieve core
  - GraphQL  attaches to api.resolver.search_resolver() -> test the resolver core

The third-party bindings (Strawberry schema, LangChain retriever, pgvector index, LLaMA
client) are thin wrappers over these cores and are compile-checked; their security
behavior is the behavior proven here. Dependency-free:  python3 tests/selftest_integrations.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import (  # noqa: E402
    ExtractiveLlm,
    HashingEmbedding,
    InMemoryIdentity,
    InMemoryIndex,
    InMemoryObjectStore,
    InMemoryQueue,
    PlainTextExtractor,
)
from dbsearch.api.resolver import search_resolver  # noqa: E402
from dbsearch.connectors.sharepoint import SharePointConnector  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.query import QueryService  # noqa: E402

TENANT = "acme-001"
FALCON = "falcon-ma-confidential"


def build_qs():
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    index = InMemoryIndex(store)
    run_ingestion(SharePointConnector(TENANT), queue, store, PlainTextExtractor(), HashingEmbedding(), index)
    identity = InMemoryIdentity({
        "alice": ["grp-all-consultants", "grp-falcon-team"],
        "bob": ["grp-all-consultants"],
    })
    return QueryService(index, identity, HashingEmbedding(), ExtractiveLlm(), store, tenant_id=TENANT)


def main():
    print("Integration-seam self-test (LAW 2 holds at the LangChain + GraphQL seams):")
    qs = build_qs()
    q = "confidential falcon merger acquisition target valuation"

    # --- LangChain seam: build_retriever() calls qs.retrieve(); test that core ---
    alice_chunks = qs.retrieve("alice", q)
    bob_chunks = qs.retrieve("bob", q)
    assert any(c.doc_external_id == FALCON for c in alice_chunks), "alice should retrieve Falcon"
    assert all(c.doc_external_id != FALCON for c in bob_chunks), "LAW 2 BREACH: bob retrieved Falcon"
    assert all(c.text for c in alice_chunks), "retrieved chunks must carry text for the LLM/LangChain"
    print(f"  PASS  retrieve() core (LangChain seam) trims  ->  alice={[c.doc_external_id for c in alice_chunks]}")
    print(f"        bob (no Falcon access)                  ->  bob={[c.doc_external_id for c in bob_chunks]}")

    # --- GraphQL seam: the Strawberry resolver calls search_resolver(); test that core ---
    alice_res = search_resolver(qs, "alice", q)
    bob_res = search_resolver(qs, "bob", q)
    assert any(c["doc"] == FALCON for c in alice_res["citations"]), "alice's GraphQL result should cite Falcon"
    assert all(c["doc"] != FALCON for c in bob_res["citations"]), "LAW 2 BREACH: bob's GraphQL result leaked Falcon"
    assert FALCON not in bob_res["authorized_docs"]
    print(f"  PASS  search_resolver() core (GraphQL seam) trims  ->  bob cites={[c['doc'] for c in bob_res['citations']]}")

    # --- Exercise the REAL third-party bindings if their libs are installed (else skip) ---
    try:
        from dbsearch.api.graphql_app import build_schema

        schema = build_schema(qs)
        # Identity comes from the request context, not a query arg (LAW 2).
        gql = '{ search(question: "%s") { citations { doc } authorizedDocs } }'
        ga = schema.execute_sync(gql % q, context_value={"user_oid": "alice"})
        gb = schema.execute_sync(gql % q, context_value={"user_oid": "bob"})
        assert ga.errors is None and gb.errors is None, (ga.errors, gb.errors)
        a_docs = [c["doc"] for c in ga.data["search"]["citations"]]
        b_docs = [c["doc"] for c in gb.data["search"]["citations"]]
        assert FALCON in a_docs and FALCON not in b_docs, (a_docs, b_docs)
        print(f"  PASS  REAL Strawberry GraphQL execute  ->  alice={a_docs}  bob={b_docs} (no Falcon)")
    except ImportError:
        print("  SKIP  Strawberry not installed (pip install '.[graphql]') — core proven above")

    try:
        from dbsearch.adapters.langchain import build_retriever

        da = [d.metadata["doc"] for d in build_retriever(qs, "alice").invoke(q)]
        db = [d.metadata["doc"] for d in build_retriever(qs, "bob").invoke(q)]
        assert FALCON in da and FALCON not in db, (da, db)
        print(f"  PASS  REAL LangChain retriever         ->  alice={da}  bob={db} (no Falcon)")
    except ImportError:
        print("  SKIP  langchain-core not installed (pip install '.[langchain]') — core proven above")

    print("\nALL INTEGRATION-SEAM TESTS PASSED — trimming holds where LangChain and GraphQL attach.")


if __name__ == "__main__":
    main()
