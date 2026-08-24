import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.ports.base import ReadScope  # noqa: E402

from dbsearch.adapters.local import (
    HashingEmbedding, InMemoryIndex, InMemoryObjectStore, InMemoryQueue, LocalRichExtractor,
)
from dbsearch.connectors.upload import UploadConnector
from dbsearch.pipeline.runner import run_ingestion


def test_list_doc_segments_returns_locators_and_preview():
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    index = InMemoryIndex(store)
    conn = UploadConnector("t", "notes", "Notes.csv", b"name,role\nAlice,Partner\nBob,Analyst\n",
                           "text/csv", ["all-staff"], "")
    run_ingestion(conn, queue, store, LocalRichExtractor(), HashingEmbedding(), index)
    segs = index.list_doc_segments(ReadScope("t"), "notes", principals=["all-staff"])
    assert len(segs) == 2
    assert segs[0]["locator"] == {"kind": "row", "n": 2}
    assert "Alice" in segs[0]["preview"]
    assert "chunk_id" in segs[0]


if __name__ == "__main__":
    test_list_doc_segments_returns_locators_and_preview(); print("PASS")
