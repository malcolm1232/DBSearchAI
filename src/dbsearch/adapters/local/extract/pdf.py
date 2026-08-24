"""PDF (text-layer) -> one segment per page. pypdf lazy-imported (LAW 7)."""
from __future__ import annotations

import io

from dbsearch.core.models import Segment


def segment(data: bytes) -> list[Segment]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    out: list[Segment] = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            out.append(Segment(text=text, locator={"kind": "page", "n": i}))
    return out
