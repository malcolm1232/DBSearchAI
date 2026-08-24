"""#569 - the EDITION ingest rail still crawls inside the HTTP request.

#454 / ADR 0016 moved the ROUTER rail off the request: `/router/stores/{id}/sync` submits a
job and returns 202 with a handle. The FLAGSHIP path did not move with it.
`POST /connectors/sharepoint/finish` -> `edition.connect_sharepoint` ->
`run_ingestion(...)` runs synchronously, with no `checkpoint=` argument at all, and so does
`edition.resync_source`.

Two consequences, and this file pins both BEFORE the fix so the failure is a measurement
rather than an assertion of intent:

  1. LAW 4 - the request holds the whole crawl. #536 measured 40.2MB / 4884 docs exceeding a
     3600s timeout on the shared pipeline, so this is not theoretical on a real library.
  2. ADR 0016 - no job record means a crash re-crawls from nothing. #455's per-document
     checkpoint never pays off on the connector the primary objective is built around.

THE TRAP this fix must not walk into, recorded on #454 and repeated here because it is the
one failure that is silent: `ConnectorPort.list_changes` advances its cursor PER BATCH, not
per item. Persisting `next_cursor` early - the intuitive way to make a crawl resumable -
SKIPS every unrecorded item of the in-flight batch. The contract is: cursor pinned until the
batch completes, per-DOCUMENT checkpoint within it.

    PYTHONPATH=src python3 tests/selftest_569_edition_ingest_off_the_request.py
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("SELFHOST_BACKEND", "memory")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.core.models import Document, Principal  # noqa: E402
from dbsearch.ports.base import ConnectorPort  # noqa: E402
from dbsearch.server.app import _edition  # noqa: E402

TENANT = "tid-569"


class SlowConnector(ConnectorPort):
    """A library that takes a measurable time to crawl, so "did the request wait for it?" is
    a question about the clock rather than about intent."""

    def __init__(self, docs: int = 6, per_doc: float = 0.05, tenant_id: str = TENANT) -> None:
        self.docs = docs
        self.per_doc = per_doc
        self.tenant_id = tenant_id
        self.fetched: list[str] = []          # every doc this crawl actually paid for

    def authenticate(self, config: dict) -> object:
        return object()

    BATCH_CURSOR = "cursor-after-batch-1"

    def list_changes(self, cursor):
        """ONE batch, deliberately: the cursor advances per batch, so a single batch is the
        shape in which "resume skipped the rest of the batch" would be silent.

        The cursor is HONOURED, like a real incremental connector: pass the post-batch cursor
        and there is nothing new to list. A fixture that ignored it would let a fresh connect
        pass any cursor at all with no visible effect - and "a fresh connect must crawl from
        the beginning" is precisely a property worth being unable to break silently."""
        if cursor is not None:
            return [], cursor
        items = [{"id": f"doc-569-{i}"} for i in range(self.docs)]
        return items, self.BATCH_CURSOR

    def fetch_content(self, item):
        time.sleep(self.per_doc)
        self.fetched.append(item["id"])
        return (f"Document {item['id']} covers the Bremen actuator line.".encode(), "text/plain")

    def fetch_acl(self, item):
        return [Principal(oid="alice", kind="user")]

    def to_documents(self, item):
        return [Document(tenant_id=self.tenant_id, source_id="sharepoint",
                         external_id=item["id"], content_ref="",
                         acl=self.fetch_acl(item), title=item["id"],
                         uri=f"sp://{item['id']}")]

    def external_ids(self, item):
        return [item["id"]]


def test_connect_sharepoint_returns_before_the_crawl_finishes():
    """LAW 4. The call must hand back a job handle promptly, not the finished result.

    Pre-fix this returns only after every document is crawled, so the elapsed time is the
    whole crawl and the assertion below fails by the clock.
    """
    conn = SlowConnector(docs=6, per_doc=0.05)          # ~0.3s of crawl
    started = time.monotonic()
    out = _edition.connect_sharepoint("tenant-569-a", "drive-1", connector=conn,
                                      tenant_id=TENANT, owner_oid="alice")
    elapsed = time.monotonic() - started
    assert elapsed < 0.15, (
        f"connect_sharepoint held the caller for {elapsed:.2f}s - the crawl ran inside the "
        "request (LAW 4). It must submit and return.")
    assert out.get("job_id"), f"no job handle returned: {out}"


def test_a_fresh_connect_crawls_from_the_beginning():
    """A newly consented drive has no history to resume from, so the crawl must start with
    NO cursor. Passing one would list only "changes since", and a freshly connected library
    would come up empty with nothing reporting a fault."""
    conn = SlowConnector(docs=3, per_doc=0.01)
    out = _edition.connect_sharepoint("tenant-569-f", "drive-6", connector=conn,
                                      tenant_id=TENANT, owner_oid="alice")
    job = _edition.await_ingest(out["job_id"], timeout=20)
    assert job.status == "succeeded", f"{job.status} {job.error}"
    assert job.docs_done == 3, f"a fresh connect indexed {job.docs_done}/3 - it did not crawl from the start"


