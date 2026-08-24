"""Self-test: _chunk_text produces real size+overlap chunks (not the whole doc as one),
so retrieval hands small passages to the LLM (fast + precise).

    python3 tests/selftest_chunking.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.pipeline.runner import _chunk_text  # noqa: E402

MAX = 1200


def main():
    # empty / whitespace -> no chunks
    assert _chunk_text("") == []
    assert _chunk_text("   \n  ") == []

    # short doc -> a single chunk (whitespace-normalized)
    assert _chunk_text("hello world") == ["hello world"]
    assert _chunk_text("hello   world\n\nfoo") == ["hello world foo"]

    # long doc -> many chunks, each within the size cap
    big = " ".join(f"word{i}" for i in range(2000))   # ~ 13k chars
    chunks = _chunk_text(big)
    assert len(chunks) > 1, "long doc must split into multiple chunks"
    assert all(len(c) <= MAX for c in chunks), f"chunk exceeds {MAX}: {[len(c) for c in chunks]}"

    # coverage: every word from the source appears in some chunk (no dropped content)
    src_words = set(big.split())
    seen = set()
    for c in chunks:
        seen.update(c.split())
    assert src_words <= seen, "chunking dropped content"

    # overlap: consecutive chunks share at least one token (continuity across boundaries)
    overlaps = 0
    for a, b in zip(chunks, chunks[1:]):
        if set(a.split()) & set(b.split()):
            overlaps += 1
    assert overlaps >= 1, "expected overlap between consecutive chunks"

    # #367: a mis-decoded file (UTF-16 read as 8-bit) carries NUL bytes. PostgreSQL text
    # cannot hold 0x00, so one NUL surviving normalization aborted the whole crawl at the
    # index stage. They must be gone by the time text becomes chunks — every consumer.
    nul = _chunk_text("alpha\x00beta \x00 gamma\x00")
    assert nul, "NUL-bearing text should still chunk"
    assert not any("\x00" in c for c in nul), f"NUL survived chunking: {nul!r}"
    assert set("alpha beta gamma".split()) <= set(nul[0].split()), \
        f"stripping NUL must not drop the words around it: {nul!r}"
    # a NUL-only document is empty text, not a chunk of nothing
    assert _chunk_text("\x00\x00") == []

    print(f"PASS selftest_chunking ({len(chunks)} chunks for ~13k chars)")


if __name__ == "__main__":
    main()
