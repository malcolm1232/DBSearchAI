"""#883: a restart must not wipe the source registry.

MEASURED ON PROD, twice: after every deploy /admin/sources showed only the seeded default
"sharepoint" with doc_count 0, the canvas node read "never-synced / Pick library & ingest",
and the corpus underneath was answering questions with citations the whole time. The registry
descriptor died with the process; pgvector did not.

Section A is HERMETIC and carries the load: it runs against InMemorySourceSyncStore, so it
fails on any box with or without Postgres. That is deliberate - a guard whose assertions only
run behind an opt-in DSN is green-by-skip on the machine that matters, which reads as CAUGHT
in the mutation matrix for entirely the wrong reason.

Section B proves the SQL against a real Postgres when DBSEARCH_SYNCSTATE_TEST_DSN is set, and
says so out loud when it is not: an unrun SQL path is an unproven one.
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dbsearch.connectors.registry import SourceDescriptor, SourceRegistry  # noqa: E402
from dbsearch.server.source_sync_store import (InMemorySourceSyncStore,  # noqa: E402
                                               PgSourceSyncStore)

CFG = {"az_tenant_id": "tid-1", "drive_id": "drive-A", "folder_path": "/HR", "owner_oid": "o1"}


class _FakeConnector:
    """The registry only ever holds a connector; it never calls one."""


def _desc(source_id="sharepoint:tid-1", config=CFG, job_tenant="", display="SharePoint"):
    return SourceDescriptor(source_id=source_id, kind="sharepoint", display_name=display,
                            connector=_FakeConnector(), config=config, job_tenant=job_tenant)


def test_sync_state_survives_a_discarded_registry():
    """The card's own guard: record a sync, DISCARD the registry, rebuild, assert it survived."""
    store = InMemorySourceSyncStore()

    reg1 = SourceRegistry(store=store, scope="selfhost")
    reg1.register(_desc(job_tenant="selfhost"))
    reg1.record_sync("sharepoint:tid-1", cursor="delta-token-1", doc_count=6,
                     at="2026-08-21T00:00:00Z", unreadable=2)

    del reg1                                    # the deploy

    reg2 = SourceRegistry(store=store, scope="selfhost")
    reg2.register(_desc(job_tenant="selfhost"))  # a fresh, virgin descriptor - what boot builds

    d = reg2.get("sharepoint:tid-1")
    assert d.doc_count == 6, f"doc_count did not survive: {d.doc_count}"
    assert d.cursor == "delta-token-1", f"cursor did not survive: {d.cursor}"
    assert d.last_sync_at == "2026-08-21T00:00:00Z", d.last_sync_at
    assert d.unreadable == 2, d.unreadable

    # The node reads list_sources(), not the descriptor - assert the surface, not the plumbing.
    summary = [s for s in reg2.list_sources() if s.source_id == "sharepoint:tid-1"][0]
    assert summary.doc_count == 6, f"the node would still render {summary.doc_count} docs"
    assert summary.last_sync_at == "2026-08-21T00:00:00Z"


def test_a_reregistering_seed_does_not_clobber_the_row():
    """build_edition re-registers its seeds on EVERY boot with a virgin descriptor.

    If register() overwrote instead of merging, the durable row would be zeroed a moment after
    being read, and the table would faithfully persist that zero forever."""
    store = InMemorySourceSyncStore()
    reg = SourceRegistry(store=store, scope="selfhost")
    reg.register(_desc(source_id="sharepoint", config=None))
    reg.record_sync("sharepoint", cursor="c", doc_count=5, at="2026-08-21T00:00:00Z")

    for _ in range(3):                          # three more deploys
        reg = SourceRegistry(store=store, scope="selfhost")
        reg.register(_desc(source_id="sharepoint", config=None))

    assert reg.get("sharepoint").doc_count == 5, "the seed clobbered its own persisted count"
    assert store.get("selfhost", "sharepoint")["doc_count"] == 5, "the ROW was zeroed"


def test_syncing_is_coerced_to_idle_but_error_survives():
    """No crawl outlives its process, so a persisted 'syncing' is a node spinning forever."""
    store = InMemorySourceSyncStore()
    reg = SourceRegistry(store=store, scope="selfhost")
    reg.register(_desc())
    reg.mark_syncing("sharepoint:tid-1")

    # mark_syncing deliberately does not write through: the row must never say "syncing".
    assert store.get("selfhost", "sharepoint:tid-1")["status"] != "syncing"

    reg2 = SourceRegistry(store=store, scope="selfhost")
    reg2.register(_desc())
    assert reg2.get("sharepoint:tid-1").status == "idle", "a crawl survived its own process"

    # An error, by contrast, IS the last thing known to be true and must persist.
    reg2.record_error("sharepoint:tid-1")
    reg3 = SourceRegistry(store=store, scope="selfhost")
    reg3.register(_desc())
    assert reg3.get("sharepoint:tid-1").status == "error", "a failed sync healed itself"


