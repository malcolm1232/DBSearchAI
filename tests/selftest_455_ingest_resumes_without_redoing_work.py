"""#455 — a crash mid-crawl must not throw away the work that already succeeded.

THE DEFECT, as measured (#536) and as the runner's own comment already admits: ingest is
staged over the WHOLE batch — every document is fetched, then every document is parsed,
then every document is embedded, then every document is indexed. Embedding is the entire
cost (66-108ms/chunk on the 2-vCPU prod box), and it is the stage in the middle. So a crawl
that dies at 90% has:

  * indexed NOTHING (the index stage never ran), and
  * recorded nothing, so the next run re-embeds all 90% from scratch.

On a 4MB fixture that is a rounding error. On a real SharePoint library it is the difference
between a job that finishes and one that never can: 40.2MB already exceeds an hour (#536),
and the probability of an uninterrupted run falls as the corpus grows.

WHAT THIS TEST PINS (ADR 0016's contract, the parts that must not be renegotiated):

  1. Per-document checkpoint. A document that finished before the crash is INDEXED and
     queryable, not lost with the run.
  2. Resume does not redo completed work. The second run must not re-embed a document the
     first run already indexed.
  3. Pinned cursor. The resume lists changes with the SAME cursor as the failed run. This is
     the counterintuitive half: advancing the cursor early would silently skip every
     unprocessed item in the batch, and a store that answers as though those documents do
     not exist is far worse than a slow re-crawl.
  4. No document is lost. All six documents are indexed once the resume completes.
  5. The cursor advances only when the whole batch is done.

    PYTHONPATH=src python3 tests/selftest_455_ingest_resumes_without_redoing_work.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.ports.base import ReadScope  # noqa: E402

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIndex, InMemoryObjectStore, InMemoryQueue, LocalRichExtractor,
)
from dbsearch.core.models import Document, Principal  # noqa: E402
from dbsearch.pipeline.jobs import InMemoryJobStore, JobCheckpoint  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.ports.base import ConnectorPort  # noqa: E402

TENANT = "t-455"
SOURCE = "library-455"
DOC_IDS = [f"doc-{i}.txt" for i in range(1, 7)]
BOOM = "doc-4.txt"          # the document the first run dies on

# The cursor the source hands back once the WHOLE batch is done. It must never be persisted
# by a run that did not finish, and the resume must not be listed with it.
NEXT_CURSOR = "delta-token-AFTER-the-batch"


class SixDocConnector(ConnectorPort):
    """Six small documents in ONE batch, which is the shape that makes the cursor dangerous:
    `list_changes` advances per batch, not per item, so a batch that is 5/6 done has no
    cursor that can express that."""

    def __init__(self) -> None:
        self.listed_with: list[str | None] = []      # every cursor list_changes was called with
        self.fetched: list[str] = []                 # every document whose bytes were fetched

    def authenticate(self, config: dict) -> object:
        return object()

    def list_changes(self, cursor):
        self.listed_with.append(cursor)
        items = [{"external_id": d, "title": d} for d in DOC_IDS]
        return items, NEXT_CURSOR

    def fetch_content(self, item: dict):
        self.fetched.append(item["external_id"])
        # Body carries its own id so an embed call can be attributed to a document.
        body = (f"This is {item['external_id']}. "
                "The probation period is six months and laptops are issued by the IT desk. ")
        return (body * 3).encode(), "text/plain"

    def fetch_acl(self, item: dict):
        return [Principal(oid="all-staff", kind="group")]

    def to_documents(self, item: dict):
        return [Document(tenant_id=TENANT, source_id=SOURCE,
                         external_id=item["external_id"], content_ref="",
                         acl=self.fetch_acl(item), title=item["title"])]


class CrashingEmbedder(HashingEmbedding):
    """Counts what it embeds, and dies once on the document named by `crash_on`.

    Attribution is by the document id carried in the chunk text, which is what lets the test
    assert the thing that actually costs money: that resume does not RE-EMBED work the first
    run already paid for."""

    def __init__(self, crash_on: str | None = None) -> None:
        super().__init__()
        self.crash_on = crash_on
        self.embedded: list[str] = []            # doc ids, one entry per embedded chunk

    def embed(self, texts: list[str]):
        for t in texts:
            for doc_id in DOC_IDS:
                if doc_id in t:
                    if doc_id == self.crash_on:
                        raise RuntimeError(f"embedding backend died on {doc_id}")
                    self.embedded.append(doc_id)
        return super().embed(texts)

    def docs_embedded(self) -> set[str]:
        return set(self.embedded)


def _indexed_docs(index: InMemoryIndex) -> set[str]:
    """Which documents actually have chunks in the index, read the same way retrieval reads
    them — no private attribute, so this cannot pass against a half-written index."""
    found = set()
    for doc_id in DOC_IDS:
        if index.list_doc_segments(ReadScope(TENANT), doc_id, ["all-staff"]):
            found.add(doc_id)
    return found


def main() -> int:
    obj = InMemoryObjectStore()
    index = InMemoryIndex(obj)
    connector = SixDocConnector()
    jobs = InMemoryJobStore()

    # ---------------------------------------------------------------- run 1: dies at doc-4
    job1 = jobs.create(tenant_id=TENANT, source_id=SOURCE, cursor=None)
    embedder1 = CrashingEmbedder(crash_on=BOOM)
    crashed = False
    try:
        run_ingestion(connector, InMemoryQueue(), obj, LocalRichExtractor(), embedder1, index,
                      cursor=None, checkpoint=JobCheckpoint(jobs, job1.job_id))
    except RuntimeError:
        crashed = True
    assert crashed, "the rig is wrong: run 1 was supposed to die on doc-4"

    done_before = _indexed_docs(index)
    assert done_before == {"doc-1.txt", "doc-2.txt", "doc-3.txt"}, (
        "1. PER-DOCUMENT CHECKPOINT: the three documents that finished before the crash must "
        f"be indexed and queryable, not lost with the run. Indexed: {sorted(done_before)}")

    recorded = jobs.completed_documents(job1.job_id)
    assert recorded == done_before, (
        f"the job record must match what is really indexed: {sorted(recorded)} vs "
        f"{sorted(done_before)}")

    failed = jobs.get(job1.job_id)
    assert failed.status == "failed", f"a crashed job must say so, not {failed.status!r}"
    assert failed.next_cursor is None, (
        "5. CURSOR: a run that did not finish its batch must not leave an advanced cursor "
        f"behind, got {failed.next_cursor!r}")

    # ------------------------------------------------------- run 2: resume, same cursor
    embedder2 = CrashingEmbedder(crash_on=None)
    result = run_ingestion(connector, InMemoryQueue(), obj, LocalRichExtractor(), embedder2,
                           index, cursor=None,
                           checkpoint=JobCheckpoint(jobs, job1.job_id, resume=True))

    assert connector.listed_with == [None, None], (
        "3. PINNED CURSOR: the resume must re-list with the SAME cursor as the failed run. "
        f"Persisting the batch's next_cursor early silently skips unprocessed items. "
        f"Cursors used: {connector.listed_with}")

    redone = embedder2.docs_embedded() & done_before
    assert not redone, (
        "2. NO REDONE WORK: the resume re-embedded documents the first run already indexed "
        f"({sorted(redone)}). Embedding is the entire cost of a crawl — redoing it is the "
        "defect this card exists for.")

    assert embedder2.docs_embedded() == {"doc-4.txt", "doc-5.txt", "doc-6.txt"}, (
        f"the resume should embed exactly the remainder, got {sorted(embedder2.docs_embedded())}")

    assert not (set(connector.fetched[3:]) & done_before), (
        "a skipped document must be skipped BEFORE fetch_content — a completed document "
        "should cost no network on resume either")

    assert _indexed_docs(index) == set(DOC_IDS), (
        "4. NO DOCUMENT LOST: every document must be indexed once the resume completes, "
        f"got {sorted(_indexed_docs(index))}")

    assert result.cursor == NEXT_CURSOR, (
        "5. CURSOR: the batch completed, so the source's next cursor is now safe to persist")
    finished = jobs.get(job1.job_id)
    assert finished.status == "succeeded", f"got {finished.status!r}"
    assert finished.next_cursor == NEXT_CURSOR

    # ----------------------------------------------- a clean run needs no checkpoint at all
    obj2 = InMemoryObjectStore()
    index2 = InMemoryIndex(obj2)
    plain = run_ingestion(SixDocConnector(), InMemoryQueue(), obj2, LocalRichExtractor(),
                          HashingEmbedding(), index2, cursor=None)
    assert plain.doc_count == 6 and plain.cursor == NEXT_CURSOR, (
        "every existing caller passes no checkpoint and must be unchanged")
    assert _indexed_docs(index2) == set(DOC_IDS)

    print("PASS #455: a crash keeps its completed documents, the resume re-lists with the "
          "same cursor, re-embeds only the remainder, and loses nothing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
