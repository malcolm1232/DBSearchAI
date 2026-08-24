"""#207 — a truncated breakdown must show the rows that MATTER, not an alphabetical slice.

Live symptom that produced this: "What is the total revenue for each product sku?" against
1,200-table AdventureWorks generated

    WITH ProductRevenue AS (
      SELECT p.ProductNumber, SUM(sod.LineTotal) AS TotalRevenue
      FROM SalesLT.SalesOrderDetail sod JOIN SalesLT.Product p ON sod.ProductID = p.ProductID
      GROUP BY p.ProductNumber)
    SELECT ProductNumber, TotalRevenue FROM ProductRevenue
    ORDER BY ProductNumber                                   <-- the key, not the measure

142 rows came back, the display cap kept the first 5, and the user was shown $4,884.10 of
revenue — 0.7% of the $708,690.15 total — presented as "the total revenue for each product
SKU". The true top 5 are 24.0%. Confidently formatted, correctly cited, and useless.

#232 already solved the rewrite, but scoped it to semi-join CARRY-SOURCE halves and made it a
no-op on CTEs. Both limits had to go for the single-store case — carefully:

  - widening must not override an ordering the QUESTION asked for. Only a breakdown ordered
    by its own GROUP BY key (the alphabetical accident) is rewritten; any other ORDER BY is
    left alone.
  - the CTE rewrite is only safe when the measure's alias is actually visible OUTSIDE the CTE
    body. `WITH c AS (... COUNT(*) n ...) SELECT a FROM c` cannot ORDER BY n.

Run: python3 tests/selftest_router_rank_default.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.store import AccessContext  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    FederatedSqlStore, SqliteEngine, rank_by_measure, rank_grouped_default)

ACCESS = AccessContext(user_oid="u1", principals=[])

SKU_CTE = ("WITH ProductRevenue AS (SELECT p.ProductNumber, SUM(sod.LineTotal) AS TotalRevenue "
           "FROM SalesOrderDetail sod JOIN Product p ON sod.ProductID = p.ProductID "
           "GROUP BY p.ProductNumber) SELECT ProductNumber, TotalRevenue FROM ProductRevenue "
           "ORDER BY ProductNumber")


def test_the_live_sku_cte_is_reordered_by_revenue():
    """The exact shape that shipped the 0.7%-of-revenue answer."""
    out = rank_grouped_default(SKU_CTE)
    assert out.endswith("ORDER BY TotalRevenue DESC"), out
    assert "ORDER BY ProductNumber" not in out, out
    # the CTE body itself is untouched — only the OUTER ordering changes
    assert "GROUP BY p.ProductNumber)" in out, out


def test_a_cte_whose_measure_alias_is_not_selected_outside_is_left_alone():
    """`SELECT a FROM c` cannot ORDER BY n — rewriting would produce invalid SQL. Fail safe.

    NB the alias here is declared with AS. An earlier version of this test used the implicit
    form (`COUNT(*) n`), which _AGG_MEASURE does not capture at all — so the alias came back
    None and the visibility check was never reached. The test passed against a build with the
    check deleted, i.e. it proved nothing. Keep the AS form so this actually exercises it."""
    sql = "WITH c AS (SELECT a, COUNT(*) AS n FROM t GROUP BY a) SELECT a FROM c"
    assert rank_grouped_default(sql) == sql, "alias n is not selected outside the CTE body"
    assert rank_by_measure(sql) == sql          # #232's contract is unchanged

    # ...and the implicit-alias form too, which is unreferenceable for a different reason
    implicit = "WITH c AS (SELECT a, COUNT(*) n FROM t GROUP BY a) SELECT a FROM c"
    assert rank_grouped_default(implicit) == implicit


def test_a_plain_key_ordered_breakdown_is_reordered():
    assert rank_grouped_default(
        "SELECT region, SUM(amount) AS total FROM s GROUP BY region ORDER BY region"
    ) == "SELECT region, SUM(amount) AS total FROM s GROUP BY region ORDER BY total DESC"


def test_an_ordering_the_question_asked_for_is_never_overridden():
    """The widened default must not silently re-sort a query that already expresses intent.
    Only the key-ordered accident is rewritten."""
    # already ordered by the measure — nothing to do
    already = "SELECT region, SUM(amount) AS total FROM s GROUP BY region ORDER BY total DESC"
    assert rank_grouped_default(already) == already
    # ordered ASC by the measure — that is a deliberate "smallest first", leave it
    asc = "SELECT region, SUM(amount) AS total FROM s GROUP BY region ORDER BY total ASC"
    assert rank_grouped_default(asc) == asc
    # ordered by some OTHER column — leave it
    other = "SELECT region, mgr, SUM(amount) AS total FROM s GROUP BY region, mgr ORDER BY mgr"
    assert rank_grouped_default(other) == other


def test_a_cte_whose_outer_query_is_a_scalar_aggregate_is_left_alone():
    """#267, a regression this very function introduced. "how many product skus have total
    revenue over 10000" generates

        WITH product_revenue AS (SELECT ..., SUM(...) AS total_revenue ... GROUP BY ...)
        SELECT COUNT(*) FROM product_revenue WHERE total_revenue > 10000

    The alias appears at depth 0 — but in the WHERE clause, not the SELECT list. Appending
    ORDER BY total_revenue there is invalid SQL ("not contained in either an aggregate function
    or the GROUP BY clause") and the whole query hard-errors. Worse than the bug it was fixing:
    the user got no answer at all.

    The alias must be SELECTED by the outer query, not merely mentioned somewhere in it."""
    sql = ("WITH product_revenue AS (SELECT p.sku, SUM(d.amount) AS total_revenue FROM d "
           "JOIN p ON d.pid = p.id GROUP BY p.sku) "
           "SELECT COUNT(*) FROM product_revenue WHERE total_revenue > 10000")
    assert rank_grouped_default(sql) == sql, \
        "appended an ORDER BY to a scalar aggregate — invalid SQL, the query dies"

    # the same shape with a plain filtered row list IS orderable and still gets ranked
    listed = ("WITH product_revenue AS (SELECT p.sku, SUM(d.amount) AS total_revenue FROM d "
              "JOIN p ON d.pid = p.id GROUP BY p.sku) "
              "SELECT sku, total_revenue FROM product_revenue WHERE total_revenue > 10000")
    assert rank_grouped_default(listed).endswith("ORDER BY total_revenue DESC"), \
        rank_grouped_default(listed)


def test_non_breakdowns_are_untouched():
    for sql in ("SELECT * FROM t",
                "SELECT name FROM t ORDER BY name",
                "SELECT COUNT(*) FROM t"):
        assert rank_grouped_default(sql) == sql, sql


def test_retrieve_now_shows_the_top_rows_not_the_alphabetical_head():
    """End to end through the store: the rows a user actually sees."""
    eng = SqliteEngine.from_tables({"sales": {"columns": ["product", "amount"],
                                              "rows": [["A", 1], ["B", 2], ["C", 10], ["D", 20]]}})
    store = FederatedSqlStore(
        "s", "bu", "S", "sales", eng,
        sql_generator=lambda q, s: "SELECT product, SUM(amount) AS total FROM sales "
                                   "GROUP BY product ORDER BY product")     # key-ordered
    shown = [e.content.split(",")[0] for e in store.retrieve(ACCESS, "totals", top_k=2)]
    assert shown == ["product=D", "product=C"], (
        f"retrieve() still shows the alphabetical head {shown} — the truncated sample must be "
        "the top rows by measure")


def test_provenance_reports_the_sql_that_actually_ran():
    """If the augmented SQL is not what provenance shows, 'Show query' and 'Verify data' lie —
    and the re-run tally in e2edbs would compare against a query that never executed."""
    eng = SqliteEngine.from_tables({"sales": {"columns": ["product", "amount"],
                                              "rows": [["A", 1], ["D", 20]]}})
    store = FederatedSqlStore(
        "s", "bu", "S", "sales", eng,
        sql_generator=lambda q, s: "SELECT product, SUM(amount) AS total FROM sales "
                                   "GROUP BY product ORDER BY product")
    ev = store.retrieve(ACCESS, "totals", top_k=1)
    assert ev[0].provenance["sql"].endswith("ORDER BY total DESC"), ev[0].provenance["sql"]


def main():
    print("#207 measure-ordered breakdowns (the truncated sample must be the top rows):")
    test_the_live_sku_cte_is_reordered_by_revenue()
    test_a_cte_whose_measure_alias_is_not_selected_outside_is_left_alone()
    print("  PASS  the live SKU CTE reorders by revenue; a CTE whose alias is not visible "
          "outside stays untouched")
    test_a_plain_key_ordered_breakdown_is_reordered()
    test_an_ordering_the_question_asked_for_is_never_overridden()
    test_a_cte_whose_outer_query_is_a_scalar_aggregate_is_left_alone()
    test_non_breakdowns_are_untouched()
    print("  PASS  key-ordered accidents are rewritten; deliberate orderings and non-breakdowns "
          "are left alone")
    test_retrieve_now_shows_the_top_rows_not_the_alphabetical_head()
    test_provenance_reports_the_sql_that_actually_ran()
    print("  PASS  retrieve() shows the true top rows, and provenance reports the SQL that ran")
    print("\nRANK-DEFAULT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
