"""#107 (azure_sql slice) — AzureSqlEngine + AzureSqlProvider: cloud pushdown as a
SqlEnginePort sibling of SqliteEngine (ADR 0007), proven WITHOUT network or pymssql
(a fake DB-API connection is injected). The guard/audit/NL2SQL stay in
FederatedSqlStore — the engine only introspects INFORMATION_SCHEMA and executes.

Run: python3 tests/selftest_router_azure_sql.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router import AzureSqlEngine, AzureSqlProvider  # noqa: E402
from dbsearch.router.store import FEDERATED_SQL  # noqa: E402


_SCHEMA_ROWS = [
    # (TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE) — the schema is NOT optional: a
    # real database puts tables in named schemas (AdventureWorksLT -> SalesLT), and a bare
    # name is not queryable there (`FROM Product` -> Invalid object name). #203
    ("dbo", "notes", "body", "nvarchar"),
    ("dbo", "sales", "id", "int"),
    ("dbo", "sales", "region", "nvarchar"),
    ("dbo", "sales", "amount", "decimal"),
    ("SalesLT", "Product", "ProductID", "int"),
    ("SalesLT", "Product", "ListPrice", "money"),
]
_DATA = (["region", "total_amount"], [("apac", 205000.0), ("emea", 125000.0)])


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql, params=()):
        if self._conn.broken:
            raise RuntimeError("the connection has been severed (auto-pause)")
        self._conn.executed.append(sql)
        if "INFORMATION_SCHEMA" in sql:
            self.description = [("TABLE_SCHEMA",), ("TABLE_NAME",), ("COLUMN_NAME",),
                                ("DATA_TYPE",)]
            self._rows = list(_SCHEMA_ROWS)
        else:
            self.description = [(c,) for c in _DATA[0]]
            self._rows = list(_DATA[1])

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, broken=False):
        self.broken = broken
        self.closed = False
        self.executed = []

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


def test_schema_groups_information_schema_rows():
    eng = AzureSqlEngine(lambda: _FakeConn())
    schema = eng.schema()
    # #203: names are SCHEMA-QUALIFIED. The generators put schema[i]["table"] straight into
    # `FROM ...`, so a bare name here is SQL that cannot execute outside the default schema.
    assert [t["table"] for t in schema] == ["dbo.notes", "dbo.sales", "SalesLT.Product"], schema
    sales = next(t for t in schema if t["table"] == "dbo.sales")
    assert sales["columns"] == [{"name": "id", "type": "int"},
                                {"name": "region", "type": "nvarchar"},
                                {"name": "amount", "type": "decimal"}], sales


def test_schema_honors_table_allowlist():
    eng = AzureSqlEngine(lambda: _FakeConn(), tables=["SALES"])   # case-insensitive
    schema = eng.schema()
    # a BARE allowlist entry still works — existing stores.yml configs keep their meaning
    assert [t["table"] for t in schema] == ["dbo.sales"], schema


def test_schema_allowlist_accepts_qualified_names():
    """#203: the natural thing to write for a real DB is the qualified name — and it was
    silently matching nothing, emptying the schema and reporting the store as degraded."""
    eng = AzureSqlEngine(lambda: _FakeConn(), tables=["SalesLT.Product"])
    assert [t["table"] for t in eng.schema()] == ["SalesLT.Product"]


def test_schema_allowlist_does_not_confuse_same_name_in_two_schemas():
    """A bare entry must not silently pull in a same-named table from another schema."""
    rows = _SCHEMA_ROWS + [("audit", "sales", "id", "int")]
    conn = _FakeConn()
    eng = AzureSqlEngine(lambda: conn, tables=["audit.sales"])
    global _SCHEMA_ROWS_PATCH
    orig = list(_SCHEMA_ROWS)
    _SCHEMA_ROWS[:] = rows
    try:
        assert [t["table"] for t in eng.schema()] == ["audit.sales"]
    finally:
        _SCHEMA_ROWS[:] = orig


def test_schema_is_cached_per_engine():
    conn = _FakeConn()
    eng = AzureSqlEngine(lambda: conn)
    eng.schema(); eng.schema()
    assert len([s for s in conn.executed if "INFORMATION_SCHEMA" in s]) == 1, conn.executed


def test_execute_returns_cols_and_rows():
    eng = AzureSqlEngine(lambda: _FakeConn())
    cols, rows = eng.execute("SELECT region, SUM(amount) AS total_amount FROM sales "
                             "GROUP BY region")
    assert cols == ["region", "total_amount"], cols
    assert rows == [("apac", 205000.0), ("emea", 125000.0)], rows


def test_execute_reconnects_once_after_severed_connection():
    # Serverless auto-pause severs idle connections: the FIRST statement after resume
    # fails on the dead conn — the engine must reconnect ONCE and retry (reads only,
    # so a double execute is harmless), never surface the pause to the caller.
    dead, live = _FakeConn(broken=True), _FakeConn()
    conns = [dead, live]
    eng = AzureSqlEngine(lambda: conns.pop(0))
    cols, rows = eng.execute("SELECT region FROM sales")
    assert dead.closed and live.executed, (dead.closed, live.executed)
    assert cols and rows, (cols, rows)


def test_connect_retries_while_serverless_resumes():
    # A PAUSED free-tier DB refuses the connection fast (error 40613) — and that very
    # attempt triggers the auto-resume. The engine must keep retrying the connect
    # until the DB wakes, instead of surfacing the pause to compose/ask.
    attempts = []
    good = _FakeConn()

    def factory():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("Database 'd' on server 's' is not currently available. "
                               "Error 40613. Please retry the connection later.")
        return good

    eng = AzureSqlEngine(factory, resume_wait=0)          # no real sleeping in tests
    cols, rows = eng.execute("SELECT region FROM sales")
    assert len(attempts) == 3 and good.executed, (attempts, good.executed)
    assert cols and rows

    def always_down():
        raise RuntimeError("login failed for user")       # NOT a pause -> no retry loop

    try:
        AzureSqlEngine(always_down, resume_wait=0).execute("SELECT 1")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "login failed" in str(e), e


def test_delegated_execute_uses_per_credential_connection():
    # Phase H (#131): a delegated credential (AAD access token from the E5 broker)
    # must open its OWN connection as that user — Azure SQL's permissions then
    # enforce source-side — and NEVER fall back to the service (SQL-auth) conn.
    service = _FakeConn()
    user_conns = {}

    def user_connect(token):
        user_conns.setdefault(token, []).append(_FakeConn())
        return user_conns[token][-1]

    eng = AzureSqlEngine(lambda: service, user_connect=user_connect)
    cols, rows = eng.execute("SELECT region FROM sales", credential="tok-alice")
    assert cols and rows
    assert not service.executed, service.executed          # service conn untouched
    assert user_conns["tok-alice"][0].executed
    # same credential -> cached connection, no reconnect
    eng.execute("SELECT region FROM sales", credential="tok-alice")
    assert len(user_conns["tok-alice"]) == 1, user_conns
    # a different user's token -> a different connection
    eng.execute("SELECT region FROM sales", credential="tok-bob")
    assert len(user_conns["tok-bob"]) == 1 and len(user_conns) == 2, user_conns


def test_delegated_fails_closed_without_user_connect():
    # No pyodbc path wired -> the delegated ask must FAIL CLOSED (LAW 2), never
    # silently run as the service account.
    eng = AzureSqlEngine(lambda: _FakeConn())
    try:
        eng.execute("SELECT 1", credential="tok")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "failing closed" in str(e), e


def test_delegated_connect_retries_through_resume():
    # the 40613 auto-resume dance applies to the delegated connect too
    attempts = []
    good = _FakeConn()

    def user_connect(token):
        attempts.append(token)
        if len(attempts) < 3:
            raise RuntimeError("Error 40613. not currently available")
        return good

    eng = AzureSqlEngine(lambda: _FakeConn(), user_connect=user_connect,
                         resume_wait=0)
    cols, rows = eng.execute("SELECT region FROM sales", credential="tok")
    assert len(attempts) == 3 and good.executed, (attempts, good.executed)
    assert cols and rows


def test_aad_token_struct_packs_utf16_with_length():
    # SQL_COPT_SS_ACCESS_TOKEN (1256) wants <uint32 byte-length><UTF-16-LE token>
    from dbsearch.router.providers.azure_sql import _aad_token_struct

    packed = _aad_token_struct("ab")
    expected_body = "ab".encode("utf-16-le")
    assert packed[:4] == len(expected_body).to_bytes(4, "little"), packed[:4]
    assert packed[4:] == expected_body, packed[4:]


def test_from_config_wires_the_delegated_path():
    eng = AzureSqlEngine.from_config({"server": "s", "database": "d",
                                      "user": "u", "password": "p"})
    assert eng._user_connect is not None    # pyodbc factory present (lazy import)


def test_from_config_requires_connection_fields():
    try:
        AzureSqlEngine.from_config({"server": "s", "database": "d"})
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "user" in str(e) and "password" in str(e), e


def test_provider_builds_federated_store_with_pushdown_mode():
    assert AzureSqlProvider.kind == "azure_sql"
    assert tuple(AzureSqlProvider.modes) == ("pushdown",)
    prov = AzureSqlProvider(engine_factory=lambda cfg: AzureSqlEngine(lambda: _FakeConn(),
                                                                      tables=cfg.get("tables")))
    store = prov.build({"id": "sales-az", "business_unit": "sales", "title": "Sales DW",
                        "description": "revenue by region", "tables": ["sales"]})
    p = store.profile()
    assert p.kind == FEDERATED_SQL and p.store_id == "sales-az", p
    assert [t["table"] for t in p.schema] == ["dbo.sales"], p.schema
    # probe goes through the same construction (freshness 'live', real schema)
    assert prov.probe({"id": "x", "tables": ["sales"]}).freshness == "live"


def main():
    print("#107 azure_sql engine+provider self-test:")
    test_schema_groups_information_schema_rows()
    test_schema_honors_table_allowlist()
    test_schema_is_cached_per_engine()
    test_execute_returns_cols_and_rows()
    print("  PASS  schema introspection (+allowlist) / execute cols+rows")
    test_execute_reconnects_once_after_severed_connection()
    test_connect_retries_while_serverless_resumes()
    print("  PASS  auto-pause resilience: reconnect-once on severed conn / "
          "retry-connect through 40613 resume")
    test_delegated_execute_uses_per_credential_connection()
    test_delegated_fails_closed_without_user_connect()
    test_delegated_connect_retries_through_resume()
    test_aad_token_struct_packs_utf16_with_length()
    test_from_config_wires_the_delegated_path()
    print("  PASS  delegated query-as-user: per-credential conns / fail-closed / "
          "resume-retry / AAD token struct (#131)")
    test_from_config_requires_connection_fields()
    test_provider_builds_federated_store_with_pushdown_mode()
    print("  PASS  config validation / provider -> FederatedSqlStore (pushdown)")
    print("\n#107 AZURE_SQL SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
