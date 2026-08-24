"""#910 - an incremental resync must never write its per-crawl count over the corpus size.

THE DEFECT, measured on prod 260821: a fresh connect crawled 5 SharePoint docs
(doc_count=5, durable row 5), then POST /admin/resync ran an INCREMENTAL crawl off the
persisted deltaLink, correctly found nothing changed (docs_done=0) - and the registry
then reported doc_count=0, durably. The canvas node rendered "0 docs" over an intact
corpus. `runner.py` builds `doc_count` as a PER-CRAWL counter; `record_sync` wrote it
into the field the UI renders as CORPUS SIZE. Full crawl: the two coincide. Incremental:
they do not. #883 did not create this - it removed the accident (state dying with the
process) that was hiding it.

THE RULE (owner-ratified 260821, one home: `SourceRegistry.record_sync`): doc_count means
corpus size. A full crawl sets it absolutely; an incremental crawl carries the previous
count forward adjusted by its net change (created minus deleted), which the runner now
measures against the index. Deletion tombstones from the delta feed are processed - chunks
removed at once (LAW 2 freshness) and counted - and an out-of-scope tombstone for a
document this index never held changes nothing.

One test per clause, so each regressing alone goes red:
  - quiet resync leaves the count alone (the exact prod defect);
  - an incremental that adds counts up;
  - a tombstone counts down AND its chunks stop serving (both halves asserted);
  - a FULL crawl of an emptied folder still reaches zero (fails the never-overwrite
    wrong fix);
  - an out-of-scope tombstone is a no-op;
  - the Graph connector keeps tombstones in the delta and reports them via deletions().
"""
from __future__ import annotations

import sys
import types

from dbsearch.adapters.local import (
    HashingEmbedding, InMemoryIndex, InMemoryObjectStore, InMemoryQueue, LocalRichExtractor,
)
from dbsearch.connectors.registry import SourceDescriptor, SourceRegistry
from dbsearch.pipeline.runner import run_ingestion
from dbsearch.ports.base import ConnectorPort, Document, Principal, ReadScope

TENANT = "t910"
ACL = ["all-staff"]


class _DeltaConnector(ConnectorPort):
    """A scripted delta source: each crawl pops the next (items, cursor) batch."""

    tenant_id = TENANT

    def __init__(self, batches: list) -> None:
        self._batches = list(batches)

    def authenticate(self, config: dict) -> object:
        return None

    def list_changes(self, cursor):
        items, next_cursor = self._batches.pop(0)
        return items, next_cursor

    def fetch_content(self, item):
        return item["body"].encode(), "text/plain"

    def fetch_acl(self, item):
        return [Principal(oid=ACL[0], kind="group")]

    def to_documents(self, item):
        return [Document(tenant_id=TENANT, source_id="delta:src", external_id=item["id"],
                         content_ref="", acl=self.fetch_acl(item), title=item["id"],
                         uri="", content_hash=item["body"], source_meta={},
                         owner_oid="owner")]

    def external_ids(self, item):
        return [item["id"]]

    def deletions(self, item):
        return [item["id"]] if item.get("deleted") else []


def _doc(i: str, body: str) -> dict:
    return {"id": i, "body": body}


def _rig(batches):
    store = InMemoryObjectStore()
    index = InMemoryIndex(store)
    conn = _DeltaConnector(batches)
    reg = SourceRegistry()
    reg.register(SourceDescriptor(source_id="delta:src", kind="delta", display_name="d",
                                  connector=conn))
    return store, index, conn, reg


def _crawl(store, index, conn, reg, cursor):
    """One crawl + the same record_sync call both rails' _commit now makes."""
    result = run_ingestion(conn, InMemoryQueue(), store, LocalRichExtractor(),
                           HashingEmbedding(), index, cursor=cursor)
    reg.record_sync("delta:src", cursor=result.cursor, doc_count=result.doc_count,
                    at="2026-08-21T00:00:00Z", unreadable=result.unreadable,
                    full_crawl=result.full_crawl, created=result.created,
                    deleted=result.deleted)
    return result


def test_quiet_incremental_resync_keeps_the_corpus_count():
    """The exact prod defect: full crawl of 3, then a delta that finds nothing."""
    store, index, conn, reg = _rig([
        ([_doc("a", "alpha text"), _doc("b", "beta text"), _doc("c", "gamma text")], "cur1"),
        ([], "cur2"),
    ])
    _crawl(store, index, conn, reg, cursor=None)
    assert reg.get("delta:src").doc_count == 3
    _crawl(store, index, conn, reg, cursor="cur1")
    assert reg.get("delta:src").doc_count == 3, (
        "a quiet incremental resync wrote its per-crawl 0 over the corpus size (#910)")
    assert reg.get("delta:src").cursor == "cur2", "the delta cursor must still advance"


