"""#572 - ADR 0013: accounts and identities are rows, not implications.

    PYTHONPATH=src python3 tests/selftest_572_account_store.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.server.accounts import (  # noqa: E402
    AccountStoreUnavailable, InMemoryAccountStore, PgAccountStore,
)


def test_first_signin_creates_an_account():
    s = InMemoryAccountStore()
    acc = s.resolve("entra", "oid-1", preferred_account_id="oid-1")
    assert acc == "oid-1"
    row = s.get("oid-1")
    assert row and row["created_at"] and row["last_seen"]


def test_second_signin_finds_the_same_account_and_touches_last_seen():
    s = InMemoryAccountStore()
    s.resolve("entra", "oid-1", preferred_account_id="oid-1")
    first = s.get("oid-1")["last_seen"]
    import time; time.sleep(0.01)
    assert s.resolve("entra", "oid-1") == "oid-1"
    assert s.get("oid-1")["last_seen"] >= first


def test_a_new_account_without_preferred_id_gets_an_opaque_one():
    s = InMemoryAccountStore()
    acc = s.resolve("local", "someone@example.com")
    assert acc.startswith("acct_") and len(acc) > 10


def test_link_attaches_a_second_identity_to_an_existing_account():
    s = InMemoryAccountStore()
    s.resolve("entra", "oid-1", preferred_account_id="oid-1")
    assert s.link("google", "gsub-9", "oid-1") == "oid-1"
    # the linked identity now signs in to the SAME account (ADR 0013 decision 4)
    assert s.resolve("google", "gsub-9") == "oid-1"


def test_link_never_repoints_an_already_mapped_identity():
    s = InMemoryAccountStore()
    s.resolve("google", "gsub-9", preferred_account_id="g@example.com")
    assert s.link("google", "gsub-9", "oid-ATTACKER") == "g@example.com"
    assert s.resolve("google", "gsub-9") == "g@example.com"


def test_pg_store_unreachable_raises_unavailable():
    """Mirrors selftest_manifest_store.py's twin for PgManifestStore: every op must fail
    CLOSED (AccountStoreUnavailable), never silently proceed with no account recorded, when
    Postgres cannot be reached. No live Postgres needed - the DSN targets a port nothing is
    listening on."""
    s = PgAccountStore("postgresql://nobody:x@127.0.0.1:1/none", connect_timeout=1)
    for op in (lambda: s.resolve("entra", "oid-x", preferred_account_id="oid-x"),
               lambda: s.link("google", "gsub-x", "oid-x"),
               lambda: s.get("oid-x")):
        try:
            op()
            raise AssertionError("expected AccountStoreUnavailable")
        except AccountStoreUnavailable:
            pass


def test_store_error_never_carries_driver_text():
    """Same guard as manifest_store.py's `test_store_error_never_carries_manifest_content`:
    the raised message must be the exception CLASS name only (plus a SQLSTATE, if the driver
    supplies one) - never psycopg's own text, which can quote the DSN, host, or (for a write)
    a fragment of the payload. Also no live Postgres needed."""
    s = PgAccountStore("postgresql://nobody:x@127.0.0.1:1/none", connect_timeout=1)
    caught = None
    try:
        s.get("oid-x")
        raise AssertionError("expected AccountStoreUnavailable")
    except AccountStoreUnavailable as exc:
        caught = exc
    rendered = f"{caught}{caught.args}{caught.__cause__!r}{caught.__context__!r}"
    assert "127.0.0.1" not in rendered and "nobody" not in rendered, (
        f"the store error leaked connection details: {rendered}")
    assert caught.__cause__ is None and caught.__context__ is None, (
        "the driver error is still reachable from this exception (__cause__/__context__) - "
        "a traceback render could still print its message")
    assert re.fullmatch(r"[A-Za-z_]+Error( \(sqlstate [^)]*\))?", str(caught)), (
        f"expected a bare exception class name (+ optional sqlstate), got: {caught!r}")


def test_ddl_failure_leaves_the_schema_flag_unset():
    """Hermetic twin of manifest_store.py's identical guard, ported to accounts.py's own
    `_ensure_schema`/`_schema_done` pair: whatever the failure, the flag must never be set on
    a path where the CREATE TABLE did not commit - manifest_store.py's docstring explains why
    that used to poison the whole process (one failed op -> every later call skips the DDL
    and dies on `relation ... does not exist`, forever, for every user)."""
    class Boom(Exception):
        pass

    class FailingConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            raise Boom("DDL refused")

    s = PgAccountStore("postgresql://unused/none")
    s._conn = lambda: FailingConn()          # noqa: SLF001 - that is the seam under test
    for _ in range(2):
        try:
            s.get("oid-a")
            raise AssertionError("expected AccountStoreUnavailable")
        except AccountStoreUnavailable:
            pass
        assert s._schema_done is False, (      # noqa: SLF001
            "the schema flag was set even though the DDL failed - every later call would "
            "skip the CREATE TABLE and fail forever")


def test_google_signin_after_linking_lands_on_the_linked_account():
    """#442: Monday-Microsoft + Tuesday-Google is ONE workspace once linked."""
    from dbsearch.server import app as appmod
    from dbsearch.server.accounts import InMemoryAccountStore
    saved = appmod.ACCOUNTS
    appmod.ACCOUNTS = InMemoryAccountStore()
    try:
        appmod.ACCOUNTS.resolve("entra", "oid-ms", preferred_account_id="oid-ms")
        appmod.ACCOUNTS.link("google", "gsub-1", "oid-ms")
        assert appmod.ACCOUNTS.resolve("google", "gsub-1") == "oid-ms"
    finally:
        appmod.ACCOUNTS = saved


def test_unlinked_google_signin_provisions_a_fresh_account():
    from dbsearch.server.accounts import InMemoryAccountStore
    s = InMemoryAccountStore()
    acc = s.resolve("google", "gsub-new", preferred_account_id="new@example.com")
    assert acc == "new@example.com"
    assert s.get(acc) is not None


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
