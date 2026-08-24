"""#40 — FolderConnector: ACL-aware PULL ingestion of a local directory.

Proves: the subfolder-as-group ACL convention flows through to permission-trimmed retrieval
(LAW 2 — bob can't see a deal-team file), incremental crawl is resumable, and files with no
group are default-denied (never indexed).

    python3 tests/selftest_folder_connector.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIdentity, InMemoryIndex, InMemoryObjectStore,
    InMemoryQueue, PlainTextExtractor,
)
from dbsearch.connectors.folder import FolderConnector  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.query import QueryService  # noqa: E402

FALCON_TXT = "confidential falcon merger acquisition target valuation deal team only"
HANDBOOK_TXT = "general staff handbook holidays expenses onboarding for all staff"


def build_corpus(root: Path):
    (root / "deal-team").mkdir()
    (root / "all-staff").mkdir()
    (root / "deal-team" / "falcon.txt").write_text(FALCON_TXT)
    (root / "all-staff" / "handbook.txt").write_text(HANDBOOK_TXT)
    (root / "loose.txt").write_text("a file with no group — must be default-denied")  # root-level


def main():
    print("FolderConnector self-test (ACL-aware PULL ingestion, LAW 2 trim):")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        build_corpus(root)

        store, queue = InMemoryObjectStore(), InMemoryQueue()
        index = InMemoryIndex(store)
        embedder = HashingEmbedding()
        conn = FolderConnector("acme", str(root))

        # 1. ACL convention: subdir = group; root-level file gets none (default-deny).
        items, cursor = conn.list_changes(None)
        ext_ids = {i["external_id"]: i["acl"] for i in items}
        assert ext_ids == {"deal-team/falcon.txt": ["deal-team"],
                           "all-staff/handbook.txt": ["all-staff"]}, ext_ids
        assert "loose.txt" not in ext_ids, "root-level file with no group must be skipped (default-deny)"
        print(f"  PASS  ACL-aware crawl  ->  {ext_ids}  (loose.txt default-denied)")

        # 2. Ingest end-to-end, then permission-trimmed retrieval.
        run_ingestion(conn, queue, store, PlainTextExtractor(), embedder, index)
        identity = InMemoryIdentity({"alice": ["all-staff", "deal-team"], "bob": ["all-staff"]})
        qs = QueryService(index, identity, embedder, _Noop(), store, tenant_id="acme")

        fa = [c.doc_external_id for c in qs.retrieve("alice", "confidential falcon merger valuation")]
        fb = [c.doc_external_id for c in qs.retrieve("bob", "confidential falcon merger valuation")]
        assert "deal-team/falcon.txt" in fa, "alice (deal-team) must retrieve the falcon file"
        assert "deal-team/falcon.txt" not in fb, "LAW 2 BREACH: bob retrieved a deal-team file"
        # positive control: both retrieve the all-staff handbook (recall != 0)
        hb = [c.doc_external_id for c in qs.retrieve("bob", "staff handbook holidays expenses")]
        assert "all-staff/handbook.txt" in hb, "bob should retrieve the all-staff handbook (positive control)"
        print(f"  PASS  LAW-2 trim  ->  alice falcon={fa}  bob falcon={fb}  bob handbook ok")

        # 3. Incremental crawl is resumable: re-crawling with the cursor yields nothing new.
        items2, _ = conn.list_changes(cursor)
        assert items2 == [], f"incremental crawl should be empty, got {items2}"
        print("  PASS  incremental crawl with cursor returns no new items (resumable)")

    test_edition_wiring()
    print("\nALL FOLDER-CONNECTOR TESTS PASSED — ACL-aware PULL ingestion is permission-faithful.")


def test_edition_wiring():
    """FOLDER_SOURCE_DIR registers a 'folder' source; resync_source crawls it through the
    full pipeline and the result is permission-trimmed (LAW 2)."""
    import importlib
    import os

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        build_corpus(root)
        os.environ["SELFHOST_BACKEND"] = "memory"
        os.environ["FOLDER_SOURCE_DIR"] = str(root)
        import dbsearch.server.edition as ed
        importlib.reload(ed)
        e = ed.build_edition()
        ids = {s.source_id for s in e.admin_service.sources()}
        assert "folder" in ids, f"folder source not registered: {ids}"
        # #569: resync_source now SUBMITS. This test wants the finished summary, which is
        # what resync_source_blocking is for - same worker, same job record, plus a wait.
        summary = e.resync_source_blocking("folder")
        assert summary.doc_count == 2, f"expected 2 docs ingested, got {summary.doc_count}"
        a = e.query_service.answer("alice", "confidential falcon merger valuation").retrieved_docs
        b = e.query_service.answer("bob", "confidential falcon merger valuation").retrieved_docs
        assert "deal-team/falcon.txt" in a and "deal-team/falcon.txt" not in b, (a, b)
        os.environ.pop("FOLDER_SOURCE_DIR", None)
    print("  PASS  FOLDER_SOURCE_DIR wires a 'folder' source; resync ingests 2 docs, trim holds")


class _Noop:
    def answer(self, question, context_chunks):
        return {"answer": "", "citations": []}


if __name__ == "__main__":
    main()