def test_a_different_drive_does_not_inherit_the_cursor():
    """A delta token belongs to ONE drive.

    Resuming another library's change feed skips everything before that position - the #716
    shape. The count is still restored (it describes the source id, not the token)."""
    store = InMemorySourceSyncStore()
    reg = SourceRegistry(store=store, scope="selfhost")
    reg.register(_desc())
    reg.record_sync("sharepoint:tid-1", cursor="delta-for-drive-A", doc_count=6,
                    at="2026-08-21T00:00:00Z")

    other = dict(CFG, drive_id="drive-B")       # same tenant, re-connected to a new library
    reg2 = SourceRegistry(store=store, scope="selfhost")
    reg2.register(_desc(config=other))
    assert reg2.get("sharepoint:tid-1").cursor is None, "resumed another drive's change feed"


def test_scope_partitions_the_table():
    """#565 in advance: two deployments' registries must not read each other's rows."""
    store = InMemorySourceSyncStore()
    a = SourceRegistry(store=store, scope="tenant-a")
    a.register(_desc())
    a.record_sync("sharepoint:tid-1", cursor="a", doc_count=9, at="2026-08-21T00:00:00Z")

    b = SourceRegistry(store=store, scope="tenant-b")
    b.register(_desc())
    assert b.get("sharepoint:tid-1").doc_count == 0, "read another scope's sync-state"


def test_rehydrate_brings_back_a_source_no_seed_builds():
    """The half the card's original fix shape missed.

    `sharepoint:<tid>` is registered only by connect_sharepoint, so after a restart it is not
    merely stale - it does not exist, and /admin/resync 404s on the id the canvas is asking
    about. Persisting the six mutable fields alone would rehydrate nothing at all."""
    from dbsearch.server.edition import _rehydrate_sources

    store = InMemorySourceSyncStore()
    reg = SourceRegistry(store=store, scope="selfhost")
    reg.register(_desc(job_tenant="selfhost"))
    reg.record_sync("sharepoint:tid-1", cursor="delta-1", doc_count=6,
                    at="2026-08-21T00:00:00Z")

    built = {}

    def factory(job_tenant, cfg):
        built["job_tenant"], built["cfg"] = job_tenant, cfg
        return _FakeConnector()

    fresh = SourceRegistry(store=store, scope="selfhost")     # a boot with only its seeds
    fresh.register(_desc(source_id="sharepoint", config=None))
    _rehydrate_sources(fresh, store, "selfhost", connector_factory=factory)

    d = fresh.get("sharepoint:tid-1")                         # would KeyError before the fix
    assert d.doc_count == 6, d.doc_count
    assert d.cursor == "delta-1", "an incremental resync would re-pay for the whole library"
    assert d.connector is not None, "a descriptor with no connector cannot be resynced"
    assert built["cfg"]["drive_id"] == "drive-A", built
    assert built["job_tenant"] == "selfhost", built["job_tenant"]

    ids = {s.source_id for s in fresh.list_sources()}
    assert ids == {"sharepoint", "sharepoint:tid-1"}, ids


def test_rehydrate_skips_what_it_cannot_rebuild():
    """A row with no recipe, and a factory that raises, must cost a warning and not the boot."""
    from dbsearch.server.edition import _rehydrate_sources

    store = InMemorySourceSyncStore()
    store.put("selfhost", "folder:old", {"kind": "folder", "display_name": "f", "cursor": None,
                                         "last_sync_at": None, "doc_count": 3, "unreadable": 0,
                                         "status": "idle", "job_tenant": "", "config": None})
    store.put("selfhost", "sharepoint:boom", {"kind": "sharepoint", "display_name": "b",
                                              "cursor": None, "last_sync_at": None,
                                              "doc_count": 1, "unreadable": 0, "status": "idle",
                                              "job_tenant": "", "config": CFG})

    def boom(job_tenant, cfg):
        raise RuntimeError("no credentials on this box")

    reg = SourceRegistry(store=store, scope="selfhost")
    _rehydrate_sources(reg, store, "selfhost", connector_factory=boom)   # must not raise
    assert reg.list_sources() == [], "rehydrated a source it could not build a connector for"

    # And a factory that declines (no SP_CONNECTOR_* env) is the same story without an exception.
    reg2 = SourceRegistry(store=store, scope="selfhost")
    _rehydrate_sources(reg2, store, "selfhost", connector_factory=lambda t, c: None)
    assert reg2.list_sources() == []


