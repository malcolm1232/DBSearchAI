"""Per-format local parsers. Each module exposes segment(data)->list[Segment] and lazy-imports
its parsing lib INSIDE the function so core/ports stay dependency-free (LAW 7). segment_for(mime)
returns the parser callable, or None for an unsupported mime (caller raises UnsupportedMedia)."""
from __future__ import annotations

from typing import Callable

from dbsearch.core.models import Segment
from dbsearch.adapters.local.extract import text as _text
from dbsearch.adapters.local.extract import pdf as _pdf
from dbsearch.adapters.local.extract import pptx as _pptx
from dbsearch.adapters.local.extract import docx as _docx
from dbsearch.adapters.local.extract import tabular as _tabular
from dbsearch.adapters.local.extract import json_ as _json_mod

# mime -> callable(bytes) -> list[Segment]. Parser modules import only stdlib at top level;
# the heavy libs (pptx/docx/openpyxl) are imported inside each segment() call, so building this
# table is cheap and import-safe even when those libs aren't installed.
_REGISTRY: dict[str, Callable[[bytes], list[Segment]]] = {
    "text/plain": _text.segment,
    "text/markdown": _text.segment,
    "": _text.segment,
    "application/pdf": _pdf.segment,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": _pptx.segment,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _docx.segment,
    "text/csv": _tabular.segment_csv,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": _tabular.segment_xlsx,
    "application/json": _json_mod.segment,
}


def register(mime: str, fn: Callable[[bytes], list[Segment]]) -> None:
    _REGISTRY[mime] = fn


def segment_for(mime: str) -> Callable[[bytes], list[Segment]] | None:
    return _REGISTRY.get(mime)
