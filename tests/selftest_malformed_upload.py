"""Self-test: malformed new-format uploads raise ParseProducedNoText (-> 422), not a raw
parser exception (-> 500) — finding C of the Phase B fix wave.

Before the fix, `LocalRichExtractor.extract_segments` let parser libs raise their native
errors (json.JSONDecodeError, pptx's PackageNotFoundError on a non-zip, etc.) straight
through; those escape as an unhandled 500 at the upload endpoint and are poison messages
in the async pipeline. This test feeds clearly-malformed bytes to the JSON and PPTX
parsers directly and asserts the extractor maps ANY parser exception to the already-
intended `ParseProducedNoText` (which the caller maps to 422).

    python3 tests/selftest_malformed_upload.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local.rich_extractor import (  # noqa: E402
    LocalRichExtractor, ParseProducedNoText, UnsupportedMedia,
)

_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def test_malformed_json_raises_parse_produced_no_text():
    try:
        LocalRichExtractor().extract_segments(b"{not json", "application/json")
        assert False, "expected ParseProducedNoText"
    except ParseProducedNoText:
        pass


def test_malformed_pptx_raises_parse_produced_no_text():
    try:
        LocalRichExtractor().extract_segments(b"not a zip", _PPTX_MIME)
        assert False, "expected ParseProducedNoText"
    except ParseProducedNoText:
        pass


def test_unsupported_mime_still_raises_unsupported_media():
    # 415 path (unknown mime) must stay intact — Fix C only touches parser-exception mapping.
    try:
        LocalRichExtractor().extract_segments(b"\x89PNG", "image/png")
        assert False, "expected UnsupportedMedia"
    except UnsupportedMedia:
        pass


def test_empty_csv_still_raises_parse_produced_no_text():
    # existing 422 path (parse succeeded, no usable text) must stay intact.
    try:
        LocalRichExtractor().extract_segments(b"", "text/csv")
        assert False, "expected ParseProducedNoText"
    except ParseProducedNoText:
        pass


if __name__ == "__main__":
    test_malformed_json_raises_parse_produced_no_text()
    test_malformed_pptx_raises_parse_produced_no_text()
    test_unsupported_mime_still_raises_unsupported_media()
    test_empty_csv_still_raises_parse_produced_no_text()
    print("PASS")
