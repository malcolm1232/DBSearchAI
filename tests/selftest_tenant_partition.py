"""ADR 0012 (#389): the document plane is partitioned by tenant at RETRIEVAL time.

The breach this pins: before ADR 0012, search() trimmed ONLY on ACL overlap, so a
document with a wide ACL (e.g. `all-staff`) ingested by tenant B was retrievable by any
signed-in identity in tenant A whose principal set happened to intersect. The tenant
trim is a SECOND mandatory predicate alongside the ACL trim - a leak now needs both an
ACL collision AND a tenant-id collision, and tenant_id is a server-supplied verified
value, not user data.

Covers:
  1. signature drift: IndexPort.search and every shipped adapter REQUIRE tenant_id
     (required param - a caller that omits it fails loudly, never queries cross-tenant)
  2. InMemoryIndex: the wide-ACL case - tenant B's `all-staff` doc is invisible to a
     tenant-A search even when principals overlap; has_authorized is tenant-scoped too
  3. QueryService: tenant_id is REQUIRED at construction; retrieve() trims to it; a
     per-call override retargets the partition (the hosted per-request tid path)

Run: python3 tests/selftest_tenant_partition.py
"""
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.ports.base import ReadScope  # noqa: E402

from dbsearch.adapters.local import (ExtractiveLlm, HashingEmbedding, InMemoryIdentity,
                                     InMemoryIndex, InMemoryObjectStore)
from dbsearch.core.models import Chunk
from dbsearch.ports.base import IndexPort
from dbsearch.query import QueryService


def _required_params(fn) -> list[str]:
    sig = inspect.signature(fn)
    return [n for n, p in sig.parameters.items()
            if n != "self" and p.default is inspect.Parameter.empty
            and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]


def check_scope_defaults_are_closed() -> None:
    """#582: a ReadScope with no doorway shares NOTHING, and the doorway is never
    populated by default. A regression here would silently widen every read on the box."""
    from dbsearch.ports.base import as_read_scope

    assert ReadScope("tenant-a").doorway == frozenset()
    assert ReadScope("tenant-a").allows("tenant-a", "doc-1") is True
    assert ReadScope("tenant-a").allows("tenant-b", "doc-1") is False
    # A bare partition string widens to "own partition, no doorway" - never the reverse.
    assert as_read_scope("tenant-a").doorway == frozenset()
    assert as_read_scope("").partition == ""      # empty stays empty: matches no chunk

    # #790: THE LINE ABOVE PASSED THROUGHOUT THE DEFECT, because it uses the ONE-ARG form where
    # `default_partition` is itself "". Every real caller uses the TWO-ARG form, and there `""`
    # was rewritten into the deployment constant - the fail-closed value becoming the home
    # partition. The distinction the fix rests on is None vs "", so assert exactly that.
    assert as_read_scope("", "selfhost").partition == "", \
        "a partition that RESOLVED TO EMPTY (fail-closed) was widened to the deployment constant"
    assert as_read_scope(None, "selfhost").partition == "selfhost", \
        "omitting the partition must still mean 'this service's own tenant' (single-tenant path)"
    assert as_read_scope("tenant-a", "selfhost").partition == "tenant-a", \
        "an explicit partition must never be overridden by the default"
    # And the arm that was already safe, kept so the two cannot drift apart again: a ReadScope
    # carrying an empty partition is returned untouched. This is what REST passes and is the
    # reason REST was fail-closed while GraphQL was not.
    assert as_read_scope(ReadScope(""), "selfhost").partition == ""
    print("  ok: a scope with no doorway shares nothing, and empty never widens")


def check_signatures() -> None:
    """The partition argument is REQUIRED on the port and every shipped implementation - an
    adapter that drifts back to optional (or drops it) fails here, not in production.

    #582 / ADR 0019 renamed it `tenant_id` -> `scope` (a `ReadScope`: the caller's own
    partition PLUS a doorway of explicitly shared documents). The NAME changed; the
    property this test exists to protect did not, so the assertion is renamed rather than
    relaxed, and `check_scope_defaults_are_closed` pins the half that is new."""
    targets = [("IndexPort", IndexPort.search), ("InMemoryIndex", InMemoryIndex.search)]
    from dbsearch.adapters.vectordb.pgvector import PgVectorIndex
    targets.append(("PgVectorIndex", PgVectorIndex.search))
    try:
        from dbsearch.adapters.azure.aisearch import AiSearchIndex
        targets.append(("AiSearchIndex", AiSearchIndex.search))
    except Exception:
        pass  # azure sdk not installed - the port + local + pg checks still hold
    for name, fn in targets:
        req = _required_params(fn)
        assert "scope" in req, f"{name}.search: scope must be REQUIRED, got {req}"
    print("  ok: search() requires an explicit scope on the port and all shipped adapters")


def _mk_chunk(store, embedder, tenant, doc, text, acl, owner_oid=None):
    text_ref = store.put(f"chunk/{tenant}/{doc}/0", text.encode())
    vec = embedder.embed([text])[0]
    emb_ref = store.put(f"emb/{tenant}/{doc}/0", json.dumps(vec).encode())
    return Chunk(tenant_id=tenant, doc_external_id=doc, chunk_id=f"{doc}#0",
                 text_ref=text_ref, allowed_principals=acl, embedding_ref=emb_ref,
                 title=doc, uri=f"upload://{doc}", owner_oid=owner_oid)


