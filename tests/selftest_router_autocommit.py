"""#226 - every pushdown connection must be AUTOCOMMIT.

The drivers default autocommit=OFF, so a plain SELECT opens a transaction that is never
committed and the connection sits `idle in transaction` on the CUSTOMER's database, holding
locks. Caught live on the fleet: the Postgres connector's own fk_edges() introspection sat
idle-in-transaction for hours and BLOCKED DDL - a DROP SCHEMA queued behind it as
`active | wait: Lock`, and only died when the server was stopped. On a live database that
also blocks VACUUM and pins the write-ahead log.

Every query on this rail is read-only by construction (validate_sql permits only SELECT/WITH),
so there is nothing to commit: autocommit is simply correct.

This drives the REAL connect closures with a fake driver module and asserts what they actually
pass - not a grep of the source, which would pass against a gutted call.

Run: python3 tests/selftest_router_autocommit.py
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.providers.azure_sql import AzureSqlEngine  # noqa: E402
from dbsearch.router.providers.mysql import MySqlEngine  # noqa: E402
from dbsearch.router.providers.postgres import PostgresEngine  # noqa: E402


class _Recorder:
    """Stands in for the driver module. Records the kwargs the connector passes to connect()."""

    def __init__(self):
        self.kwargs = None

    def connect(self, *a, **kw):
        self.kwargs = kw
        return object()          # a connection handle nobody touches in this test


def _with_fake_driver(module_name, fn):
    rec = _Recorder()
    fake = types.ModuleType(module_name)
    fake.connect = rec.connect
    saved = sys.modules.get(module_name)
    sys.modules[module_name] = fake
    try:
        fn()
    finally:
        if saved is not None:
            sys.modules[module_name] = saved
        else:
            sys.modules.pop(module_name, None)
    return rec


def test_postgres_connects_with_autocommit():
    cfg = {"host": "h", "database": "d", "user": "u", "password": "p"}
    rec = _with_fake_driver("psycopg", lambda: PostgresEngine.from_config(cfg)._connect())
    assert rec.kwargs is not None, "connect() was never called"
    assert rec.kwargs.get("autocommit") is True, rec.kwargs


def test_mysql_connects_with_autocommit():
    cfg = {"host": "h", "database": "d", "user": "u", "password": "p"}
    rec = _with_fake_driver("pymysql", lambda: MySqlEngine.from_config(cfg)._connect())
    assert rec.kwargs is not None, "connect() was never called"
    assert rec.kwargs.get("autocommit") is True, rec.kwargs


def test_azure_sql_pymssql_connects_with_autocommit():
    """SynapseProvider reuses THIS engine verbatim, so this covers Synapse too."""
    cfg = {"server": "s", "database": "d", "user": "u", "password": "p"}
    rec = _with_fake_driver("pymssql", lambda: AzureSqlEngine.from_config(cfg)._connect())
    assert rec.kwargs is not None, "connect() was never called"
    assert rec.kwargs.get("autocommit") is True, rec.kwargs


def main():
    test_postgres_connects_with_autocommit()
    test_mysql_connects_with_autocommit()
    test_azure_sql_pymssql_connects_with_autocommit()
    print("  PASS  postgres / mysql / azure_sql(pymssql, and so synapse) all connect AUTOCOMMIT")
    print("\n#226 AUTOCOMMIT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
