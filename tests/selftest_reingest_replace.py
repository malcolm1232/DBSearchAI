"""Self-test: re-ingesting a document REPLACES its chunk set, no orphans — finding B of
the Phase B fix wave.

Before the fix, `pipeline/runner.py` was upsert-only: chunk ids are `{external_id}#{n}`,
so re-ingesting a doc that now yields FEWER chunks left the surplus higher-`n` chunks
behind, still carrying the OLD content and OLD (possibly looser) ACL — a LAW 2 staleness
hole. This test ingests a doc with 3 chunks, re-ingests the SAME external_id with 1 chunk
and a DIFFERENT acl, and proves: (1) the chunk count drops to the new count (no orphans),
and (2) a user holding only the OLD acl can no longer retrieve any of the doc's chunks.

    python3 tests/selftest_reingest_replace.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.ports.base import ReadScope  # noqa: E402

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIndex, InMemoryObjectStore, InMemoryQueue, LocalRichExtractor,
)
from dbsearch.connectors.upload import UploadConnector  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402

TENANT = "t"
OLD_ACL = ["deal-team"]
NEW_ACL = ["all-staff"]


def _ingest(index, store, csv_bytes, acl):
    conn = UploadConnector(TENANT, "roster", "Roster", csv_bytes, "text/csv", acl, "")
    run_ingestion(conn, InMemoryQueue(), store, LocalRichExtractor(), HashingEmbedding(), index)


def test_reingest_replaces_stale_chunks_no_orphans():
    store = InMemoryObjectStore()
    index = InMemoryIndex(store)

    # first ingest: 3 rows -> 3 chunks, deal-team only
    _ingest(index, store, b"name,role\nAlice,Partner\nBob,Analyst\nCarol,VP\n", OLD_ACL)
    assert index.chunk_count_for(TENANT, "roster") == 3

    # re-ingest SAME external_id: fewer rows (1) + a DIFFERENT acl
    _ingest(index, store, b"name,role\nAlice,Partner\n", NEW_ACL)

    # no orphans: chunk count must reflect ONLY the fresh, smaller set
    assert index.chunk_count_for(TENANT, "roster") == 1, index.chunk_count_for(TENANT, "roster")

    # a user holding only the OLD acl can no longer retrieve ANY of this doc's chunks
    segs = index.list_doc_segments(ReadScope(TENANT), "roster", principals=OLD_ACL)
    assert segs == [], segs

    # the NEW acl retrieves the (single, fresh) chunk
    segs = index.list_doc_segments(ReadScope(TENANT), "roster", principals=NEW_ACL)
    assert len(segs) == 1, segs


if __name__ == "__main__":
    test_reingest_replaces_stale_chunks_no_orphans()
    print("PASS")
