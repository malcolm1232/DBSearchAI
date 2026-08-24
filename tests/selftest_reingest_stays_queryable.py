"""Self-test: a re-ingest must NOT blank the corpus while it embeds (#391).

`run_ingestion` used to delete EVERY discovered document's chunks up front, then parse,
then embed, and only write replacement chunks in the final `_stage_index`. Embedding is
the slow stage — CPU-only `nomic-embed-text` on the 2-vCPU prod box measured 66 chunks/min
— so for the whole multi-hour window the document was gone from the index and every query
answered "Searched 0 documents you can access". That is indistinguishable from a broken
product, and it is what the operator hit on 2026-07-29.

The replace semantics of `selftest_reingest_replace.py` must survive this fix: no orphaned
higher-`n` chunks, and a doc that now yields ZERO chunks must still lose its stale ones.
Those are re-asserted here so the two guarantees can't be traded against each other.

    python3 tests/selftest_reingest_stays_queryable.py
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
ACL = ["all-staff"]


class _WatchingEmbedding(HashingEmbedding):
    """Records how much of `external_id` is still queryable at each embed call — i.e.
    exactly what a user asking a question DURING the ingest would be able to retrieve."""

    def __init__(self, index, external_id: str) -> None:
        super().__init__()
        self._index = index
        self._external_id = external_id
        self.counts_during_embed: list[int] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.counts_during_embed.append(
            self._index.chunk_count_for(TENANT, self._external_id))
        return super().embed(texts)


def _ingest(index, store, csv_bytes, acl, embedder=None):
    conn = UploadConnector(TENANT, "roster", "Roster", csv_bytes, "text/csv", acl, "")
    run_ingestion(conn, InMemoryQueue(), store, LocalRichExtractor(),
                  embedder or HashingEmbedding(), index)


def test_old_chunks_stay_queryable_while_the_reingest_embeds():
    store = InMemoryObjectStore()
    index = InMemoryIndex(store)

    _ingest(index, store, b"name,role\nAlice,Partner\nBob,Analyst\nCarol,VP\n", ACL)
    assert index.chunk_count_for(TENANT, "roster") == 3

    watcher = _WatchingEmbedding(index, "roster")
    _ingest(index, store, b"name,role\nAlice,Partner\nBob,Analyst\nCarol,VP\n", ACL,
            embedder=watcher)

    assert watcher.counts_during_embed, "embedder was never called — test is not exercising the crawl"
    # The corpus must never go to zero while the replacement is still being computed.
    assert min(watcher.counts_during_embed) > 0, (
        f"corpus was blanked during the re-ingest: chunk counts seen while embedding "
        f"were {watcher.counts_during_embed} — a user querying mid-ingest gets nothing")


def test_replace_semantics_still_hold():
    """The #391 fix must not reintroduce the orphan bug selftest_reingest_replace.py covers."""
    store = InMemoryObjectStore()
    index = InMemoryIndex(store)

    _ingest(index, store, b"name,role\nAlice,Partner\nBob,Analyst\nCarol,VP\n", ["deal-team"])
    assert index.chunk_count_for(TENANT, "roster") == 3

    _ingest(index, store, b"name,role\nAlice,Partner\n", ["all-staff"])
    assert index.chunk_count_for(TENANT, "roster") == 1, index.chunk_count_for(TENANT, "roster")
    assert index.list_doc_segments(ReadScope(TENANT), "roster", principals=["deal-team"]) == []
    assert len(index.list_doc_segments(ReadScope(TENANT), "roster", principals=["all-staff"])) == 1


def test_doc_yielding_zero_chunks_still_loses_its_stale_chunks():
    """The guarantee the up-front delete existed for: if this crawl produces NO chunks for a
    document, its old chunks must not survive carrying stale content and a stale ACL."""
    store = InMemoryObjectStore()
    index = InMemoryIndex(store)

    _ingest(index, store, b"name,role\nAlice,Partner\nBob,Analyst\n", ["deal-team"])
    assert index.chunk_count_for(TENANT, "roster") == 2

    # header only -> parses to zero rows -> zero chunks for the same external_id
    _ingest(index, store, b"name,role\n", ["deal-team"])
    assert index.chunk_count_for(TENANT, "roster") == 0, (
        "a crawl that yielded no chunks left stale ones behind (LAW 2 freshness)")


if __name__ == "__main__":
    test_old_chunks_stay_queryable_while_the_reingest_embeds()
    test_replace_semantics_still_hold()
    test_doc_yielding_zero_chunks_still_loses_its_stale_chunks()
    print("PASS")