def test_connect_sharepoint_records_a_durable_job():
    """ADR 0016. Without a job record there is nothing to resume from, and nothing for the
    canvas to poll except a request that has not come back."""
    conn = SlowConnector(docs=4, per_doc=0.01)
    out = _edition.connect_sharepoint("tenant-569-b", "drive-2", connector=conn,
                                      tenant_id=TENANT, owner_oid="alice")
    job = _edition.ingest_job(out["job_id"])
    assert job is not None, "connect_sharepoint left no job record"
    final = _edition.await_ingest(out["job_id"], timeout=20)
    assert final.status == "succeeded", f"job did not succeed: {final.status} {final.error}"
    assert final.docs_done == 4, final


class _FailsPartWay(SlowConnector):
    """Dies mid-batch, after banking some documents - the shape #455 exists for.

    `once=True` makes the failure TRANSIENT: the retry runs against the same connector
    instance, which is what actually happens, since `resync_source` re-uses the connector
    recorded on the descriptor rather than taking a fresh one."""

    def __init__(self, docs: int = 5, explode_at: int = 3, once: bool = False) -> None:
        super().__init__(docs=docs, per_doc=0.01)
        self.explode_at = explode_at
        self.armed = True
        self.once = once

    def fetch_content(self, item):
        if self.armed and int(item["id"].rsplit("-", 1)[1]) == self.explode_at:
            if self.once:
                self.armed = False
            raise RuntimeError("connection reset mid-crawl")
        return super().fetch_content(item)


def test_the_checkpoint_banks_documents_as_they_are_indexed():
    """#569's actual deliverable on the resume half: the crawl now runs THROUGH a
    JobCheckpoint, so a death mid-batch leaves the finished documents recorded instead of
    leaving nothing at all. Pre-fix `run_ingestion` was called with no `checkpoint=` argument,
    so there was nothing to bank into and nothing to resume from."""
    conn = _FailsPartWay()
    out = _edition.connect_sharepoint("tenant-569-c", "drive-3", connector=conn,
                                      tenant_id=TENANT, owner_oid="alice")
    job = _edition.await_ingest(out["job_id"], timeout=20)
    assert job.status == "failed", f"expected a failed job, got {job.status}"
    banked = _edition.ingest_jobs.completed_documents(out["job_id"])
    assert banked == {"doc-569-0", "doc-569-1", "doc-569-2"}, (
        f"the checkpoint banked {sorted(banked)} - the crawl is not running through one")
    assert job.docs_done == 3, job


def test_a_failed_crawl_is_picked_back_up___577_LANDED():
    """FLIPPED BY #577, exactly as the previous version of this test instructed.

    #569 wired the checkpoint so a death mid-batch banks its finished documents. #577 made
    `resumable()` exclude only SUCCEEDED rather than every terminal state, so those banked
    documents are now actually picked up: the retry reuses the SAME job and skips them.

    The full rule and its measurements live in tests/selftest_577_failed_job_resumes.py;
    this keeps the edition rail honest about it too.
    """
    conn = _FailsPartWay(once=True)
    out = _edition.connect_sharepoint("tenant-569-e", "drive-5", connector=conn,
                                      tenant_id=TENANT, owner_oid="alice")
    _edition.await_ingest(out["job_id"], timeout=20)
    banked = set(conn.fetched)
    assert banked == {"doc-569-0", "doc-569-1", "doc-569-2"}, sorted(banked)

    conn.fetched.clear()                      # measure ONLY what the retry pays for
    out2 = _edition.resync_source(out["source_id"])
    final = _edition.await_ingest(out2["job_id"], timeout=20)
    assert final.status == "succeeded", f"the retry did not succeed: {final.error}"

    assert out2["job_id"] == out["job_id"], "the retry forked a new job instead of resuming"
    assert not (banked & set(conn.fetched)), "the resume re-fetched banked documents"
    covered = banked | set(conn.fetched)
    assert covered == {f"doc-569-{i}" for i in range(5)}, (
        f"documents went missing across the resume - silent loss: {sorted(covered)}")


def test_resync_source_also_submits_rather_than_crawling_inline():
    conn = SlowConnector(docs=6, per_doc=0.05)
    out = _edition.connect_sharepoint("tenant-569-d", "drive-4", connector=conn,
                                      tenant_id=TENANT, owner_oid="alice")
    _edition.await_ingest(out["job_id"], timeout=20)

    started = time.monotonic()
    handle = _edition.resync_source(out["source_id"])
    elapsed = time.monotonic() - started
    assert elapsed < 0.15, (
        f"resync_source held the caller for {elapsed:.2f}s - it still crawls inside the request")
    assert getattr(handle, "job_id", None) or (isinstance(handle, dict) and handle.get("job_id")), \
        f"resync_source returned no job handle: {handle!r}"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
            except Exception as e:                      # a missing seam is a failure, not a crash
                print(f"  FAIL  {name}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
