"""#107 (databricks slice) — DatabricksEngine + DatabricksProvider: a Databricks SQL
warehouse as a pushdown SqlEnginePort sibling (ADR 0007), proven WITHOUT network or
databricks-sql-connector (a fake DB-API connection is injected).

Run: python3 tests/selftest_router_databricks.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router import DatabricksEngine, DatabricksProvider  # noqa: E402
from dbsearch.router.store import FEDERATED_SQL  # noqa: E402


_SCHEMA_ROWS = [
    ("notes", "body", "STRING"),
    ("sales", "id", "INT"),
    ("sales", "region", "STRING"),
    ("sales", "amount", "DECIMAL(12,2)"),
]
_DATA = (["region", "total_amount"], [("apac", 205000.0), ("emea", 125000.0)])


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql):
        self._conn.executed.append(sql)
        if "information_schema" in sql:
            self.description = [("table_name",), ("column_name",), ("data_type",)]
            self._rows = list(_SCHEMA_ROWS)
        else:
            self.description = [(c,) for c in _DATA[0]]
            self._rows = list(_DATA[1])

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self):
        self.executed = []
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


def test_schema_scoped_to_catalog_and_schema():
    conn = _FakeConn()
    eng = DatabricksEngine(lambda: conn, catalog="workspace", schema="default")
    out = eng.schema()
    assert [t["table"] for t in out] == ["notes", "sales"], out
    sales = next(t for t in out if t["table"] == "sales")
    assert {"name": "region", "type": "STRING"} in sales["columns"], sales
    # introspection reads the CATALOG's information_schema, filtered to the schema
    q = conn.executed[0]
    assert "workspace.information_schema.columns" in q and "'default'" in q, q


def test_schema_allowlist_and_cache():
    conn = _FakeConn()
    eng = DatabricksEngine(lambda: conn, catalog="c", schema="s", tables=["SALES"])
    assert [t["table"] for t in eng.schema()] == ["sales"]
    eng.schema()
    assert len(conn.executed) == 1, conn.executed      # cached per engine lifetime


def test_execute_returns_cols_and_rows():
    eng = DatabricksEngine(lambda: _FakeConn(), catalog="c", schema="s")
    cols, rows = eng.execute("SELECT region, SUM(amount) AS total_amount FROM sales "
                             "GROUP BY region")
    assert cols == ["region", "total_amount"] and rows == _DATA[1], (cols, rows)


def test_from_config_requires_connection_fields():
    try:
        DatabricksEngine.from_config({"host": "h"})
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "http_path" in str(e) and "token" in str(e), e


def test_provider_builds_federated_store():
    assert DatabricksProvider.kind == "databricks"
    assert tuple(DatabricksProvider.modes) == ("pushdown",)
    prov = DatabricksProvider(engine_factory=lambda cfg: DatabricksEngine(
        lambda: _FakeConn(), catalog="c", schema="s", tables=cfg.get("tables")))
    store = prov.build({"id": "sales-dbx", "business_unit": "sales",
                        "description": "revenue", "tables": ["sales"]})
    p = store.profile()
    assert p.kind == FEDERATED_SQL and [t["table"] for t in p.schema] == ["sales"], p


def main():
    print("#107 databricks engine+provider self-test:")
    test_schema_scoped_to_catalog_and_schema()
    test_schema_allowlist_and_cache()
    test_execute_returns_cols_and_rows()
    print("  PASS  catalog/schema-scoped introspection (+allowlist, cached) / execute")
    test_from_config_requires_connection_fields()
    test_provider_builds_federated_store()
    print("  PASS  config validation / provider -> FederatedSqlStore (pushdown)")
    print("\n#107 DATABRICKS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
