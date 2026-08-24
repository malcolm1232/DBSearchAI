"""#368: user manifests persist server-side so a workspace survives restarts.

    PYTHONPATH=src python3 tests/selftest_manifest_store.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.server.manifest_store import (  # noqa: E402
    InMemoryManifestStore, ManifestStoreUnavailable, PgManifestStore,
)

MANIFEST = {"tenant": "acme", "stores": [
    {"id": "pg-1", "kind": "postgres", "business_unit": "eng",
     "acl": ["oid-a"], "config": {"host": "h", "password": "secret://acme/oid-a/pg-1/password"}}]}

# The easiest real trigger for a failed write, and a live bug (#367): jsonb rejects a NUL byte,
# so this manifest cannot be stored no matter how healthy the database is.
NUL_MANIFEST = {"tenant": "acme", "stores": [
    {"id": "nul-1", "kind": "local", "business_unit": "eng", "acl": ["oid-a"],
     "config": {"description": "a NUL byte sneaks in here: \x00 - jsonb refuses it"}}]}


def _drop(dsn: str, table: str) -> None:
    import psycopg
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def test_inmemory_round_trip():
    s = InMemoryManifestStore()
    assert s.get("oid-a") is None
    s.put("oid-a", MANIFEST)
    assert s.get("oid-a") == MANIFEST
    assert s.get("oid-b") is None, "one owner's manifest must never serve another's"
    s.delete("oid-a")
    assert s.get("oid-a") is None
    print("  PASS  in-memory round trip, per-owner keyed")


def test_put_replaces():
    s = InMemoryManifestStore()
    s.put("oid-a", MANIFEST)
    s.put("oid-a", {"tenant": "acme", "stores": []})
    assert s.get("oid-a")["stores"] == []
    print("  PASS  put is an upsert (last write wins)")


def test_pg_store_unreachable_raises_unavailable():
    s = PgManifestStore("postgresql://nobody:x@127.0.0.1:1/none",
                        connect_timeout=1)
    for op in (lambda: s.get("oid-a"), lambda: s.put("oid-a", MANIFEST),
               lambda: s.delete("oid-a")):
        try:
            op()
            raise AssertionError("expected ManifestStoreUnavailable")
        except ManifestStoreUnavailable:
            pass
    print("  PASS  configured-but-unreachable PG raises ManifestStoreUnavailable (fail closed)")


def test_pg_round_trip_if_dsn_present():
    dsn = os.environ.get("PGVECTOR_DSN", "")
    if not dsn:
        print("  SKIP  PGVECTOR_DSN unset - pg round trip needs a live Postgres")
        return
    s = PgManifestStore(dsn, table="user_manifests_selftest")
    s.put("oid-a", MANIFEST)
    assert s.get("oid-a") == MANIFEST
    s.delete("oid-a")
    assert s.get("oid-a") is None
    print("  PASS  pg round trip against live Postgres")


def test_pg_first_operation_failure_does_not_poison_the_store():
    """#368 final review, CRITICAL 1. `_schema_done = True` used to be set INSIDE the
    operation's transaction. psycopg's `with conn` rolls back on exception and PostgreSQL
    DDL is transactional, so a FAILED first operation rolled the CREATE TABLE back while the
    flag stayed True - and every later call skipped the DDL and died on
    `relation "..." does not exist`, permanently, for every user (app.py holds one
    process-wide store; the router maps the failure to a 503 on /catalog, /ask, /route,
    /compose, /probe, /health, /setup/turn and /manifest).

    Drive it exactly as production would hit it: a manifest carrying a NUL byte (#367) as
    the process's very FIRST manifest-store operation, then a perfectly ordinary write."""
    dsn = os.environ.get("PGVECTOR_DSN", "")
    if not dsn:
        print("  SKIP  PGVECTOR_DSN unset - the poisoning test needs a live Postgres")
        return
    table = "user_manifests_poison_selftest"
    _drop(dsn, table)                      # a genuinely cold store: the DDL has to run
    s = PgManifestStore(dsn, table=table)

    try:
        s.put("oid-a", NUL_MANIFEST)
        raise AssertionError("a NUL byte in a manifest must be rejected by jsonb")
    except ManifestStoreUnavailable as exc:
        first = str(exc)

    # ...and now the store must still work. Before the fix this raised
    # ManifestStoreUnavailable('relation "user_manifests_poison_selftest" does not exist').
    s.put("oid-b", MANIFEST)
    assert s.get("oid-b") == MANIFEST, "one failed write poisoned every later operation"
    s.put("oid-a", MANIFEST)               # the same owner recovers too
    assert s.get("oid-a") == MANIFEST
    s.delete("oid-a")
    s.delete("oid-b")
    _drop(dsn, table)
    print("  PASS  a failed FIRST operation does not poison the store (schema flag is only "
          f"set once the DDL has committed); its reason was {first!r}")


def test_store_error_never_carries_manifest_content():
    """The same failure, viewed as a leak: a psycopg message can quote the offending value,
    and a manifest's `delegation.client_secret` is exactly such a value. The exception must
    carry a class name (and at most a SQLSTATE) - never the payload, and never a __cause__
    whose traceback would print the driver's message for it."""
    dsn = os.environ.get("PGVECTOR_DSN", "")
    if not dsn:
        print("  SKIP  PGVECTOR_DSN unset - needs a live Postgres to produce a real "
              "driver error")
        return
    table = "user_manifests_leak_selftest"
    _drop(dsn, table)
    secretish = "TOPSECRET-client-secret-9999"
    manifest = {"tenant": "acme", "stores": [
        {"id": "s1", "kind": "azure_sql", "business_unit": "eng", "acl": ["oid-a"],
         "config": {"description": "trailing NUL \x00"},
         "delegation": {"kind": "entra_refresh", "client_secret": secretish}}]}
    s = PgManifestStore(dsn, table=table)
    caught = None
    try:
        s.put("oid-a", manifest)
        raise AssertionError("expected the NUL byte to be rejected")
    except ManifestStoreUnavailable as exc:
        caught = exc
    # Everything a caller could render: the message, the args, and BOTH chaining slots a
    # traceback would follow.
    rendered = f"{caught}{caught.args}{caught.__cause__!r}{caught.__context__!r}"
    assert secretish not in rendered, f"THE STORE ERROR CARRIED THE SECRET: {rendered}"
    assert "\\u0000" not in rendered and "NUL" not in rendered, \
        f"the store error echoes manifest content: {rendered}"
    assert caught.__cause__ is None and caught.__context__ is None, (
        "the driver error is still reachable from this exception (__cause__/__context__) - "
        "a traceback render would print its message, manifest fragment included")
    _drop(dsn, table)
    print("  PASS  a store failure reports its exception class only - no manifest content, "
          "no chained driver message")


def test_ddl_failure_leaves_the_schema_flag_unset():
    """Hermetic twin of the poisoning test: whatever the failure, the flag must never be set
    on a path where the CREATE TABLE did not commit. Fails the DDL itself, which no live
    Postgres will do on demand."""
    class Boom(Exception):
        pass

    class FailingConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            raise Boom("DDL refused")

    s = PgManifestStore("postgresql://unused/none")
    s._conn = lambda: FailingConn()          # noqa: SLF001 - that is the seam under test
    for _ in range(2):
        try:
            s.get("oid-a")
            raise AssertionError("expected ManifestStoreUnavailable")
        except ManifestStoreUnavailable:
            pass
        assert s._schema_done is False, (      # noqa: SLF001
            "the schema flag was set even though the DDL failed - every later call would "
            "skip the CREATE TABLE and fail forever")
    print("  PASS  a failed DDL leaves the schema flag unset, so the next call retries it")


if __name__ == "__main__":
    test_inmemory_round_trip()
    test_put_replaces()
    test_pg_store_unreachable_raises_unavailable()
    test_pg_round_trip_if_dsn_present()
    test_pg_first_operation_failure_does_not_poison_the_store()
    test_store_error_never_carries_manifest_content()
    test_ddl_failure_leaves_the_schema_flag_unset()
    print("OK selftest_manifest_store")
