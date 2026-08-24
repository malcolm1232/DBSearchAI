"""#159 (synapse slice) — SynapseProvider reuses AzureSqlEngine verbatim (Synapse's dedicated
SQL pool is the same TDS/T-SQL/INFORMATION_SCHEMA as Azure SQL). This proves the provider
declares its own kind, still builds a FederatedSqlStore via the shared engine, and introspects
+ runs pushdown against a FAKE DB-API connection (no network, no pymssql).

Run: python3 tests/selftest_router_synapse.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router import AzureSqlEngine, SynapseProvider  # noqa: E402
from dbsearch.router.providers.azure_sql import AzureSqlProvider  # noqa: E402
from dbsearch.router.store import FEDERATED_SQL  # noqa: E402


_SCHEMA_ROWS = [
    # (TABLE_SCHEMA, TABLE_NAME, ...) — Synapse rides the same AzureSqlEngine, whose schema
    # discovery now keeps the schema qualifier (#203); a bare name isn't queryable off dbo.
    ("dbo", "sales_fact", "region", "nvarchar"),
    ("dbo", "sales_fact", "quarter", "nvarchar"),
    ("dbo", "sales_fact", "revenue", "decimal"),
]
_DATA = (["region", "total"], [("apac", 900000.0), ("emea", 640000.0)])


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql, params=()):
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
    def __init__(self):
        self.executed = []

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        pass


def test_kind_is_synapse_but_engine_is_shared():
    assert SynapseProvider.kind == "synapse"
    assert tuple(SynapseProvider.modes) == ("pushdown",)
    # it IS an AzureSqlProvider — the whole TDS engine + delegated path is inherited
    assert issubclass(SynapseProvider, AzureSqlProvider)
    assert SynapseProvider().kind != AzureSqlProvider().kind   # distinct catalog identity


def test_provider_builds_federated_store_over_the_tds_engine():
    prov = SynapseProvider(engine_factory=lambda cfg: AzureSqlEngine(lambda: _FakeConn(),
                                                                     tables=cfg.get("tables")))
    store = prov.build({"id": "wh-syn", "business_unit": "sales", "title": "Sales DW",
                        "description": "sales facts by region/quarter", "tables": ["sales_fact"]})
    p = store.profile()
    assert p.kind == FEDERATED_SQL and p.store_id == "wh-syn", p
    assert [t["table"] for t in p.schema] == ["dbo.sales_fact"], p.schema
    assert prov.probe({"id": "x", "tables": ["sales_fact"]}).freshness == "live"


def test_shared_engine_pushdown_executes_and_returns_rows():
    # the shared AzureSqlEngine round-trips a pushdown query (cols+rows) — Synapse uses it as-is
    eng = AzureSqlEngine(lambda: _FakeConn())
    cols, rows = eng.execute(
        "SELECT region, SUM(revenue) AS total FROM sales_fact GROUP BY region")
    assert cols == ["region", "total"], cols
    assert rows == [("apac", 900000.0), ("emea", 640000.0)], rows


def test_provider_forces_use_odbc_for_synapse():
    # Synapse rejects USE; SynapseProvider must inject use_odbc so the shared engine takes the
    # pyodbc SQL-login path (no USE). We capture the config the engine_factory receives.
    seen = {}
    def factory(cfg):
        seen.update(cfg)
        return AzureSqlEngine(lambda: _FakeConn())
    SynapseProvider(engine_factory=factory).build(
        {"id": "wh", "business_unit": "sales", "title": "DW", "description": "d"})
    assert seen.get("use_odbc") is True, seen
    # an explicit override is respected (e.g. a non-Synapse endpoint behind kind synapse)
    seen.clear()
    SynapseProvider(engine_factory=factory).build(
        {"id": "wh", "business_unit": "sales", "title": "DW", "description": "d",
         "use_odbc": False})
    assert seen.get("use_odbc") is False, seen


def test_from_config_requires_connection_fields():
    # inherited from AzureSqlEngine — Synapse dedicated pool uses server/db/user/password
    try:
        AzureSqlEngine.from_config({"server": "s", "database": "pool"})
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "user" in str(e) and "password" in str(e), e


def main():
    print("#159 synapse provider self-test:")
    test_kind_is_synapse_but_engine_is_shared()
    test_provider_builds_federated_store_over_the_tds_engine()
    test_shared_engine_pushdown_executes_and_returns_rows()
    test_provider_forces_use_odbc_for_synapse()
    test_from_config_requires_connection_fields()
    print("  PASS  kind=synapse w/ shared AzureSqlEngine / builds FederatedSqlStore / "
          "pushdown cols+rows / config validation")
    print("\n#159 SYNAPSE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
