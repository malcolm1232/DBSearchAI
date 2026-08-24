"""Self-test: LocalRichExtractor parses PDF + text behind ExtractorPort; rejects the rest.

    python3 tests/selftest_rich_extractor.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import (  # noqa: E402
    LocalRichExtractor, UnsupportedMedia, ParseProducedNoText,
)

# Minimal valid PDF whose page text is "Hello Falcon deal-team" (verified with pypdf).
PDF_HELLO = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 52>>stream\n"
    b"BT /F1 24 Tf 20 100 Td (Hello Falcon deal-team) Tj ET\n"
    b"endstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R/Size 6>>\nstartxref\n0\n%%EOF\n"
)
# Minimal PDF with a page but no text content -> extraction yields "".
PDF_EMPTY = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]>>endobj\n"
    b"trailer<</Root 1 0 R/Size 4>>\nstartxref\n0\n%%EOF\n"
)


def main():
    ext = LocalRichExtractor()

    # text passthrough equals PlainTextExtractor behavior
    assert ext.extract(b"hello world", "text/plain") == "hello world"
    assert ext.extract(b"# title", "text/markdown") == "# title"

    # PDF -> text
    text = ext.extract(PDF_HELLO, "application/pdf")
    assert "Hello Falcon deal-team" in text, repr(text)

    # unsupported mime -> UnsupportedMedia
    try:
        ext.extract(b"\x89PNG", "image/png")
        assert False, "expected UnsupportedMedia"
    except UnsupportedMedia:
        pass

    # textless PDF -> ParseProducedNoText
    try:
        ext.extract(PDF_EMPTY, "application/pdf")
        assert False, "expected ParseProducedNoText"
    except ParseProducedNoText:
        pass

    print("PASS selftest_rich_extractor")


if __name__ == "__main__":
    main()
