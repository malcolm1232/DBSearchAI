"""#939 - a connector store can say WHICH documents it holds, and how many, for THIS caller.

The launch gate (#895) requires "connect a node, verify it shows synced + doc count". Today a
gdrive node shows neither: `/admin/sources` lists only sharepoint rows, the node badge says
`syncing` forever, and nothing anywhere can name the files. Measured on prod 260823 with the
catalog reporting `ingested@08:58:31` while the badge still read `syncing`.

The data was never missing - it was never read. Every index adapter already implements
`list_doc_acls(scope)`, returning one row per document with its title, uri and the principals
allowed to see it. Both the local and the pgvector adapter have it, so this needs no new port
method and cannot drift between them (the #916 lesson: the reference adapter kept the contract
and the real ones dropped it).

WHAT IT DOES NEED IS A TRIM, AND THAT IS THE WHOLE RISK. `list_doc_acls` is an ADMIN surface -
it returns every document in the partition, untrimmed, because the permission tester's job is
to answer "who can see what". Handing that straight to a user-facing file list would publish
the names of documents they cannot read. So `document_inventory` intersects the caller's own
expanded principals with each row, and the two-identity tests below are the point of this file:
one that can see a document and one that cannot, asserting on what each is TOLD, not on what
the index holds.

  PYTHONPATH=src python3 tests/selftest_939_document_inventory.py
"""
import json
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import (  # noqa: E402
    ExtractiveLlm, HashingEmbedding, InMemoryIdentity, InMemoryIndex, InMemoryObjectStore,
)
from dbsearch.core.models import Chunk  # noqa: E402
from dbsearch.query.service import QueryService  # noqa: E402
from dbsearch.router.indexed_store import IndexedStore  # noqa: E402

TENANT = "gdrive-1"

# Two documents in ONE store, with DIFFERENT audiences. A fixture where every document is
# visible to everyone cannot fail on the trim, which is the only thing worth testing here.
DOCS = [
    ("c1", "drive-a", "DBSNotes.txt", "Wave 1 is pure canvas rendering fixes.", ["oid-alice"]),
    ("c2", "drive-a", "DBSNotes.txt", "Wave 2 is state and error honesty.", ["oid-alice"]),
    ("c3", "drive-b", "handbook.pdf", "Staff receive 25 days of leave.", ["oid-alice", "oid-bob"]),
]


def _service():
    obj = InMemoryObjectStore()
    index = InMemoryIndex(obj)
    emb = HashingEmbedding()
    chunks = []
    for cid, doc, title, text, acl in DOCS:
        tref, vref = f"t/{cid}", f"v/{cid}"
        obj.put(tref, text.encode())
        obj.put(vref, json.dumps(emb.embed([text])[0]).encode())
        chunks.append(Chunk(tenant_id=TENANT, chunk_id=cid, doc_external_id=doc, title=title,
                            uri=f"gdrive://{doc}", text_ref=tref, embedding_ref=vref,
                            allowed_principals=list(acl), locator={}))
    index.upsert(chunks)
    identity = InMemoryIdentity({"oid-alice": [], "oid-bob": []})
    qs = QueryService(index, identity, emb, ExtractiveLlm(), obj, tenant_id=TENANT)
    return qs, identity


def _store():
    qs, identity = _service()
    return IndexedStore(TENANT, "unassigned", TENANT, "", qs, identity)


def test_the_owner_sees_both_documents_once_each():
    """One row per DOCUMENT, not per chunk. DBSNotes.txt has two chunks and is one file."""
    qs, _ = _service()
    inv = qs.document_inventory("oid-alice")
    titles = sorted(r["title"] for r in inv)
    assert titles == ["DBSNotes.txt", "handbook.pdf"], titles
    assert len(inv) == 2, f"chunks leaked through as separate files: {inv}"


def test_a_document_carries_the_name_a_person_would_recognise():
    """The whole point of the card: 'did DBSNotes.txt land?' must be answerable by reading it.
    An opaque id is what #771 is already open about."""
    qs, _ = _service()
    row = next(r for r in qs.document_inventory("oid-alice") if r["doc"] == "drive-a")
    assert row["title"] == "DBSNotes.txt", row
    assert row["uri"], f"no uri to open the source with: {row}"


def test_a_second_identity_is_told_only_about_its_own_document():
    """LAW 2, and the reason this file exists.

    `list_doc_acls` is an admin surface and returns EVERY document in the partition. Bob is in
    the audience for handbook.pdf and not for DBSNotes.txt, so a file list that forwards the
    admin answer publishes a filename he may not see - a disclosure with no query attached."""
    qs, _ = _service()
    inv = qs.document_inventory("oid-bob")
    titles = [r["title"] for r in inv]
    assert titles == ["handbook.pdf"], f"bob was told about documents he cannot read: {titles}"
    assert all("DBSNotes" not in t for t in titles), titles


def test_an_identity_with_no_documents_gets_an_empty_list_not_everything():
    """The fail-open shape: a caller in nobody's audience must get [], never the whole store."""
    qs, _ = _service()
    assert qs.document_inventory("oid-nobody") == []


def test_the_store_exposes_the_same_inventory_through_its_access_context():
    """The router-facing seam the API will call, trimmed by the SAME identity the store
    authorized - not by an id the caller handed in."""
    st = _store()
    inv = st.documents(st.authorize("oid-bob"))
    assert [r["title"] for r in inv] == ["handbook.pdf"], inv
    assert len(st.documents(st.authorize("oid-alice"))) == 2


def test_the_count_is_documents_not_chunks():
    """#895 asks for a DOC COUNT. Three chunks, two files: reporting 3 would inflate every
    store by its chunking, which is a number about our pipeline and not about their folder."""
    st = _store()
    assert len(st.documents(st.authorize("oid-alice"))) == 2


if __name__ == "__main__":
    fails = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                fails.append(name)
                print(f"FAIL {name}\n     {exc}")
            except Exception as exc:  # a missing method is a failure, not an error to hide
                fails.append(name)
                print(f"FAIL {name}\n     {type(exc).__name__}: {exc}")
    print(f"\n{'FAILED' if fails else 'PASSED'}: {len(fails)} failure(s)")
    sys.exit(1 if fails else 0)
