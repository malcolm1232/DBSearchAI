"""#498 - a lexical candidate source feeds the retrieval pool (keyword alongside vector).

Measured (findings s15/s16): the needle class - "What is Loo Say Hoo's job title?"
against one line of raw JSON 200KB deep - is invisible to embeddings: the fact-chunk has
no meaning-shape, so it never enters the vector-selected candidate pool, and the #44
hybrid rerank can only reorder a pool the needle never joined. The fix is a SECOND
candidate source: `lexical_search` scores chunks by question-term hits - mechanical,
embedding-free, exactly where raw JSON is strongest ("Loo Say Hoo" is an exact token
match) - and retrieve() unions both pools before the existing rerank.

LAW 2 / ADR 0012 are pinned here: the lexical path applies the SAME principals trim and
tenant partition inside the index as vector search does.

Run: PYTHONPATH=src python3 tests/selftest_lexical_candidates.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.ports.base import ReadScope  # noqa: E402

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIdentity, InMemoryIndex, InMemoryObjectStore,
)
from dbsearch.core.models import Chunk  # noqa: E402
from dbsearch.query.service import QueryService  # noqa: E402


def _mk_index(store, docs):
    """docs: [(chunk_id, doc_id, text, acl)] -> a populated InMemoryIndex."""
    import json
    index = InMemoryIndex(store)
    emb = HashingEmbedding()
    chunks = []
    for chunk_id, doc_id, text, acl in docs:
        text_ref = f"t/{chunk_id}"
        store.put(text_ref, text.encode())
        vec_ref = f"v/{chunk_id}"
        store.put(vec_ref, json.dumps(emb.embed([text])[0]).encode())
        chunks.append(Chunk(tenant_id="t1", chunk_id=chunk_id, doc_external_id=doc_id,
                            title=doc_id, uri=f"u/{doc_id}", text_ref=text_ref,
                            embedding_ref=vec_ref, allowed_principals=list(acl),
                            locator={}))
    index.upsert(chunks)
    return index


NEEDLE = ('{"displayName": "Loo Say Hoo", "jobTitle": "Manager, Key Account", '
          '"officeLocation": "Central - Shah Alam"}')
FILLER = [
    (f"dir#{i}", "directory",
     f'{{"displayName": "Person {i}", "jobTitle": "Analyst {i}", "officeLocation": "HQ"}}',
     ["all"]) for i in range(6)
]
DOCS = FILLER + [("dir#99", "directory", NEEDLE, ["all"]),
                 ("memo#0", "memo", "The staff directory lists every employee's job "
                                    "title and office location for the company.", ["all"])]


def test_lexical_search_scores_by_term_hits_with_the_same_trims():
    store = InMemoryObjectStore()
    index = _mk_index(store, DOCS + [("secret#0", "secret",
                                      "Loo Say Hoo salary is confidential", ["hr-only"])])
    hits = index.lexical_search(["loo", "say", "hoo", "job", "title"],
                                ["all"], 5, ReadScope("t1"))
    assert hits, "the needle must be findable lexically"
    assert hits[0]["chunk_id"] == "dir#99", hits[0]
    # LAW 2: the hr-only chunk matches the terms but must NEVER surface for ['all']
    assert all(h["chunk_id"] != "secret#0" for h in hits), hits
    # ADR 0012: wrong tenant -> nothing, however good the match
    assert index.lexical_search(["loo"], ["all"], 5, ReadScope("t2")) == []


def test_retrieve_unions_lexical_candidates_into_the_pool():
    """The end-to-end needle case: vector search ranks the meaning-shaped memo and the
    filler above the raw-JSON needle; the lexical source must carry it into the pool,
    and the #44 rerank (which scores lexical overlap) surfaces it in top_k."""
    store = InMemoryObjectStore()
    index = _mk_index(store, DOCS)
    qs = QueryService(index=index, identity=InMemoryIdentity({"u1": ["all"]}),
                      embedder=HashingEmbedding(), llm=None, store=store,
                      top_k=5, tenant_id="t1")
    chunks = qs.retrieve("u1", "What is Loo Say Hoo's job title in the staff directory?")
    texts = [c.text for c in chunks]
    assert any("Manager, Key Account" in t for t in texts), (
        f"the needle chunk never reached top_k: {[t[:60] for t in texts]}")


def test_lexical_additions_are_capped_so_they_cannot_flood_the_pool():
    """Measured live (bake doc_a, 22/33): an uncapped lexical union - candidate_k=20
    keyword hits - FLOODED the rerank and pushed semantically-correct documents to MRR
    0.0 on four previously-passing items (retrieval-miss). The keyword source exists to
    sneak a NEEDLE into the pool, not to replace the pool: additions are capped at
    _LEXICAL_ADDITIONS, enough for a needle plus margin, few enough that semantic
    winners keep their seats."""
    from dbsearch.query.service import _LEXICAL_ADDITIONS

    store = InMemoryObjectStore()
    # many chunks that ALL match the question terms lexically
    noisy = [(f"n#{i}", "noise", f"job title job title filler {i}", ["all"])
             for i in range(15)]
    index = _mk_index(store, DOCS + noisy)
    calls = {}
    real_lex = index.lexical_search
    def spy(terms, principals, k, tid):
        out = real_lex(terms, principals, k, tid)
        calls["returned"] = len(out)
        return out
    index.lexical_search = spy
    qs = QueryService(index=index, identity=InMemoryIdentity({"u1": ["all"]}),
                      embedder=HashingEmbedding(), llm=None, store=store,
                      top_k=5, tenant_id="t1")
    qs.retrieve("u1", "What is Loo Say Hoo's job title in the staff directory?")
    assert calls["returned"] <= _LEXICAL_ADDITIONS, (
        f"lexical source returned {calls['returned']} - the pool floods again")


def test_an_index_without_the_capability_degrades_gracefully():
    class _NoLex(InMemoryIndex):
        lexical_search = None
    store = InMemoryObjectStore()
    index = _NoLex(store)
    import json
    from dbsearch.core.models import Chunk as C
    emb = HashingEmbedding()
    store.put("t/a", b"parental leave is sixteen weeks")
    store.put("v/a", json.dumps(emb.embed(["parental leave is sixteen weeks"])[0]).encode())
    index.upsert([C(tenant_id="t1", chunk_id="a#0", doc_external_id="a", title="a",
                    uri="u", text_ref="t/a", embedding_ref="v/a",
                    allowed_principals=["all"], locator={})])
    qs = QueryService(index=index, identity=InMemoryIdentity({"u1": ["all"]}),
                      embedder=HashingEmbedding(), llm=None, store=store,
                      top_k=5, tenant_id="t1")
    chunks = qs.retrieve("u1", "how long is parental leave")
    assert chunks and "sixteen weeks" in chunks[0].text


def main():
    test_lexical_search_scores_by_term_hits_with_the_same_trims()
    test_retrieve_unions_lexical_candidates_into_the_pool()
    test_lexical_additions_are_capped_so_they_cannot_flood_the_pool()
    test_an_index_without_the_capability_degrades_gracefully()
    print("  PASS  #498 lexical candidates: term-hit scoring with LAW 2/ADR 0012 trims, "
          "pool union reaches top_k, capability-gated")
    print("\nLEXICAL-CANDIDATES SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