def test_registry_without_a_store_is_unchanged():
    """store=None is the pre-#883 lifecycle, which the whole router rail and every rig use."""
    reg = SourceRegistry()
    reg.register(_desc())
    reg.record_sync("sharepoint:tid-1", cursor="c", doc_count=4, at="2026-08-21T00:00:00Z")
    assert reg.get("sharepoint:tid-1").doc_count == 4
    reg.mark_syncing("sharepoint:tid-1")
    assert reg.get("sharepoint:tid-1").status == "syncing"
    reg.record_error("sharepoint:tid-1")
    assert reg.get("sharepoint:tid-1").status == "error"


def test_an_unreachable_store_degrades_instead_of_raising():
    """A Postgres blip must cost a count, never a canvas load or a crawl commit.

    Hermetic: needs no Postgres, only a connection that refuses. Also pins that _schema_done
    ends up False so the next call retries the DDL. Note what this does NOT prove: unlike
    PgManifestStore, this store resets that flag in its own except and holds the schema lock
    across the whole DDL, so the flag ORDERING inside _ensure_schema is not load-bearing here.
    A mutation moving it earlier was written for this guard and correctly survived; see the
    note where it would have lived in scripts/mutate_guards.py."""
    class FailingConn:
        def __enter__(self):
            raise RuntimeError("connection refused")

        def __exit__(self, *a):
            return False

    s = PgSourceSyncStore("postgresql://nobody:x@127.0.0.1:1/none")
    s._conn = lambda: FailingConn()
    assert s.get("selfhost", "sharepoint") is None      # swallowed, not raised
    assert s._schema_done is False, "a failed DDL left the flag set: the process is poisoned"
    s.put("selfhost", "sharepoint", {"kind": "sharepoint", "display_name": "x", "cursor": None,
                                     "last_sync_at": None, "doc_count": 0, "unreadable": 0,
                                     "status": "idle", "job_tenant": "", "config": None})
    assert s._schema_done is False
    assert s.list("selfhost") == []                     # degrades to empty, never 500s


def _pg_contract(store, scope):
    """The same contract Section A proves against the in-memory twin."""
    assert store.get(scope, "nope") is None
    row = {"kind": "sharepoint", "display_name": "SharePoint", "cursor": "delta-1",
           "last_sync_at": "2026-08-21T00:00:00Z", "doc_count": 6, "unreadable": 2,
           "status": "idle", "job_tenant": "selfhost", "config": CFG}
    store.put(scope, "sharepoint:tid-1", row)
    got = store.get(scope, "sharepoint:tid-1")
    assert got["doc_count"] == 6 and got["cursor"] == "delta-1", got
    assert got["config"] == CFG, got["config"]          # jsonb round trip
    assert got["last_sync_at"] == "2026-08-21T00:00:00Z", got

    store.put(scope, "sharepoint:tid-1", dict(row, doc_count=11, cursor="delta-2"))
    assert store.get(scope, "sharepoint:tid-1")["doc_count"] == 11, "upsert did not update"

    listed = store.list(scope)
    assert [r["source_id"] for r in listed] == ["sharepoint:tid-1"], listed
    assert listed[0]["config"] == CFG

    assert store.list("some-other-scope") == [], "scope did not partition the table"

    store.delete(scope, "sharepoint:tid-1")
    assert store.get(scope, "sharepoint:tid-1") is None


def test_pg_source_sync_store():
    dsn = os.environ.get("DBSEARCH_SYNCSTATE_TEST_DSN", "")
    if not dsn:
        print("  SKIP  PgSourceSyncStore SQL - set DBSEARCH_SYNCSTATE_TEST_DSN to prove it "
              "(said out loud rather than skipped quietly: an unrun SQL path is unproven)")
        return
    import psycopg

    table = f"source_sync_state_t{uuid.uuid4().hex[:8]}"
    store = PgSourceSyncStore(dsn, table=table)
    try:
        _pg_contract(store, "selfhost")
        # And the registry end to end over real SQL, which is the claim the card makes.
        reg = SourceRegistry(store=store, scope="selfhost")
        reg.register(_desc(job_tenant="selfhost"))
        reg.record_sync("sharepoint:tid-1", cursor="delta-pg", doc_count=7,
                        at="2026-08-21T01:00:00Z", unreadable=1)
        reg2 = SourceRegistry(store=store, scope="selfhost")
        reg2.register(_desc(job_tenant="selfhost"))
        d = reg2.get("sharepoint:tid-1")
        assert (d.doc_count, d.cursor, d.unreadable) == (7, "delta-pg", 1), d
        print(f"  PG    round trip green on {table}")
    finally:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {table}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok    {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