def check_inmemory_wide_acl() -> None:
    store = InMemoryObjectStore()
    embedder = HashingEmbedding(dim=64)
    idx = InMemoryIndex(store)
    idx.upsert([
        # tenant A: a normal, narrowly-ACL'd doc
        _mk_chunk(store, embedder, "tenant-a", "a-handbook",
                  "quarterly revenue playbook alpha", ["grp-a"], owner_oid="oid-alice"),
        # tenant B: SAME topic, deliberately WIDE acl - the pre-ADR leak shape
        _mk_chunk(store, embedder, "tenant-b", "b-secret",
                  "quarterly revenue playbook omega", ["all-staff", "grp-a"]),
    ])
    qv = embedder.embed(["quarterly revenue playbook"])[0]

    # The caller's principals intersect BOTH docs' ACLs. Only the tenant trim separates them.
    hits_a = idx.search(qv, ["grp-a", "all-staff"], top_k=10, scope=ReadScope("tenant-a"))
    assert [h["doc_external_id"] for h in hits_a] == ["a-handbook"], hits_a
    hits_b = idx.search(qv, ["grp-a", "all-staff"], top_k=10, scope=ReadScope("tenant-b"))
    assert [h["doc_external_id"] for h in hits_b] == ["b-secret"], hits_b

    # a tenant nobody ingested under sees nothing, ever
    assert idx.search(qv, ["all-staff"], top_k=10, scope=ReadScope("tenant-c")) == []

    # the existence probe respects the same partition (health/exercise path)
    assert idx.has_authorized(["all-staff"], scope=ReadScope("tenant-b"))
    assert not idx.has_authorized(["all-staff"], scope=ReadScope("tenant-a")), \
        "has_authorized leaked existence across tenants"
    print("  ok: InMemoryIndex - wide-ACL doc invisible across tenants; existence probe scoped")


def check_query_service() -> None:
    store = InMemoryObjectStore()
    embedder = HashingEmbedding(dim=64)
    idx = InMemoryIndex(store)
    idx.upsert([
        _mk_chunk(store, embedder, "tenant-a", "a-doc", "onboarding checklist alpha", ["grp-a"]),
        _mk_chunk(store, embedder, "tenant-b", "b-doc", "onboarding checklist omega",
                  ["all-staff", "grp-a"]),
    ])
    identity = InMemoryIdentity({"alice": ["grp-a", "all-staff"]})

    # tenant_id is REQUIRED at construction - a QueryService cannot exist un-partitioned
    try:
        QueryService(idx, identity, embedder, ExtractiveLlm(), store)
        raise AssertionError("QueryService accepted construction WITHOUT tenant_id")
    except TypeError:
        pass

    qs = QueryService(idx, identity, embedder, ExtractiveLlm(), store, tenant_id="tenant-a")
    got = [c.doc_external_id for c in qs.retrieve("alice", "onboarding checklist")]
    assert got == ["a-doc"], f"constructor tenant must trim: {got}"

    # per-call override (the hosted path: the caller's verified tid wins per request)
    got_b = [c.doc_external_id for c in qs.retrieve("alice", "onboarding checklist",
                                                    tenant_id="tenant-b")]
    assert got_b == ["b-doc"], f"per-call tenant override must retarget the partition: {got_b}"

    # answer() carries the same partition through to citations
    r = qs.answer("alice", "onboarding checklist")
    assert r.retrieved_docs == ["a-doc"], r.retrieved_docs

    # the existence path is partition-aware too
    assert qs.has_visible_content("alice")
    assert not qs.has_visible_content("alice", tenant_id="tenant-nobody")
    print("  ok: QueryService - required at construction, trims retrieve/answer, per-call override")


def check_ingest_stamping() -> None:
    """The full pipeline stamps the caller's tenant AND owner onto every chunk — so a
    foreign ingest lands in the foreign partition, attributed, without any app-level code
    having to remember to do it."""
    from dbsearch.adapters.local import InMemoryQueue, PlainTextExtractor
    from dbsearch.connectors.sharepoint import SharePointConnector
    from dbsearch.pipeline.runner import run_ingestion

    store = InMemoryObjectStore()
    embedder = HashingEmbedding(dim=64)
    idx = InMemoryIndex(store)
    conn = SharePointConnector(
        tenant_id="tenant-foreign", owner_oid="oid-stranger",
        seed=[{"external_id": "f-doc", "title": "Foreign Doc", "uri": "sp://f",
               "acl": ["all-staff"], "text": "foreign quarterly notes"}])
    run_ingestion(conn, InMemoryQueue(), store, PlainTextExtractor(), embedder, idx)

    chunks = [c for (tid, _), (c, _) in idx._items.items() if tid == "tenant-foreign"]
    assert chunks, "ingest did not land in the caller's partition"
    assert all(c.owner_oid == "oid-stranger" for c in chunks), \
        f"owner attribution missing: {[c.owner_oid for c in chunks]}"

    qv = embedder.embed(["foreign quarterly notes"])[0]
    assert idx.search(qv, ["all-staff"], top_k=5, scope=ReadScope("tenant-foreign")), \
        "foreign partition must be retrievable BY the foreign tenant"
    assert not idx.search(qv, ["all-staff"], top_k=5, scope=ReadScope("tenant-home")), \
        "foreign ingest leaked into another partition"
    print("  ok: pipeline stamps caller tenant + owner_oid; partition holds both ways")


def main() -> None:
    check_signatures()
    check_scope_defaults_are_closed()
    check_inmemory_wide_acl()
    check_query_service()
    check_ingest_stamping()
    print("PASS selftest_tenant_partition (ADR 0012: tenant is a mandatory retrieval predicate)")


if __name__ == "__main__":
    main()
