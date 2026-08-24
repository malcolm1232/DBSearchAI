"""#107 (redshift slice) — RedshiftEngine + RedshiftProvider: Redshift Serverless via
the Data API as a pushdown SqlEnginePort sibling (ADR 0007), proven WITHOUT network
or boto3 (a fake redshift-data client is injected). The Data API is async — the
engine polls to completion and decodes the typed record fields.

Run: python3 tests/selftest_router_redshift.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router import RedshiftEngine, RedshiftProvider  # noqa: E402
from dbsearch.router.store import FEDERATED_SQL  # noqa: E402


def _rec(*vals):
    out = []
    for v in vals:
        if v is None:
            out.append({"isNull": True})
        elif isinstance(v, bool):
            out.append({"booleanValue": v})
        elif isinstance(v, int):
            out.append({"longValue": v})
        elif isinstance(v, float):
            out.append({"doubleValue": v})
        else:
            out.append({"stringValue": v})
    return out


class _FakeDataApi:
    """Scripted redshift-data client: execute_statement -> id; describe polls through
    the given statuses; get_statement_result returns typed records."""

    def __init__(self, statuses=("FINISHED",), error=""):
        self.statuses = list(statuses)
        self.error = error
        self.executed = []

    def execute_statement(self, *, WorkgroupName, Database, Sql):
        self.executed.append({"wg": WorkgroupName, "db": Database, "sql": Sql})
        return {"Id": f"stmt-{len(self.executed)}"}

    def describe_statement(self, *, Id):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        d = {"Status": status, "HasResultSet": True}
        if self.error:
            d["Error"] = self.error
        return d

    def get_statement_result(self, *, Id):
        sql = self.executed[-1]["sql"]
        if "information_schema" in sql:
            # (table_schema, table_name, ...) — #214: a real warehouse uses named schemas,
            # and hard-coding 'public' made every one of them invisible.
            return {"ColumnMetadata": [{"name": "table_schema"}, {"name": "table_name"},
                                       {"name": "column_name"}, {"name": "data_type"}],
                    "Records": [_rec("public", "notes", "body", "character varying"),
                                _rec("public", "sales", "id", "integer"),
                                _rec("public", "sales", "region", "character varying"),
                                _rec("public", "sales", "amount", "numeric"),
                                _rec("finance", "sales", "id", "integer")]}
        return {"ColumnMetadata": [{"name": "region"}, {"name": "total_amount"}],
                "Records": [_rec("apac", 205000.0), _rec("emea", None)]}


def _eng(client=None, tables=None):
    client = client or _FakeDataApi()
    return RedshiftEngine(lambda: client, workgroup="dbsearch-wg", database="dev",
                          tables=tables, poll_interval=0), client


def test_schema_groups_information_schema_rows():
    eng, client = _eng()
    schema = eng.schema()
    assert [t["table"] for t in schema] == ["public.notes", "public.sales",
                                            "finance.sales"], schema
    assert client.executed[0]["wg"] == "dbsearch-wg" and client.executed[0]["db"] == "dev"
    sales = next(t for t in schema if t["table"] == "public.sales")
    assert {"name": "region", "type": "character varying"} in sales["columns"], sales


def test_schema_honors_table_allowlist():
    eng, _ = _eng(tables=["SALES"])
    # a BARE entry means the DEFAULT schema only — it must NOT also pull in finance.sales,
    # which is a different table (widening the store that way would be a LAW-2 leak)
    assert [t["table"] for t in eng.schema()] == ["public.sales"]


def test_schema_allowlist_accepts_a_qualified_name():
    """#214: 'finance.sales' is the natural thing to write for a real warehouse, and it used
    to match nothing — silently emptying the schema."""
    eng, _ = _eng(tables=["finance.sales"])
    assert [t["table"] for t in eng.schema()] == ["finance.sales"]


def test_schema_is_cached_per_engine():
    # every ask calls schema() (SQL generation + guard) — one cloud round trip per
    # ENGINE lifetime, not per query; a recompose builds a fresh engine anyway
    eng, client = _eng()
    eng.schema(); eng.schema()
    assert len(client.executed) == 1, client.executed


def test_execute_polls_and_decodes_typed_records():
    eng, _ = _eng(_FakeDataApi(statuses=("SUBMITTED", "STARTED", "FINISHED")))
    cols, rows = eng.execute("SELECT region, SUM(amount) AS total_amount FROM sales "
                             "GROUP BY region")
    assert cols == ["region", "total_amount"], cols
    assert rows == [("apac", 205000.0), ("emea", None)], rows   # typed + NULL decode


def test_execute_surfaces_statement_failure():
    eng, _ = _eng(_FakeDataApi(statuses=("FAILED",), error='relation "x" does not exist'))
    try:
        eng.execute("SELECT * FROM x")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "does not exist" in str(e), e


def test_from_config_requires_workgroup_and_database():
    try:
        RedshiftEngine.from_config({"workgroup": "wg"})
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "database" in str(e), e


def test_provider_builds_federated_store():
    assert RedshiftProvider.kind == "redshift"
    assert tuple(RedshiftProvider.modes) == ("pushdown",)
    prov = RedshiftProvider(engine_factory=lambda cfg: _eng(tables=cfg.get("tables"))[0])
    store = prov.build({"id": "sales-rs", "business_unit": "sales",
                        "description": "revenue", "tables": ["sales"]})
    p = store.profile()
    assert p.kind == FEDERATED_SQL and [t["table"] for t in p.schema] == ["public.sales"], p


def main():
    print("#107 redshift engine+provider self-test:")
    test_schema_groups_information_schema_rows()
    test_schema_honors_table_allowlist()
    test_schema_allowlist_accepts_a_qualified_name()
    test_schema_is_cached_per_engine()
    print("  PASS  information_schema introspection (+allowlist, cached)")
    test_execute_polls_and_decodes_typed_records()
    test_execute_surfaces_statement_failure()
    print("  PASS  async poll to FINISHED / typed+NULL decode / FAILED surfaces error")
    test_from_config_requires_workgroup_and_database()
    test_provider_builds_federated_store()
    print("  PASS  config validation / provider -> FederatedSqlStore (pushdown)")
    print("\n#107 REDSHIFT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
