import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.core.models import Segment, Chunk
from dbsearch.ports.base import ExtractorPort


def test_segment_roundtrip():
    s = Segment(text="hello", locator={"kind": "slide", "n": 7})
    assert s.to_dict() == {"text": "hello", "locator": {"kind": "slide", "n": 7}}
    assert Segment.from_dict(s.to_dict()) == s
    assert Segment(text="x").locator == {}


def test_chunk_carries_locator():
    c = Chunk(tenant_id="t", doc_external_id="d", chunk_id="d#0", text_ref="r",
              allowed_principals=["g"], locator={"kind": "row", "n": 42})
    assert c.to_dict()["locator"] == {"kind": "row", "n": 42}
    assert Chunk.from_dict(c.to_dict()).locator == {"kind": "row", "n": 42}
    # backward-compat: an old chunk dict without locator decodes to {}
    old = {"tenant_id": "t", "doc_external_id": "d", "chunk_id": "d#0",
           "text_ref": "r", "allowed_principals": ["g"]}
    assert Chunk.from_dict(old).locator == {}


class _Stub(ExtractorPort):
    def extract(self, data: bytes, mime: str) -> str:
        return data.decode()


def test_extract_segments_default_wraps_extract():
    segs = _Stub().extract_segments(b"body text", "text/plain")
    assert [s.text for s in segs] == ["body text"]
    assert segs[0].locator == {}
    assert _Stub().extract_segments(b"", "text/plain") == []  # empty -> no segment


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("PASS", name)
