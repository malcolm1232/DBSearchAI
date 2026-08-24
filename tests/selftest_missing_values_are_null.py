"""#490 - a missing value must load as NULL, not as an empty string.

Asked "how many orders were called off before delivery", the model wrote

    SELECT COUNT(order_id) FROM orders WHERE order_status = 'canceled'
      AND (order_delivered_customer_date IS NULL OR order_delivered_customer_date > ...)

which is a reasonable reading: an order called off before delivery has no delivery date.
The embedded engine loaded the missing dates as `''`, so `IS NULL` matched nothing, the
whole query returned zero, and the answer was "there were no orders called off before
delivery" against a gold of 9.

Same shape as #481: the model's instinct was right and the engine was misrepresenting the
data. A real database stores an absent date as NULL, and every SQL idiom for absence -
IS NULL, COALESCE, LEFT JOIN ... IS NULL - depends on it.

The type-sniffing rule is deliberately NOT relaxed to match: a blank still makes a column
TEXT, exactly as the independent gold engine (`eval/golden/stage2._load_csv`) decides it,
so the two engines cannot disagree about which columns are numbers.

Run: PYTHONPATH=src python3 tests/selftest_missing_values_are_null.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.structured import SqliteEngine  # noqa: E402

ORDERS = {
    "orders": {
        "columns": ["order_id", "order_status", "delivered_on", "score"],
        "rows": [["o1", "delivered", "2018-03-05", 5],
                 ["o2", "canceled", "", 4],
                 ["o3", "canceled", "", 3]],
    },
}


def _engine():
    return SqliteEngine.from_tables(ORDERS)


def test_a_blank_loads_as_null():
    _cols, rows = _engine().execute(
        "SELECT COUNT(*) FROM orders WHERE delivered_on IS NULL")
    assert rows[0][0] == 2, rows


def test_the_question_that_started_it():
    """E-005's shape: cancelled AND never delivered."""
    _cols, rows = _engine().execute(
        "SELECT COUNT(order_id) FROM orders WHERE order_status = 'canceled' "
        "AND delivered_on IS NULL")
    assert rows[0][0] == 2, rows


def test_a_present_value_is_untouched():
    _cols, rows = _engine().execute(
        "SELECT COUNT(*) FROM orders WHERE delivered_on IS NOT NULL")
    assert rows[0][0] == 1, rows


def test_an_empty_string_no_longer_matches_as_a_value():
    _cols, rows = _engine().execute("SELECT COUNT(*) FROM orders WHERE delivered_on = ''")
    assert rows[0][0] == 0, rows


def test_the_type_rule_still_matches_the_gold_engine():
    """A blank keeps its column TEXT, the same call the independent gold engine makes, so
    the two can never disagree about which columns are numbers. Relaxing this to 'numeric
    apart from the NULLs' would reintroduce exactly that divergence."""
    types = {c["name"]: c["type"] for t in _engine().schema() for c in t["columns"]}
    assert types["delivered_on"] == "TEXT", types
    assert types["score"] == "INTEGER", types

    mixed = SqliteEngine.from_tables(
        {"t": {"columns": ["n"], "rows": [[1], [""], [3]]}}).schema()
    assert mixed[0]["columns"][0]["type"] == "TEXT", mixed


def test_whitespace_only_is_treated_as_missing_too():
    engine = SqliteEngine.from_tables(
        {"t": {"columns": ["a"], "rows": [["  "], ["x"]]}})
    _cols, rows = engine.execute("SELECT COUNT(*) FROM t WHERE a IS NULL")
    assert rows[0][0] == 1, rows


def test_a_real_zero_is_not_mistaken_for_missing():
    engine = SqliteEngine.from_tables(
        {"t": {"columns": ["n"], "rows": [[0], [1]]}})
    _cols, rows = engine.execute("SELECT COUNT(*) FROM t WHERE n IS NULL")
    assert rows[0][0] == 0, rows



def test_the_gold_engine_agrees_blanks_are_null_489():
    """#489: B-004's gold said 63 distinct product categories; the truth is 62. The GOLD
    engine (`eval/golden/stage2.gold_value`) loaded blank cells as '', counted the empty
    string as a 63rd category, and every 'fix' chased on the product side was a phantom -
    the model had been right all along. The two engines must make the SAME call #490
    settled: a blank is NULL."""
    import tempfile
    from pathlib import Path as _P

    from dbsearch.eval.golden.stage2 import gold_value

    with tempfile.TemporaryDirectory() as td:
        p = _P(td) / "products.csv"
        p.write_text("product_id,category\np1,beds\np2,\np3,toys\np4,beds\n")
        tables = {"catalog": {"products": p}}
        distinct = gold_value(tables, "SELECT COUNT(DISTINCT category) FROM products")
        assert distinct == 2, f"'' counted as a category: {distinct}"
        missing = gold_value(tables,
                             "SELECT COUNT(*) FROM products WHERE category IS NULL")
        assert missing == 1, f"IS NULL must see the blank: {missing}"


def main():
    test_a_blank_loads_as_null()
    test_the_question_that_started_it()
    test_a_present_value_is_untouched()
    test_an_empty_string_no_longer_matches_as_a_value()
    test_whitespace_only_is_treated_as_missing_too()
    test_a_real_zero_is_not_mistaken_for_missing()
    print("  PASS  #490 a missing value loads as NULL, so IS NULL works; a present value "
          "and a real zero are untouched")
    test_the_type_rule_still_matches_the_gold_engine()
    print("  PASS  #490 the type rule is unchanged - a blank still makes a column TEXT, "
          "the same call the independent gold engine makes")
    test_the_gold_engine_agrees_blanks_are_null_489()
    print("  PASS  #489 the GOLD engine makes the same call - a blank is NULL, never a "
          "countable value")
    print("\nMISSING-VALUES-ARE-NULL SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
