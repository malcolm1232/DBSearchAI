"""Plain text / markdown -> one whole-doc segment (no locator).

#499: real corpora store JSON inside .txt files (the B-011 staff directory is a 400,000-char
Graph-API dump saved as `Json.txt`, mime text/plain) — sniffed here and handed to the JSON
segmenter so it chunks per RECORD instead of becoming one giant segment that the sliding
window shreds at arbitrary 1200-char boundaries. Anything that does not parse as a JSON
container stays exactly what it was: one prose segment."""
from __future__ import annotations

from dbsearch.core.models import Segment
from dbsearch.adapters.local.extract import json_ as _json_mod


def segment(data: bytes) -> list[Segment]:
    text = data.decode("utf-8", "ignore").strip()
    if not text:
        return []
    if text[0] in "{[":
        doc = _json_mod.loads_lenient(text)  # strict parse, else truncation repair
        if isinstance(doc, (dict, list)):
            segs = _json_mod.segment_value(doc)
            if segs:  # e.g. "{}" parses but yields nothing — fall back to prose
                return segs
    return [Segment(text=text)]
