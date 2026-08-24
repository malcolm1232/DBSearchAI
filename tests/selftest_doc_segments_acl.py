"""Self-test: /admin/documents/{id}/segments preview is ACL-trimmed to the CALLER's
principals (LAW 2) — finding A of the Phase B fix wave.

Before the fix, `IndexPort.list_doc_segments(tenant_id, doc_external_id)` had no
`principals` parameter at all: ANY authenticated caller could read a 200-char content
preview of ANY tenant document's chunks, bypassing the permission trim that `search()`
enforces. This test builds two docs (a deal-team-only doc and an all-staff doc) and
proves a caller only sees segments of documents visible to them — same rule as search():
`set(chunk.allowed_principals) & set(caller_principals)` must be non-empty.

    python3 tests/selftest_doc_segments_acl.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.ports.base import ReadScope  # noqa: E402

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIdentity, InMemoryIndex, InMemoryObjectStore, InMemoryQueue,
    LocalRichExtractor,
)
from dbsearch.connectors.upload import UploadConnector  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402

TENANT = "t"


def _ingest(index, store, external_id, title, csv_bytes, acl):
    conn = UploadConnector(TENANT, external_id, title, csv_bytes, "text/csv", acl, "")
    run_ingestion(conn, InMemoryQueue(), store, LocalRichExtractor(), HashingEmbedding(), index)


def test_doc_segments_trimmed_to_caller_principals():
    store = InMemoryObjectStore()
    index = InMemoryIndex(store)
    _ingest(index, store, "falcon", "Falcon Deal",
            b"name,role\nAlice,Partner\nBob,Analyst\n", ["deal-team"])
    _ingest(index, store, "handbook", "Handbook",
            b"name,role\nCarol,HR\nDan,IT\n", ["all-staff"])

    identity = InMemoryIdentity({"alice": ["deal-team"], "bob": ["all-staff"]})
    alice_principals = identity.expand_groups("alice")
    bob_principals = identity.expand_groups("bob")

    # alice (deal-team) sees the deal-team-only doc's preview
    segs = index.list_doc_segments(ReadScope(TENANT), "falcon", principals=alice_principals)
    assert len(segs) == 2, segs

    # bob (all-staff only) must NOT see the deal-team-only doc's preview (LAW 2)
    segs = index.list_doc_segments(ReadScope(TENANT), "falcon", principals=bob_principals)
    assert segs == [], segs

    # bob CAN see the all-staff doc's preview
    segs = index.list_doc_segments(ReadScope(TENANT), "handbook", principals=bob_principals)
    assert len(segs) == 2, segs


if __name__ == "__main__":
    test_doc_segments_trimmed_to_caller_principals()
    print("PASS")
