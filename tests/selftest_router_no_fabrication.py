"""#211 — a store that does NOT hold the data must DECLINE, never invent a column.

The failure, live: with a support-ticket question routed to AdventureWorks (which has no
tickets at all), the generator emitted

    SELECT p.ProductNumber, COUNT(DISTINCT sod.SalesOrderDetailID) AS support_tickets ...

It counted SALES ORDER LINES and *called them* support tickets. The synthesizer then reported
those numbers with citations, a proof pill and a confident conclusion. The number was real; the
LABEL was invented — and once the fleet was composed, those fabricated counts got MIXED into
the same answer as the real ticket counts from Postgres.

`validate_sql` cannot catch this: the SQL is syntactically valid and touches only visible
tables. The guard checks SAFETY, not SEMANTIC HONESTY. The licence was in the prompt itself —
"if the question can't be answered from the schema, return the safest broad SELECT over the
most relevant table" — i.e. answer anyway.

Run: python3 tests/selftest_router_no_fabrication.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402
from dbsearch.router.executor import DECLINED, OK, execute  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    CANNOT_ANSWER, CannotAnswerFromSchema, keyword_sql_generator, llm_sql_generator,
)
from dbsearch.router.synthesizer import disclosure_from  # noqa: E402

SCHEMA = [{"table": "SalesLT.Product",
           "columns": [{"name": "ProductNumber", "type": "nvarchar"},
                       {"name": "ListPrice", "type": "money"}]}]


class _Llm:
    def __init__(self, sql):
        self._sql = sql

    def generate_sql(self, question, schema):
        return self._sql


def test_model_declining_does_NOT_fall_back_to_a_broad_select():
    """The whole point. Falling back to keyword_sql_generator here would re-introduce the bug
    by another door: a broad SELECT over the 'most relevant table' is exactly the fabrication,
    just without the invented alias."""
    gen = llm_sql_generator(_Llm(CANNOT_ANSWER))
    try:
        gen("which products have the most support tickets?", SCHEMA)
        raise AssertionError("generator answered a question the schema cannot support")
    except CannotAnswerFromSchema:
        pass


def test_a_genuinely_bad_generation_still_falls_back():
    """No regression: a broken/refused/unsafe generation must still degrade to the keyword
    generator. Only an explicit 'I cannot answer this from this schema' is a decline."""
    for bad in ("", "DROP TABLE SalesLT.Product", "SELECT * FROM secrets", "not sql at all"):
        sql = llm_sql_generator(_Llm(bad))("total revenue", SCHEMA)
        assert sql == keyword_sql_generator("total revenue", SCHEMA), (bad, sql)


def test_declining_store_yields_a_DECLINED_outcome_with_no_evidence():
    class Store:
        def authorize(self, oid):
            return object()

        def retrieve(self, access, question, top_k=5):
            raise CannotAnswerFromSchema(
                "this source holds no support-ticket data")

    class Catalog:
        def get(self, sid):
            class N:
                store = Store()
            return N()

    decision = RoutingDecision(
        query_type="analytical",
        stores=[RoutedStore(store_id="adventureworks", business_unit="sales", score=0.1,
                            why="")])
    report = execute(Catalog(), decision, "alice", "most support tickets?")
    assert not report.evidence_by_store, "a declining store must contribute NO evidence"
    out = report.outcomes[0]
    assert out.status == DECLINED, out
    assert "support-ticket" in out.error, out


def test_the_decline_is_DISCLOSED_not_silent():
    """A silent decline is how the fabrication got dressed up as an answer in the first place —
    the reader must be told the store was asked and had nothing."""
    from dbsearch.router.executor import StoreOutcome

    d = disclosure_from([
        StoreOutcome("support-tickets", "support", OK, count=5, total=5),
        StoreOutcome("adventureworks", "sales", DECLINED,
                     error="this source holds no support-ticket data"),
    ])
    assert "adventureworks" in d, d
    assert "holds no data" in d, d
    # the store that ANSWERED must not be dragged into the disclosure — that would read as a
    # failure and make the line noise, which is how disclosures get ignored
    assert "support-tickets" not in d, d


def main():
    print("#211 no fabrication — a store without the data must DECLINE:")
    test_model_declining_does_NOT_fall_back_to_a_broad_select()
    print("  PASS  a decline does NOT fall back to a broad SELECT (that IS the fabrication)")
    test_a_genuinely_bad_generation_still_falls_back()
    print("  PASS  a genuinely bad/unsafe generation still degrades to the keyword generator")
    test_declining_store_yields_a_DECLINED_outcome_with_no_evidence()
    test_the_decline_is_DISCLOSED_not_silent()
    print("  PASS  the store contributes NO evidence, and the decline is DISCLOSED")
    print("\n#211 NO-FABRICATION SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
