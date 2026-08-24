"""#834 - the embedding vector rides the queue message; emb/ blobs stop existing.

Measured before designing (the handover said the opposite): every index adapter reads
`embedding_ref` exactly ONCE, at upsert - pgvector to fill its vector column, the in-memory
index to fill its (chunk, vec) cache that search actually scores against, AI Search to fill
its payload. Nothing reads an emb/ blob after indexing, and nothing rehydrates from one. So
the blob was never per-backend state - it was a hand-off buffer between two pipeline stages
that already share an in-process queue, paying 20% of prod's store (43MB of 212MB) to move a
value one function call to the right.

Three properties pinned here:

  1. NO NEW emb/ BLOBS. A full ingest leaves zero emb/ keys in the object store, and the
     answers still cite - the vector arrived through the message, not the store.
  2. LEGACY CHUNKS STILL INDEX. A chunk carrying only embedding_ref (the old shape) upserts
     via the store fallback - old callers and half-migrated rigs keep working.
  3. NEITHER IS A LOUD FAILURE. A chunk with no embedding and no ref refuses at upsert
     rather than indexing something unsearchable.

    PYTHONPATH=src python3 tests/selftest_834_no_emb_blobs.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.adapters.local import InMemoryIndex, InMemoryObjectStore  # noqa: E402
from dbsearch.core.models import Chunk  # noqa: E402
from dbsearch.ports.base import ReadScope  # noqa: E402
from dbsearch.server.edition import build_edition  # noqa: E402

ALICE = "oid-alice"


def _ingest(ed, doc_id, text):
    return ed.ingest_file(external_id=doc_id, title=doc_id, data=text.encode(),
                          mime="text/plain", acl=["all-staff"],
                          uri=f"upload://{doc_id}.txt", owner_oid=ALICE)


def test_a_full_ingest_writes_no_emb_blobs_and_still_answers():
    ed = build_edition()
    _ingest(ed, "u-notes", "the wifi password for the office is on the kitchen whiteboard")
    emb_keys = [k for k in ed.store._blobs if k.startswith("emb/")]
    assert emb_keys == [], f"emb/ blobs are still being written: {emb_keys}"
    # and the vector genuinely arrived: retrieval scores it
    hits = ed.index.search(ed.embedder.embed(["wifi password"])[0], ["all-staff"], 5,
                           ReadScope(partition=ed.tenant_id))
    assert hits and hits[0]["doc_external_id"] == "u-notes", hits


def test_chunk_blobs_still_exist_they_are_read_at_query_time():
    """The boundary that must not creep: chunk/ text blobs are read by query/service at
    answer time, so removing emb/ must not take chunk/ with it."""
    ed = build_edition()
    _ingest(ed, "u-keep", "chunk text blobs must survive")
    chunk_keys = [k for k in ed.store._blobs if k.startswith("chunk/")]
    assert chunk_keys, "chunk/ text blobs are gone - answers would lose their text"


def test_a_legacy_chunk_with_only_a_ref_still_indexes():
    store = InMemoryObjectStore()
    ref = store.put("emb/t1/legacy-doc/0", json.dumps([1.0, 0.0, 0.0]).encode())
    idx = InMemoryIndex(store)
    idx.upsert([Chunk(tenant_id="t1", doc_external_id="legacy-doc", chunk_id="legacy-doc#0",
                      text_ref="chunk/t1/legacy-doc/0", allowed_principals=["all-staff"],
                      embedding_ref=ref)])
    hits = idx.search([1.0, 0.0, 0.0], ["all-staff"], 5, ReadScope(partition="t1"))
    assert hits and hits[0]["doc_external_id"] == "legacy-doc", hits


def test_a_chunk_with_neither_vector_nor_ref_refuses_loudly():
    idx = InMemoryIndex(InMemoryObjectStore())
    naked = Chunk(tenant_id="t1", doc_external_id="d", chunk_id="d#0",
                  text_ref="chunk/t1/d/0", allowed_principals=["all-staff"])
    try:
        idx.upsert([naked])
        raise AssertionError("a chunk with no embedding and no ref must not index silently")
    except (ValueError, KeyError):
        pass


def test_the_vector_survives_the_queue_serialization():
    """The chunk crosses the pipeline queue as a dict: the transient field must round-trip
    to_dict -> from_dict, or the in-message design silently degrades to the fallback."""
    c = Chunk(tenant_id="t1", doc_external_id="d", chunk_id="d#0", text_ref="x",
              allowed_principals=[], embedding=[0.25, 0.5])
    assert Chunk.from_dict(c.to_dict()).embedding == [0.25, 0.5]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
