"""#831 / #843 - one definition of "is there room on the blob volume for this write".

#831 put a headroom guard on POST /admin/upload: refuse a write that would push the blob
volume under its free-space floor, whatever the caller's tier says. That guard protects
AVAILABILITY, not revenue - a per-account quota counts one account, and if the disk fills,
prod goes down for everybody rather than for the uploader.

#843 found it was watching the smaller half. `_enforce_disk_headroom` was called from exactly
one place, while every connector crawl (gdrive - live since #813 - sharepoint, folder) wrote
each fetched file's full raw bytes to the same volume through `run_ingestion` with no size cap
and no headroom check at all. Prod evidence that this is the dominant writer, not a theoretical
one: #833 measured 211MB of connector blobs against 199KB of uploads. A user composing a Drive
source over tens of gigabytes could cross the floor with the guard never firing.

So the RULE lives here once - the floor, its env override, and the arithmetic - and the two
callers differ only in how they REPORT it, which is correct: the HTTP path owes the user a 507
with an explanation, and the crawl owes its job an error it can record. Two presentations of
one rule, rather than two copies of the rule.

Fail-open is deliberate and matches #831: a store that cannot report free space (in-memory
does not persist; a cloud store writes remotely) has nothing this guard could protect, so it
is not enforced there.
"""
from __future__ import annotations

import logging
import os

#: 2 GiB. Absolute bytes rather than a percentage, because 20% of a small disk is a real
#: number and 20% of a big one is waste (#831).
DEFAULT_FLOOR_BYTES = 2 * 1024 ** 3
FLOOR_ENV = "DBSEARCH_DISK_FLOOR_BYTES"


def floor_bytes() -> int:
    """The configured free-space floor, never raising. #839: a malformed value must not take
    the product down - it logs and falls back, because an operator's typo is not a reason to
    refuse every write."""
    raw = os.environ.get(FLOOR_ENV)
    if raw in (None, ""):
        return DEFAULT_FLOOR_BYTES
    try:
        return int(raw)
    except (TypeError, ValueError):
        logging.getLogger("dbsearch").error(
            "%s is not a number (%r); using the %d byte default",
            FLOOR_ENV, raw, DEFAULT_FLOOR_BYTES)
        return DEFAULT_FLOOR_BYTES


def shortfall(store, incoming: int) -> "tuple[int, int] | None":
    """`(free, floor)` when writing `incoming` bytes would cross the floor, else None.

    None also means "cannot tell, so do not enforce": a store that raises NotImplementedError
    from `free_bytes()` holds nothing on this disk, and an unexpected error must not turn a
    working ingest into a failure - the guard exists to protect availability, and taking the
    product down to protect it would be self-defeating.
    """
    try:
        free = store.free_bytes()
    except NotImplementedError:
        return None
    except Exception:
        logging.getLogger("dbsearch").exception(
            "disk headroom check failed; allowing the write")
        return None
    floor = floor_bytes()
    return None if free >= floor + incoming else (free, floor)
