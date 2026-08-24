import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local.rich_extractor import (
    LocalRichExtractor, UnsupportedMedia, ParseProducedNoText,
)


def test_text_segment_single_no_locator():
    segs = LocalRichExtractor().extract_segments(b"hello world", "text/plain")
    assert len(segs) == 1
    assert segs[0].text == "hello world"
    assert segs[0].locator == {}


def test_extract_str_still_joins_segments():
    # backward-compat: extract() returns a joined string
    assert LocalRichExtractor().extract(b"hello world", "text/markdown") == "hello world"


def test_unknown_mime_raises():
    try:
        LocalRichExtractor().extract_segments(b"x", "application/zip")
        assert False, "expected UnsupportedMedia"
    except UnsupportedMedia:
        pass


def test_empty_text_raises_no_text():
    try:
        LocalRichExtractor().extract_segments(b"   ", "text/plain")
        assert False, "expected ParseProducedNoText"
    except ParseProducedNoText:
        pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("PASS", name)
