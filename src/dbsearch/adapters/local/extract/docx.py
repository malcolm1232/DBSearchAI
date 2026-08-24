"""DOCX -> one segment per heading-delimited section. python-docx lazy-imported."""
from __future__ import annotations

import io

from dbsearch.core.models import Segment


def segment(data: bytes) -> list[Segment]:
    from docx import Document as DocxDocument

    doc = DocxDocument(io.BytesIO(data))
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    body: list[str] = []

    def flush() -> None:
        if body or heading:
            sections.append((heading, list(body)))

    for para in doc.paragraphs:
        style = (para.style.name if para.style else "") or ""
        if style.startswith("Heading"):
            flush()
            heading = para.text.strip()
            body = []
        elif para.text.strip():
            body.append(para.text)
    flush()

    out: list[Segment] = []
    n = 0
    for h, paras in sections:
        text = ("\n".join([h] + paras) if h else "\n".join(paras)).strip()
        if not text:
            continue
        n += 1
        loc = {"kind": "section", "n": n}
        if h:
            loc["heading"] = h
        out.append(Segment(text=text, locator=loc))
    return out
