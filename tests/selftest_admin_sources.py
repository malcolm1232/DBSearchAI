"""Unit tests for the Phase 2b Sources slice: run_ingestion result (Task 1),
SourceRegistry (Task 2), and Edition.resync_source / AdminService.sources (Task 3).
In-process, no HTTP. Run: python3 tests/selftest_admin_sources.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIndex, InMemoryObjectStore, InMemoryQueue, PlainTextExtractor,
)
from dbsearch.connectors.sharepoint import SharePointConnector  # noqa: E402
from dbsearch.pipeline.runner import IngestResult, run_ingestion  # noqa: E402
from dbsearch.connectors.registry import (  # noqa: E402
    SourceDescriptor, SourceRegistry, SourceSummary,
)

TENANT = "selfhost"


def test_run_ingestion_result():
    store = InMemoryObjectStore()
    index = InMemoryIndex(store)
    embedder = HashingEmbedding()
    conn = SharePointConnector(tenant_id=TENANT)            # default 3-doc seed
    res = run_ingestion(conn, InMemoryQueue(), store, PlainTextExtractor(), embedder, index)
    assert isinstance(res, IngestResult), res
    assert res.doc_count == 3, res                          # 3 seed docs published to parse
    assert res.cursor is None, res                          # seed list_changes returns no cursor
    assert index.stats(TENANT).doc_count == 3, index.stats(TENANT)
    # passing a cursor must not break the in-memory path (seed ignores it)
    res2 = run_ingestion(conn, InMemoryQueue(), store, PlainTextExtractor(), embedder, index, cursor="x")
    assert res2.doc_count == 3, res2
    assert res2.cursor is None, res2          # seed connector ignores the passed cursor
    print("  PASS  run_ingestion returns IngestResult(doc_count, cursor)")


def test_registry():
    reg = SourceRegistry()
    reg.register(SourceDescriptor(
        source_id="sharepoint", kind="sharepoint",
        display_name="SharePoint — Contoso",
        connector=SharePointConnector(tenant_id=TENANT)))

    lst = reg.list_sources()
    assert len(lst) == 1 and isinstance(lst[0], SourceSummary), lst
    assert lst[0].source_id == "sharepoint" and lst[0].kind == "sharepoint", lst[0]
    assert lst[0].last_sync_at is None and lst[0].doc_count == 0 and lst[0].status == "idle", lst[0]

    s = reg.record_sync("sharepoint", cursor="c1", doc_count=3, at="2026-06-26T00:00:00+00:00")
    assert s.doc_count == 3 and s.status == "idle", s
    assert s.last_sync_at == "2026-06-26T00:00:00+00:00", s
    assert reg.get("sharepoint").cursor == "c1", reg.get("sharepoint")

    reg.record_error("sharepoint")
    assert reg.get("sharepoint").status == "error", reg.get("sharepoint")

    try:
        reg.get("nope")
        assert False, "expected KeyError for unknown source"
    except KeyError:
        pass
    print("  PASS  registry: list / record_sync / record_error / unknown raises")


def test_resync_via_edition():
    os.environ["SELFHOST_BACKEND"] = "memory"
    from dbsearch.server.edition import build_edition

    ed = build_edition()
    before = ed.admin_service.sources()
    assert len(before) == 1, before
    assert before[0].source_id == "sharepoint", before[0]
    assert before[0].status == "idle" and before[0].last_sync_at is None, before[0]
    assert before[0].doc_count == 0, before[0]

    # #569: resync_source SUBMITS and returns a handle; the blocking twin is what a
    # test/CLI wants. Same worker and same job record, so this is not a second code path.
    s = ed.resync_source_blocking("sharepoint")
    assert s.doc_count == 3 and s.status == "idle", s
    assert s.last_sync_at is not None, s
    assert ed.index.stats(ed.tenant_id).doc_count == 3, ed.index.stats(ed.tenant_id)

    assert any(p.get("event") == "source.synced" for p in ed.control_plane.audit_log), \
        ed.control_plane.audit_log

    # idempotent: a second resync replaces, never duplicates
    s2 = ed.resync_source_blocking("sharepoint")
    assert s2.doc_count == 3, s2
    assert ed.index.stats(ed.tenant_id).doc_count == 3, ed.index.stats(ed.tenant_id)

    try:
        ed.resync_source("nope")
        assert False, "expected KeyError for unknown source"
    except KeyError:
        pass
    print("  PASS  edition.resync_source: crawl / telemetry / idempotent / unknown raises")


def main():
    print("Phase 2b Sources unit self-test:")
    test_run_ingestion_result()
    test_registry()
    test_resync_via_edition()
    print("\nPHASE 2B SOURCES UNIT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
