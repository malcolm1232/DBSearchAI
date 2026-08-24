"""#582 - the in-memory doorway: own partition OR an explicitly shared document (ADR 0019 D3).

The seam this closes: a grant writes `grant:<id>` onto the document's ACL inside the
GRANTOR's partition, but the grantee retrieves through THEIR OWN partition, and the
partition filter runs BEFORE the ACL overlap. So the grant principal was inert and the
share returned 200 having shared nothing.

The doorway opens ONE document, never the partition it lives in, and it never overrides
the ACL - LAW 2 remains the single enforcement point.

    PYTHONPATH=src python3 tests/selftest_582_doorway_local.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import InMemoryIndex, InMemoryObjectStore  # noqa: E402
from dbsearch.core.models import Chunk  # noqa: E402
from dbsearch.ports.base import ReadScope  # noqa: E402

VEC = [1.0, 0.0]


def _index():
    """One document, owned by alice, in alice's private per-account partition. Its ACL
    already carries the grant principal - that is what a share does (ADR 0017 s1)."""
    store = InMemoryObjectStore()
    store.put("emb/a", json.dumps(VEC).encode())
    store.put("txt/a", b"the payment terms are net 45")
    idx = InMemoryIndex(store)
    idx.upsert([Chunk(tenant_id="acct:alice", doc_external_id="doc-1", chunk_id="c1",
                      text_ref="txt/a", allowed_principals=["alice", "grant:g1"],
                      embedding_ref="emb/a", title="Terms", uri="",
                      locator={"kind": "page", "n": 1}, owner_oid="alice")])
    return store, idx


def test_without_the_doorway_bob_sees_nothing():
    """The #582 bug, pinned: bob holds the grant principal and still gets nothing,
    because the partition filter ran first."""
    _store, idx = _index()
    assert idx.search(VEC, ["bob", "grant:g1"], 5, ReadScope(partition="acct:bob")) == []


def test_with_the_doorway_bob_sees_exactly_the_shared_document():
    _store, idx = _index()
    scope = ReadScope(partition="acct:bob", doorway=frozenset({("acct:alice", "doc-1")}))
    hits = idx.search(VEC, ["bob", "grant:g1"], 5, scope)
    assert len(hits) == 1, hits
    assert hits[0]["doc_external_id"] == "doc-1", hits


def test_the_doorway_is_not_a_partition_skeleton_key():
    """A doorway pair opens ONE document. The rest of the grantor's partition stays shut,
    even for a document whose ACL would otherwise match."""
    store, idx = _index()
    store.put("emb/b", json.dumps(VEC).encode())
    store.put("txt/b", b"an unrelated secret in the same partition")
    idx.upsert([Chunk(tenant_id="acct:alice", doc_external_id="doc-2", chunk_id="c2",
                      text_ref="txt/b", allowed_principals=["alice", "grant:g1"],
                      embedding_ref="emb/b", title="Other", uri="",
                      locator={"kind": "page", "n": 1}, owner_oid="alice")])
    scope = ReadScope(partition="acct:bob", doorway=frozenset({("acct:alice", "doc-1")}))
    got = {h["doc_external_id"] for h in idx.search(VEC, ["bob", "grant:g1"], 5, scope)}
    assert got == {"doc-1"}, got


def test_the_doorway_never_overrides_the_acl():
    """LAW 2 stays THE enforcement point. The doorway says where retrieval may look; the
    ACL still decides what it may see."""
    _store, idx = _index()
    scope = ReadScope(partition="acct:bob", doorway=frozenset({("acct:alice", "doc-1")}))
    assert idx.search(VEC, ["bob"], 5, scope) == []


def test_empty_doorway_is_exactly_todays_behaviour():
    _store, idx = _index()
    assert len(idx.search(VEC, ["alice"], 5, ReadScope(partition="acct:alice"))) == 1
    assert idx.search(VEC, ["alice"], 5, ReadScope(partition="acct:nobody")) == []


def test_lexical_search_honours_the_doorway_too():
    """#498's keyword rail is a second candidate source. If it ignored the doorway, a
    shared document would answer vector questions and silently miss keyword ones."""
    _store, idx = _index()
    scope = ReadScope(partition="acct:bob", doorway=frozenset({("acct:alice", "doc-1")}))
    hits = idx.lexical_search(["net 45"], ["bob", "grant:g1"], 5, scope)
    assert len(hits) == 1, hits
    assert idx.lexical_search(["net 45"], ["bob", "grant:g1"], 5,
                              ReadScope(partition="acct:bob")) == []


def test_corpus_status_counts_a_shared_document():
    """Otherwise the grantee is told 'no documents indexed' while holding a live share."""
    _store, idx = _index()
    scope = ReadScope(partition="acct:bob", doorway=frozenset({("acct:alice", "doc-1")}))
    status = idx.corpus_status(scope, ["bob", "grant:g1"])
    assert status.indexed is True, status
    assert status.authorized_docs == 1, status


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