def test_incremental_addition_counts_up_and_update_does_not():
    store, index, conn, reg = _rig([
        ([_doc("a", "alpha text"), _doc("b", "beta text")], "cur1"),
        # one net-new document plus an UPDATE to an existing one: growth is exactly 1
        ([_doc("c", "gamma text"), _doc("a", "alpha rewritten")], "cur2"),
    ])
    _crawl(store, index, conn, reg, cursor=None)
    _crawl(store, index, conn, reg, cursor="cur1")
    assert reg.get("delta:src").doc_count == 3, (
        "net growth must be adds only - counting an update as growth inflates the corpus")


def test_tombstone_counts_down_and_stops_serving():
    store, index, conn, reg = _rig([
        ([_doc("a", "alpha text"), _doc("b", "beta text")], "cur1"),
        ([{"id": "b", "deleted": {"state": "deleted"}}], "cur2"),
    ])
    _crawl(store, index, conn, reg, cursor=None)
    assert index.chunk_count_for(TENANT, "b") > 0
    _crawl(store, index, conn, reg, cursor="cur1")
    assert reg.get("delta:src").doc_count == 1, "a source-side delete must count down"
    assert index.chunk_count_for(TENANT, "b") == 0, (
        "the deleted document's chunks kept serving - stale content under a stale ACL "
        "(LAW 2 freshness)")


def test_full_crawl_of_an_emptied_folder_reaches_zero():
    """The control that fails the never-overwrite-on-zero WRONG fix."""
    store, index, conn, reg = _rig([
        ([_doc("a", "alpha text")], "cur1"),
        ([], None),      # a FULL re-crawl (cursor=None) that finds a genuinely empty folder
    ])
    _crawl(store, index, conn, reg, cursor=None)
    assert reg.get("delta:src").doc_count == 1
    _crawl(store, index, conn, reg, cursor=None)
    assert reg.get("delta:src").doc_count == 0, (
        "a full crawl listed everything - an empty listing means an empty corpus, and "
        "refusing to write zero would make an emptied folder lie forever")


def test_out_of_scope_tombstone_is_a_noop():
    store, index, conn, reg = _rig([
        ([_doc("a", "alpha text")], "cur1"),
        ([{"id": "never-held", "deleted": {"state": "deleted"}}], "cur2"),
    ])
    _crawl(store, index, conn, reg, cursor=None)
    _crawl(store, index, conn, reg, cursor="cur1")
    assert reg.get("delta:src").doc_count == 1, (
        "a tombstone for a document this index never held must not shrink the corpus")


def test_graph_connector_keeps_and_reports_tombstones():
    """The delta feed's tombstones survive the file/scope filter and come out of
    deletions() - with the network stubbed at the requests seam."""
    from dbsearch.connectors.sharepoint_graph import GraphSharePointConnector

    delta_body = {"value": [
        {"id": "live1", "name": "a.pdf", "file": {"mimeType": "application/pdf"},
         "parentReference": {"path": "/drives/d/root:/mal_hr_docs"}},
        {"id": "gone1", "deleted": {"state": "deleted"}},          # no file, no parentReference
        {"id": "elsewhere", "name": "b.pdf", "file": {"mimeType": "application/pdf"},
         "parentReference": {"path": "/drives/d/root:/other"}},
    ], "@odata.deltaLink": "next"}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return delta_body

    fake_requests = types.SimpleNamespace(get=lambda *a, **k: _Resp())
    conn = GraphSharePointConnector.__new__(GraphSharePointConnector)
    conn._drive_id = "d"
    conn._folder = "mal_hr_docs"
    conn._name_contains = ""
    conn._headers = lambda: {}
    real = sys.modules.get("requests")
    sys.modules["requests"] = fake_requests
    try:
        items, cursor = conn.list_changes("some-delta-link")
    finally:
        if real is not None:
            sys.modules["requests"] = real
        else:
            del sys.modules["requests"]
    ids = [i["id"] for i in items]
    assert ids == ["live1", "gone1"], (
        f"expected the in-scope file plus the tombstone, got {ids} - a dropped tombstone "
        "means deletes never reach the runner")
    assert cursor == "next"
    assert conn.deletions(items[0]) == []
    assert conn.deletions(items[1]) == ["gone1"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("PASS", name)
