"""#843 - the disk-headroom guard covers the path that actually fills the disk.

#831 refused an UPLOAD that would push the blob volume under its free-space floor. It was
called from exactly one place. Meanwhile every connector crawl - gdrive (live since #813),
sharepoint, folder - wrote each fetched file's full raw bytes to the same volume through
`run_ingestion`, with no size cap and no headroom check at all.

That is not the smaller half. #833 measured prod at 211MB of connector blobs against 199KB of
uploads: the guarded path was the rounding error and the unguarded one was the volume. A user
composing a Drive source over tens of gigabytes could cross the floor with the guard never
firing, and when the disk fills prod goes down for EVERY account, not for the uploader.

The rule now lives once in `core.headroom` - the floor, its env override, the arithmetic - and
the two callers differ only in how they REPORT it: the HTTP path owes the user a 507 with an
explanation, the crawl owes its job a `NoDiskHeadroom` it can record. Two presentations of one
rule rather than two copies of the rule, so they cannot drift.

Checking inside `run_ingestion` rather than at each caller is deliberate: all five ingest call
sites in the product funnel through that loop, so a future one inherits the guard instead of
re-deriving it (#799).

NOT IN SCOPE, and left to the owner: whether connector blobs should be written to our volume
AT ALL. ADR 0027 rule 1 says connector content is never held, and 211MB of it on prod is in
tension with that. This card makes the volume safe; the ADR question is carded separately
because it is a product decision, not a bug fix.

    PYTHONPATH=src python3 tests/selftest_843_crawl_headroom.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("SELFHOST_BACKEND", "memory")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.adapters.local import (InMemoryIndex, InMemoryObjectStore,  # noqa: E402
                                     InMemoryQueue, LocalRichExtractor)
from dbsearch.connectors.sharepoint import SharePointConnector  # noqa: E402
from dbsearch.core import headroom  # noqa: E402
from dbsearch.pipeline.runner import NoDiskHeadroom, run_ingestion  # noqa: E402
from dbsearch.server.edition import build_edition  # noqa: E402


class _Store(InMemoryObjectStore):
    """An object store that reports whatever free space the test wants."""

    def __init__(self, free):
        super().__init__()
        self._free = free

    def free_bytes(self):
        if self._free is None:
            raise NotImplementedError
        return self._free


def _crawl(store, n=1):
    seed = [{"external_id": f"c{i}", "title": f"Doc {i}", "uri": f"https://x/{i}",
             "acl": ["all-staff"], "text": "crawled content that lands on our volume"}
            for i in range(n)]
    ed = build_edition()
    return run_ingestion(SharePointConnector(tenant_id="t", seed=seed), InMemoryQueue(),
                         store, LocalRichExtractor(), ed.embedder, InMemoryIndex(store))


def test_a_crawl_stops_at_the_floor():
    """The defect: the path that dominates the volume had no guard at all."""
    store = _Store(free=1024)                      # far below the 2GiB floor
    try:
        _crawl(store)
    except NoDiskHeadroom as exc:
        assert "headroom" in str(exc), str(exc)
        assert not [k for k in store._blobs if k.startswith("raw/")], (
            "it raised but had already written the raw blob - the check must come BEFORE "
            "the put, or the guard only reports a disk it has already filled")
        return
    raise AssertionError(
        "#843: a connector crawl wrote to a volume already under its floor. The #831 guard "
        "watches uploads only, and prod measured 211MB of connector blobs against 199KB of "
        "uploads - the guarded path is the rounding error.")


def test_a_crawl_with_room_is_untouched():
    """CONTROL. A guard that refused every crawl would pass the test above and break ingest."""
    store = _Store(free=500 * 1024 ** 3)
    result = _crawl(store, n=2)
    assert result.doc_count == 2, result
    assert len([k for k in store._blobs if k.startswith("raw/")]) == 2, sorted(store._blobs)


def test_a_store_that_cannot_report_free_space_is_not_enforced():
    """CONTROL, and the fail-open direction #831 chose: a store that holds nothing on this
    disk (in-memory, or a cloud store writing remotely) has nothing the guard could protect,
    and refusing its writes would be enforcing a limit that does not apply."""
    store = _Store(free=None)                      # raises NotImplementedError
    result = _crawl(store)
    assert result.doc_count == 1, result


def test_the_floor_has_one_definition():
    """#843's real point: the crawl and the upload path must not drift. Both read the floor
    from `core.headroom`, so moving it moves both."""
    os.environ["DBSEARCH_DISK_FLOOR_BYTES"] = "4096"
    try:
        assert headroom.floor_bytes() == 4096
        # a store with 5000 free has room for 100 bytes over a 4096 floor, but not for 2000
        assert headroom.shortfall(_Store(free=5000), 100) is None
        assert headroom.shortfall(_Store(free=5000), 2000) is not None
    finally:
        os.environ.pop("DBSEARCH_DISK_FLOOR_BYTES", None)


def test_a_malformed_floor_falls_back_instead_of_raising():
    """#839's rule, now shared: an operator's typo must not take every write down."""
    os.environ["DBSEARCH_DISK_FLOOR_BYTES"] = "two gigs"
    try:
        assert headroom.floor_bytes() == headroom.DEFAULT_FLOOR_BYTES
    finally:
        os.environ.pop("DBSEARCH_DISK_FLOOR_BYTES", None)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}\n        {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
