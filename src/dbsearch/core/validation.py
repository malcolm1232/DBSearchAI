"""#576 code review (round 2), Finding A (CRITICAL): a document `external_id` becomes a
raw filesystem PATH SEGMENT wherever the local object-store adapters are in play -
`raw/{tenant}/{external_id}`, `segments/{tenant}/{external_id}`,
`chunk/{tenant}/{external_id}/{n}`, `emb/{tenant}/{external_id}/{n}` (pipeline/runner.py).

`POST /ingest {"external_id": ".."}` reached `FilesystemObjectStore.put`/`delete_prefix`
completely unvalidated, and `..` as a path segment walks OUT of the intended per-document
directory - the reviewer verified an adapter-level `rmtree` on a `..`-derived prefix
destroyed a SIBLING account's blob. The adapter itself is now hardened (Finding A layer 1,
`adapters/local/__init__.py`'s `_safe_path`) by resolving and comparing against root rather
than trusting the string shape - but a bad id should never be ACCEPTED in the first place,
which is what this module is for (layer 2, defense in depth).

Two rules, not one, because "external_id" means two different things depending on who
produces it:

  - `is_safe_external_id` - STRICT, single segment only. For a caller-TYPED id (today,
    only `POST /ingest`'s request body): a hand-typed opaque document id has no legitimate
    reason to carry a `/` at all, so any is refused.
  - `has_traversal_segment` - LOOSER, multi-segment aware. For a CONNECTOR-produced id
    (`pipeline/runner.py`'s `run_ingestion`, the backstop every ingestion path funnels
    through - folder, CSV, SharePoint, SharePointGraph, upload, resync): `FolderConnector`
    legitimately uses the item's relative path as its stable external_id
    ("all-staff/handbook.txt" - see connectors/folder.py), which is a perfectly safe
    multi-segment key under `FilesystemObjectStore`'s own nested-path design. Refusing
    every `/` there would break real functionality for no safety gain; what actually
    matters is refusing a `.` or `..` PATH SEGMENT specifically (the directory-traversal
    shape) or an absolute path, exactly the same test `FilesystemObjectStore._safe_path`
    applies at the adapter layer.

Both refuse control characters (#576 review round 3, Finding H): a NUL byte is not a
traversal, but the OS filesystem calls raise their OWN uncaught `ValueError` on one (an
"embedded null byte"), and the pgvector adapter's `doc_external_id` column is never run
through the `_pg_text` NUL-stripper `content`/`title`/`uri` get (see pgvector.py) - either
way, an id that passes the accept-side check and dies deeper as an unhandled exception is
a 500 where this should have been a clean 400/skip. Refusing it HERE, at the one place both
of these ids are validated, means neither the filesystem adapter nor the Postgres adapter
ever sees one.
"""
from __future__ import annotations

#: Control characters (C0 set, 0x00-0x1F, plus DEL 0x7F) are never legitimate in an opaque
#: id - NUL breaks filesystem calls and Postgres text columns outright; newline/CR make an
#: id impossible to log or display safely. Checked as a shared predicate so both rules
#: below refuse the identical set.
_CONTROL_CHARS = frozenset(chr(c) for c in range(0x20)) | {chr(0x7F)}


def _has_control_char(s: str) -> bool:
    return any(ch in _CONTROL_CHARS for ch in s)


def is_safe_external_id(external_id: "str | None") -> bool:
    """Refuse anything that could ever be interpreted as more than one path segment, a
    directory traversal, or an id that would crash a downstream store:

      - empty / all-whitespace
      - contains a path separator (forward OR back slash - a Windows-hosted deploy uses
        `\\`, and a defense against traversal that only checks `/` is not one)
      - is exactly `.` or `..`
      - starts with `.` (the hidden-file convention; also blocks any other leading-dot
        traversal-adjacent shape without needing to enumerate them)
      - contains a control character (NUL, newline, CR, ... - Finding H)

    Deliberately conservative: this is an OPAQUE id used as a storage/index key, never
    displayed or parsed for meaning, so refusing a wide swath of "unusual" ids costs
    nothing a legitimate caller needs. `_slug()` (server/app.py's upload path) already
    produces ids that pass this trivially - only a raw, caller-typed id (`/ingest`) or an
    unusual connector-sourced id can ever fail it. Connector-sourced ids that legitimately
    use `/` as a segment separator (a folder connector's relative path) should be checked
    with `has_traversal_segment` instead - see the module docstring."""
    if not external_id or not external_id.strip():
        return False
    if "/" in external_id or "\\" in external_id:
        return False
    if external_id in (".", ".."):
        return False
    if external_id.startswith("."):
        return False
    if _has_control_char(external_id):
        return False
    return True


def has_traversal_segment(external_id: "str | None") -> bool:
    """True if `external_id` could ever walk outside its own directory once turned into a
    filesystem path, or crash a downstream store outright - empty, absolute, containing a
    `.`/`..` path SEGMENT (checked per-segment, not as a substring: "policy..2024" is a
    normal, safe id; a segment that IS exactly "." or ".." is not), or containing a
    control character (Finding H). Unlike `is_safe_external_id`, a plain `/` separating
    otherwise-normal segments is NOT refused here - see the module docstring for why."""
    if not external_id or not external_id.strip():
        return True
    if _has_control_char(external_id):
        return True
    normalized = external_id.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    return any(part in (".", "..") for part in normalized.split("/"))
