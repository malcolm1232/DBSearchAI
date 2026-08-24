"""JSON -> one segment per top-level record (array element or object key). Nested values
flattened to `path: value` lines so a bag-of-words embedder can retrieve them.

#499: an OVERSIZED subtree recurses structurally instead of flattening whole — a Graph-API
dump `{"@odata.context": ..., "value": [999 records]}` used to become ONE 400,000-char
segment (then sliding-windowed at arbitrary 1200-char boundaries, shredding records), and
now becomes one segment per record. Record-sized nodes keep the exact pre-#499 output."""
from __future__ import annotations

import json as _json
import os

from dbsearch.core.models import Segment

# A node whose flattened text stays under this emits as ONE segment (record-sized, the
# pre-#499 shape); over it, we recurse structurally. 2x the 1200-char chunk window, so a
# slightly-plump record survives whole (the window splits it in two with overlap) rather
# than being shredded into per-field fragments.
_SPLIT_MAX = int(os.environ.get("JSON_SPLIT_MAX_CHARS", "2400"))


def _flatten(obj, prefix: str = "") -> list[str]:
    lines: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            lines.extend(_flatten(v, key))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            lines.extend(_flatten(v, f"{prefix}[{i}]"))
    else:
        lines.append(f"{prefix}: {obj}" if prefix else str(obj))
    return lines


def _split(obj, path: str, out: list[Segment], prefix: str = "") -> None:
    """Emit `obj` as one record segment if it is record-sized; otherwise recurse into its
    structure (list -> per element, dict -> per key) until the pieces are. `prefix` keys
    the flattened lines only at the emit level — recursed records read as themselves
    (`displayName: X`), with their position kept in the locator path, not the text."""
    text = "\n".join(_flatten(obj, prefix))
    container = isinstance(obj, (dict, list))
    if not container or len(text) <= _SPLIT_MAX:
        if text.strip():
            out.append(Segment(text=text, locator={"kind": "record", "path": path}))
        return
    if isinstance(obj, list):
        if any(isinstance(e, (dict, list)) for e in obj):
            for i, e in enumerate(obj):
                _split(e, f"{path}[{i}]", out)
        elif text.strip():
            # a huge list of scalars has no record structure — one segment, the
            # downstream sliding window handles it like prose
            out.append(Segment(text=text, locator={"kind": "record", "path": path}))
        return
    for k, v in obj.items():
        _split(v, f"{path}.{k}" if path else str(k), out)


def _repair_truncated(text: str):
    """Bounded truncation repair for ONE incomplete JSON document: cut back to the last
    position where a container closed cleanly, append the closers still open there, and
    strict-parse the result. Only the final partial record is lost. None if that fails."""
    stack: list[str] = []
    in_str = escaped = False
    last_pos, last_closers = 0, ""
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                return None  # not truncation — structurally broken
            stack.pop()
            last_pos, last_closers = i + 1, "".join(reversed(stack))
    if not last_pos:
        return None
    try:
        return _json.loads(text[:last_pos] + last_closers)
    except ValueError:
        return None


def loads_lenient(text: str):
    """Strict json.loads, else salvage the two real-corpus shapes that motivated #499
    (both live in the actual B-011 artifact, a 4.9MB Graph dump saved as Json.txt):
    several API responses CONCATENATED into one file, and a final document sliced
    mid-record by an upstream cap (`text[:MAX_CHARS]`, any export limit). Without
    salvage, strict parsing loses ALL structure and the whole dump degrades to one giant
    prose segment. Concatenated documents come back as a list (so records keep their
    per-dump paths); a lone document comes back as itself. None if nothing parses."""
    try:
        return _json.loads(text)
    except ValueError:
        pass
    docs, pos, n = [], 0, len(text)
    dec = _json.JSONDecoder()
    while pos < n:
        while pos < n and text[pos] in " \t\r\n":
            pos += 1
        if pos >= n:
            break
        try:
            obj, pos = dec.raw_decode(text, pos)
            docs.append(obj)
        except ValueError:
            # raw_decode consumed every complete document before this point, so the
            # remainder is a single incomplete one — the truncation shape.
            tail = _repair_truncated(text[pos:])
            if tail is not None:
                docs.append(tail)
            break
    if not docs:
        return None
    return docs[0] if len(docs) == 1 else docs


def segment_value(doc) -> list[Segment]:
    """Segment an already-parsed JSON value (also the #499 sniff target for JSON stored
    inside .txt documents — see extract/text.py)."""
    out: list[Segment] = []
    if isinstance(doc, list):
        for i, rec in enumerate(doc):
            _split(rec, f"[{i}]", out)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _split(v, str(k), out, prefix=str(k))
    else:
        text = str(doc).strip()
        if text:
            out.append(Segment(text=text))
    return out


def segment(data: bytes) -> list[Segment]:
    doc = loads_lenient(data.decode("utf-8", "ignore"))
    if doc is None:
        raise ValueError("not JSON, and no truncation repair parses")
    return segment_value(doc)
