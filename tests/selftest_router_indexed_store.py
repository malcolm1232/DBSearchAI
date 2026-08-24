"""Phase E E1 — IndexedStore adapter self-test (LAW-2 trim preserved).
Run: python3 tests/selftest_router_indexed_store.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import (  # noqa: E402
    ExtractiveLlm, HashingEmbedding, InMemoryIdentity, InMemoryIndex,
    InMemoryObjectStore, InMemoryQueue, PlainTextExtractor,
)
from dbsearch.connectors.sharepoint import SharePointConnector  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.query import QueryService  # noqa: E402
from dbsearch.router.evidence import CHUNK  # noqa: E402
from dbsearch.router.indexed_store import IndexedStore  # noqa: E402

TENANT = "selfhost"


def _store():
    obj = InMemoryObjectStore()
    index = InMemoryIndex(obj)
    embedder = HashingEmbedding()
    seed = [
        {"external_id": "public-handbook", "title": "Staff Handbook",
         "uri": "https://ex/handbook", "acl": ["all-staff"],
         "text": "holidays expenses onboarding parental leave"},
        {"external_id": "deal-falcon", "title": "Project Falcon — Confidential",
         "uri": "https://ex/falcon", "acl": ["deal-team"],
         "text": "confidential falcon valuation numbers"},
    ]
    run_ingestion(SharePointConnector(tenant_id=TENANT, seed=seed),
                  InMemoryQueue(), obj, PlainTextExtractor(), embedder, index)
    identity = InMemoryIdentity({"alice": ["all-staff", "deal-team"], "bob": ["all-staff"]})
    qs = QueryService(index, identity, embedder, ExtractiveLlm(), obj, tenant_id=TENANT)
    return IndexedStore("hr-wiki", "hr", "HR Wiki", "hr policies", qs, identity)


def test_authorized_gets_evidence():
    s = _store()
    ev = s.retrieve(s.authorize("alice"), "confidential falcon valuation")
    docs = {e.provenance["doc"] for e in ev}
    assert "deal-falcon" in docs, docs
    e = next(e for e in ev if e.provenance["doc"] == "deal-falcon")
    assert e.kind == CHUNK, e
    assert e.business_unit == "hr", e
    assert e.provenance["uri"] == "https://ex/falcon", e


def test_unauthorized_is_trimmed():
    """bob (all-staff only) must NEVER receive the deal-team falcon doc — the
    per-store LAW-2 trim is what the router relies on."""
    s = _store()
    ev = s.retrieve(s.authorize("bob"), "confidential falcon valuation")
    docs = {e.provenance["doc"] for e in ev}
    assert "deal-falcon" not in docs, docs


def _store_described(desc):
    """Same seed as _store() but with a caller-set description, to exercise the #306 gate."""
    obj = InMemoryObjectStore()
    index = InMemoryIndex(obj)
    embedder = HashingEmbedding()
    run_ingestion(SharePointConnector(tenant_id=TENANT, seed=[
        {"external_id": "d1", "title": "Staff Handbook", "uri": "u", "acl": ["all-staff"], "text": "x"}]),
        InMemoryQueue(), obj, PlainTextExtractor(), embedder, index)
    identity = InMemoryIdentity({"alice": ["all-staff"]})
    qs = QueryService(index, identity, embedder, ExtractiveLlm(), obj, tenant_id=TENANT)
    return IndexedStore("s", "hr", "S", desc, qs, identity)


def test_profile_topics_reflect_the_indexed_doc_titles_when_undescribed():
    """#306: a document store with NO description of its own had a profile of just its id, so the
    router couldn't rank it on WHAT IT HOLDS — it fell below unrelated SQL stores whose #296 schema
    terms gave them noise scores ('epictus' ranked the SQL DBs above the store holding 'Epictus
    Discourses'). An UNDESCRIBED doc store now falls back to its ingested doc TITLES as topics."""
    from dbsearch.router.profiles import profile_text
    prof = _store_described("").profile()                         # empty description → titles used
    assert "Staff Handbook" in prof.topics, prof.topics
    assert "Handbook" in profile_text(prof), profile_text(prof)   # what the router embeds


def test_a_described_store_ALSO_folds_in_its_doc_titles():
    """#453 (flips the old #306 either/or gate): golden-suite task 10b proved the either/or wrong -
    a docs store WITH a real description (hr-wiki) had ZERO router-level signal for its actual
    content at any embedding dimension, because the description text never mentioned it and titles
    were only folded in when the description was empty. Description and titles are complementary
    signals, not alternatives, so a described store now ALSO gets its doc titles folded into
    topics, additively - the description is not replaced or shadowed, just supplemented."""
    prof = _store_described("human resources policies").profile()
    assert "Staff Handbook" in prof.topics, prof.topics
    assert prof.description == "human resources policies", prof


def test_has_content_is_existence_not_relevance():
    """#304: has_content answers 'does the caller have ANY retrievable doc?', independent of a
    relevance query. A health probe derived from the store's id ('hr-wiki') matches NO document
    text — retrieve() returns nothing — yet the source IS indexed and authorized, so has_content
    must be True. This is exactly the false 'not indexed yet' the SharePoint node hit."""
    s = _store()
    alice = s.authorize("alice")
    # the id-derived health probe finds nothing by RELEVANCE...
    assert s.retrieve(alice, "hr-wiki") == [], "precondition: the id probe matches no content"
    # ...but the source plainly HAS content the caller can see:
    assert s.has_content(alice), "an indexed, authorized source must report content is retrievable"
    assert s.has_content(s.authorize("bob")), "bob (all-staff) sees the handbook"


def test_has_content_false_for_an_identity_with_no_authorized_docs():
    """LAW 2: an identity in none of the docs' ACL groups has NO visible content — has_content
    must be False (so the source honestly reports degraded to THEM, never leaking existence)."""
    s = _store()   # 'nobody' is in no ACL group; expand_groups → ['nobody'], matching no doc
    assert not s.has_content(s.authorize("nobody")), "no authorized docs → no visible content"


def main():
    print("Phase E E1 IndexedStore self-test:")
    test_authorized_gets_evidence()
    test_unauthorized_is_trimmed()
    test_profile_topics_reflect_the_indexed_doc_titles_when_undescribed()
    test_a_described_store_ALSO_folds_in_its_doc_titles()
    print("  PASS  #306/#453  a document store's indexed doc titles are ALWAYS folded into routing "
          "topics, additively with its description (description and titles are complementary, not "
          "an either/or)")
    test_has_content_is_existence_not_relevance()
    test_has_content_false_for_an_identity_with_no_authorized_docs()
    print("  PASS  authorized evidence + provenance / unauthorized trimmed (LAW 2)")
    print("  PASS  #304  has_content = existence (not relevance); true for authorized, "
          "false for an identity with no visible docs")
    print("\nE1 INDEXED-STORE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
