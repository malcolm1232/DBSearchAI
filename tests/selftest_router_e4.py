"""Phase E E4 — federated structured store (card #101).
Read-only SQL guard, embedded in-tenant engine (ADR 0007), NL2SQL seam, row-policy
injection (E5 fallback), Evidence rows with {sql, table} provenance, SQL audit — and the
full analytical vertical: "total revenue" routes to the csv store and answers from SUM.
Run: python3 tests/selftest_router_e4.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import ExtractiveLlm, HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch import router  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    CsvSqlProvider, FederatedSqlStore, SqliteEngine, keyword_sql_generator,
    llm_sql_generator, validate_sql,
)
from dbsearch.router.store import ANALYTICAL, FEDERATED_SQL  # noqa: E402

SALES = {"sales": {
    "columns": ["region", "owner", "amount"],
    "rows": [["emea", "alice", 100], ["emea", "bob", 40], ["apac", "alice", 60]],
}}


def test_guard_accepts_readonly_select():
    validate_sql("SELECT region, SUM(amount) FROM sales GROUP BY region", ["sales"])
    validate_sql("WITH t AS (SELECT * FROM sales) SELECT * FROM t", ["sales"])


def test_guard_accepts_schema_qualified_tables():
    """#203: providers now expose SCHEMA-QUALIFIED names (SalesLT.Product), so the generators
    emit `FROM SalesLT.Product`. The guard's table regex used to stop at the dot, capture
    'SalesLT', and reject valid SQL as an invisible table."""
    validate_sql("SELECT COUNT(*) FROM SalesLT.SalesOrderHeader",
                 ["SalesLT.SalesOrderHeader"])
    validate_sql("SELECT c.Name, SUM(d.LineTotal) FROM SalesLT.SalesOrderDetail d "
                 "JOIN SalesLT.Product p ON p.ProductID = d.ProductID "
                 "JOIN SalesLT.ProductCategory c ON c.ProductCategoryID = p.ProductCategoryID "
                 "GROUP BY c.Name",
                 ["SalesLT.SalesOrderDetail", "SalesLT.Product", "SalesLT.ProductCategory"])
    validate_sql("SELECT * FROM [dbo].[sales]", ["dbo.sales"])      # bracket-quoted


def test_guard_rejects_other_schema_with_a_visible_table_name():
    """The dot must not become a bypass: `evil.sales` is a DIFFERENT table from `dbo.sales`,
    and must not be admitted just because the bare name 'sales' is visible."""
    for sql, visible in [
        ("SELECT * FROM evil.sales", ["dbo.sales"]),
        ("SELECT * FROM audit.Product", ["SalesLT.Product"]),
        ("SELECT * FROM SalesLT.Customer", ["SalesLT.Product"]),
    ]:
        try:
            validate_sql(sql, visible)
            raise AssertionError("guard let through: " + sql)
        except ValueError:
            pass


def test_guard_rejects_writes_multi_and_hidden_tables():
    bad = [
        ("DROP TABLE sales", ["sales"]),
        ("DELETE FROM sales", ["sales"]),
        ("INSERT INTO sales VALUES (1)", ["sales"]),
        ("UPDATE sales SET amount=0", ["sales"]),
        ("SELECT * FROM sales; DROP TABLE sales", ["sales"]),
        ("PRAGMA table_info(sales)", ["sales"]),
        ("ATTACH DATABASE 'x' AS y", ["sales"]),
        ("SELECT * FROM exec_comp", ["sales"]),               # invisible table
        ("SELECT * FROM sales -- sneaky", ["sales"]),         # comments banned
        ("SELECT * FROM sales JOIN exec_comp ON 1=1", ["sales"]),
    ]
    for sql, visible in bad:
        try:
            validate_sql(sql, visible)
            raise AssertionError("guard let through: " + sql)
        except ValueError:
            pass


def test_engine_from_tables_schema_and_execute():
    eng = SqliteEngine.from_tables(SALES)
    schema = eng.schema()
    assert schema[0]["table"] == "sales", schema
    assert {c["name"] for c in schema[0]["columns"]} == {"region", "owner", "amount"}, schema
    cols, rows = eng.execute("SELECT SUM(amount) AS total FROM sales")
    assert cols == ["total"] and rows[0][0] == 200, (cols, rows)


def test_keyword_generator_sum_count_group():
    schema = SqliteEngine.from_tables(SALES).schema()
    assert keyword_sql_generator("total amount by region", schema) == \
        "SELECT region, SUM(amount) AS total_amount FROM sales GROUP BY region"
    assert keyword_sql_generator("how many sales records", schema) == \
        "SELECT COUNT(*) AS count FROM sales"
    assert keyword_sql_generator("average amount", schema) == \
        "SELECT AVG(amount) AS avg_amount FROM sales"
    assert keyword_sql_generator("show me things", schema) == \
        "SELECT * FROM sales LIMIT 5"


class _FakeSqlLlm:
    """Stand-in for a chat model exposing generate_sql (the #135 seam). Records the
    schema it was handed so the test can assert grounding; returns a canned string."""

    def __init__(self, sql):
        self._sql = sql
        self.seen_schema = None

    def generate_sql(self, question, schema):
        self.seen_schema = schema
        return self._sql


def test_llm_generator_uses_valid_model_sql_and_is_schema_grounded():
    schema = SqliteEngine.from_tables(SALES).schema()
    # a query the naive keyword generator can't express (no total/sum/count/by):
    # rank regions by revenue — only a real generator gets this right.
    llm = _FakeSqlLlm("SELECT region, SUM(amount) AS rev FROM sales "
                      "GROUP BY region ORDER BY rev DESC")
    gen = llm_sql_generator(llm)
    sql = gen("which regions have the most revenue", schema)
    assert "ORDER BY rev DESC" in sql, sql
    # the model was handed the real schema to ground on
    assert llm.seen_schema[0]["table"] == "sales", llm.seen_schema


def test_llm_generator_strips_code_fences_and_sql_prefix():
    schema = SqliteEngine.from_tables(SALES).schema()
    fenced = "```sql\nSELECT region FROM sales\n```"
    assert llm_sql_generator(_FakeSqlLlm(fenced))("q", schema) == "SELECT region FROM sales"


def test_llm_generator_falls_back_when_model_raises():
    schema = SqliteEngine.from_tables(SALES).schema()

    class _Boom:
        def generate_sql(self, q, s):
            raise RuntimeError("api down")

    gen = llm_sql_generator(_Boom())
    # degrades to the deterministic keyword generator, never errors
    assert gen("total amount by region", schema) == \
        keyword_sql_generator("total amount by region", schema)


def test_llm_generator_falls_back_on_unsafe_or_invalid_sql():
    schema = SqliteEngine.from_tables(SALES).schema()
    for bad in ["DELETE FROM sales",                       # write
                "SELECT * FROM secret_table",              # invisible table
                "SELECT * FROM sales; DROP TABLE sales",   # multi-statement
                "",                                        # empty
                "not sql at all"]:                         # not a SELECT
        gen = llm_sql_generator(_FakeSqlLlm(bad))
        got = gen("total amount by region", schema)
        assert got == keyword_sql_generator("total amount by region", schema), (bad, got)
        validate_sql(got, ["sales"])                       # fallback is always guard-safe


def test_llm_generator_end_to_end_through_store():
    eng = SqliteEngine.from_tables(SALES)
    llm = _FakeSqlLlm("SELECT region, SUM(amount) AS rev FROM sales GROUP BY region "
                      "ORDER BY rev DESC")
    s = FederatedSqlStore("sales-csv", "sales", "Sales", "revenue by region", eng,
                          sql_generator=llm_sql_generator(llm))
    evs = s.retrieve(s.authorize("u"), "rank regions by revenue", top_k=5)
    assert evs and evs[0].provenance["table"] == "sales", evs
    assert "ORDER BY rev DESC" in evs[0].provenance["sql"], evs[0].provenance
    # emea (140) outranks apac (60) -> first row
    assert "emea" in evs[0].content and "140" in evs[0].content, evs[0].content


def _store(row_policy=None, audit=None):
    eng = SqliteEngine.from_tables(SALES)
    authorizer = None
    if row_policy:
        authorizer = lambda user: router.AccessContext(user_oid=user, principals=[],  # noqa: E731
                                                       row_policy=row_policy(user))
    return FederatedSqlStore("sales-csv", "sales", "Sales figures",
                             "revenue sales amounts by region totals", eng,
                             authorizer=authorizer, audit=audit)


def test_store_retrieve_rows_with_sql_provenance_and_audit():
    trail = []
    s = _store(audit=trail.append)
    evs = s.retrieve(s.authorize("carol"), "total amount by region", top_k=5)
    assert s.profile().kind == FEDERATED_SQL and ANALYTICAL in s.capabilities()
    assert len(evs) == 2, evs
    assert evs[0].kind == "row", evs[0]
    assert any("emea" in e.content and "140" in e.content for e in evs), \
        [e.content for e in evs]
    assert evs[0].provenance["table"] == "sales", evs[0].provenance
    assert "SUM(amount)" in evs[0].provenance["sql"], evs[0].provenance
    assert evs[0].score is None, "rank not score (ADR 0008)"
    assert trail and trail[0]["sql"] == evs[0].provenance["sql"], trail
    assert trail[0]["user"] == "carol" and trail[0]["store"] == "sales-csv", trail


def test_row_policy_predicate_limits_rows():
    s = _store(row_policy=lambda user: f"owner = '{user}'")
    evs = s.retrieve(s.authorize("alice"), "show me things", top_k=10)
    assert len(evs) == 2, evs                                  # alice owns 2 of 3 rows
    assert all("alice" in e.content for e in evs), [e.content for e in evs]


def test_store_blocks_malicious_generator():
    eng = SqliteEngine.from_tables(SALES)
    s = FederatedSqlStore("x", "sales", "t", "d", eng,
                          sql_generator=lambda q, sch: "DROP TABLE sales")
    try:
        s.retrieve(s.authorize("u"), "anything")
        raise AssertionError("guard must reject generated DDL")
    except ValueError:
        pass


def test_provider_probe_build_inline_tables():
    prov = CsvSqlProvider()
    cfg = {"id": "sales-csv", "business_unit": "sales", "title": "Sales figures",
           "description": "revenue sales amounts", "tables": SALES}
    p = prov.probe(cfg)
    assert p.kind == FEDERATED_SQL and p.schema and p.schema[0]["table"] == "sales", p
    store = prov.build(cfg)
    evs = store.retrieve(store.authorize("u"), "total amount", top_k=3)
    assert evs and "200" in evs[0].content, evs


def test_full_vertical_analytical_query_routes_to_sql_store():
    spec = {
        "tenant": "acme",
        "stores": [
            {"id": "hr-wiki", "kind": "local", "business_unit": "hr", "acl": ["hr-staff"],
             "title": "HR Wiki",
             "description": "human resources parental leave holidays onboarding benefits",
             "config": {"seed": [{"external_id": "hb", "title": "Handbook", "uri": "u1",
                                  "acl": ["hr-staff"],
                                  "text": "parental leave is sixteen weeks"}],
                        "user_groups": {"carol": ["hr-staff", "fin-staff"]}}},
            {"id": "sales-csv", "kind": "csv", "business_unit": "sales",
             "acl": ["fin-staff"], "title": "Sales figures",
             "description": "revenue sales amounts by region totals ledger figures",
             "config": {"tables": SALES}},
        ],
    }
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    reg.register(CsvSqlProvider())
    cat = router.load_manifest(spec, registry=reg)
    identity = InMemoryIdentity({"carol": ["hr-staff", "fin-staff"], "alice": ["hr-staff"]})
    svc = router.RouterQueryService(cat, identity, HashingEmbedding())

    res = svc.ask("carol", "total revenue amounts by region", ExtractiveLlm())
    assert res.routing["query_type"] == "analytical", res.routing
    assert res.routing["stores"][0]["store_id"] == "sales-csv", res.routing
    assert "140" in res.answer or "emea" in res.answer, res.answer
    cite = res.citations[0]
    assert cite["kind"] == "row" and cite["table"] == "sales" and "SUM" in cite["sql"], cite

    leak = svc.ask("alice", "total revenue amounts by region", ExtractiveLlm())
    assert "sales-csv" not in repr(leak.to_dict()), "sql store existence leaked (gate #1)"


def main():
    print("Phase E E4 structured-store self-test:")
    test_guard_accepts_readonly_select()
    test_guard_rejects_writes_multi_and_hidden_tables()
    test_engine_from_tables_schema_and_execute()
    test_keyword_generator_sum_count_group()
    test_llm_generator_uses_valid_model_sql_and_is_schema_grounded()
    test_llm_generator_strips_code_fences_and_sql_prefix()
    test_llm_generator_falls_back_when_model_raises()
    test_llm_generator_falls_back_on_unsafe_or_invalid_sql()
    test_llm_generator_end_to_end_through_store()
    print("  PASS  #135 llm_sql_generator: model SQL used / fences stripped / "
          "fallback on raise+unsafe+empty / guard-safe end-to-end")
    test_store_retrieve_rows_with_sql_provenance_and_audit()
    test_row_policy_predicate_limits_rows()
    test_store_blocks_malicious_generator()
    test_provider_probe_build_inline_tables()
    test_full_vertical_analytical_query_routes_to_sql_store()
    print("  PASS  guard matrix / engine / generator / rows+audit / row-policy / "
          "DDL block / provider / analytical vertical + gate-1")
    print("\nE4 STRUCTURED-STORE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
