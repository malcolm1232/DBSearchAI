"""LocalRichExtractor — ExtractorPort for the self-host edition.

Dispatches (data, mime) to a per-format parser in adapters/local/extract/, each returning
list[Segment] with a locator (slide/page/row/section). extract() is kept as a backward-compatible
shim (joins segment texts). Parser libs are imported ONLY inside their modules so core/ports
stay parser-free (LAW 7). Scanned/image PDFs (OCR) remain Azure's job.
"""
from __future__ import annotations

from dbsearch.core.models import Segment
from dbsearch.ports.base import (  # noqa: F401  (re-exported: long-standing import site)
    ExtractorPort,
    ParseProducedNoText,
    UnsupportedMedia,
)


class LocalRichExtractor(ExtractorPort):
    def extract(self, data: bytes, mime: str) -> str:
        return "\n".join(s.text for s in self.extract_segments(data, mime))

    def extract_segments(self, data: bytes, mime: str) -> list[Segment]:
        from dbsearch.adapters.local.extract import segment_for

        fn = segment_for(mime)
        if fn is None:
            raise UnsupportedMedia(mime)
        try:
            segs = fn(data)
        except (UnsupportedMedia, ParseProducedNoText):
            raise  # already the intended, caller-mapped exceptions — pass through unchanged
        except Exception:
            # Malformed bytes for this mime (bad JSON, non-zip pptx/docx, corrupt xlsx, ...):
            # parser libs raise their own native exceptions. Don't let those escape as a raw
            # 500 (or a poison message in the async pipeline) or leak file content in the
            # message — map to the existing "parse produced no usable text" 422 path instead.
            raise ParseProducedNoText(mime)
        if not segs:
            raise ParseProducedNoText(mime)
        return segs
