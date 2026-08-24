"""#481 - a string function on a NUMERIC column is a type error, and it answers 0.0.

Asked "typically, how satisfied were shoppers once their purchase arrived", llama3.1:8b
wrote, against a `review_score REAL` holding 1-5:

    SELECT AVG(CAST(REPLACE(SUBSTR(review_score, 3), ',', '') AS REAL)) FROM reviews ...

SUBSTR from position 3 of "4" is the empty string, CAST('' AS REAL) is 0.0, and the answer
was "shoppers were typically very dissatisfied, with an average review score of 0.0".
Gold is 3.79. Nothing about that reads as a failure to the person receiving it.

The model does this because it does not trust the declared type - the same defensive
hedging #468 measured, which survived types being added to the payload. Since the SERVER
knows the type, it does not have to be argued about: a string function whose argument is
exactly a numeric column is stripped, because a number has no commas to remove and no
substring to take. The rewrite is refused in every other case.

Run: PYTHONPATH=src python3 tests/selftest_numeric_string_ops.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.store import AccessContext  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    FederatedSqlStore, SqliteEngine, strip_string_ops_on_numeric,
)

ACCESS = AccessContext(user_oid="u1", principals=[])

SCHEMA = [
    {"table": "reviews", "columns": [{"name": "review_id", "type": "TEXT"},
                                     {"name": "review_score", "type": "REAL"}]},
    {"table": "products", "columns": [{"name": "sku", "type": "TEXT"},
                                      {"name": "price", "type": "REAL"}]},
]


def test_the_query_the_model_actually_wrote_is_normalised():
    got = strip_string_ops_on_numeric(
        "SELECT AVG(CAST(REPLACE(SUBSTR(review_score, 3), ',', '') AS REAL)) FROM reviews",
        SCHEMA)
    assert got == "SELECT AVG(CAST(review_score AS REAL)) FROM reviews", got


def test_a_single_wrapper_is_stripped():
    assert strip_string_ops_on_numeric(
        "SELECT SUM(REPLACE(price, ',', '')) FROM products", SCHEMA) == \
        "SELECT SUM(price) FROM products"


def test_a_qualified_column_is_stripped_too():
    assert strip_string_ops_on_numeric(
        "SELECT AVG(TRIM(T1.review_score)) FROM reviews AS T1", SCHEMA) == \
        "SELECT AVG(T1.review_score) FROM reviews AS T1"


def test_a_text_column_is_left_alone():
    """The whole point of these functions - on TEXT they are correct and necessary."""
    sql = "SELECT * FROM products WHERE LOWER(TRIM(sku)) = 'abc'"
    assert strip_string_ops_on_numeric(sql, SCHEMA) == sql


def test_an_unknown_column_is_left_alone():
    sql = "SELECT REPLACE(mystery, ',', '') FROM reviews"
    assert strip_string_ops_on_numeric(sql, SCHEMA) == sql


def test_an_expression_argument_is_left_alone():
    """Only an argument that is EXACTLY a numeric column is safe to strip - the function
    may be doing real work on anything else."""
    sql = "SELECT REPLACE(review_score || 'x', ',', '') FROM reviews"
    assert strip_string_ops_on_numeric(sql, SCHEMA) == sql


def test_a_literal_argument_is_left_alone():
    sql = "SELECT REPLACE('a,b', ',', '') FROM reviews"
    assert strip_string_ops_on_numeric(sql, SCHEMA) == sql


def test_a_column_ambiguous_across_tables_is_left_alone():
    schema = [
        {"table": "a", "columns": [{"name": "code", "type": "REAL"}]},
        {"table": "b", "columns": [{"name": "code", "type": "TEXT"}]},
    ]
    sql = "SELECT TRIM(code) FROM a"
    assert strip_string_ops_on_numeric(sql, schema) == sql


def test_non_string_functions_are_untouched():
    sql = "SELECT AVG(ROUND(review_score, 2)) FROM reviews"
    assert strip_string_ops_on_numeric(sql, SCHEMA) == sql


# --- the root cause: the engine declared no types at all -------------------------------

def _engine():
    return SqliteEngine.from_tables({
        "reviews": {"columns": ["review_id", "review_score", "note"],
                    "rows": [["r1", 5.0, "ok"], ["r2", 4.0, ""], ["r3", 3.0, "late"]]},
        "seasons": {"columns": ["yearID", "hr"], "rows": [[2015.0, 232.0], [2014.0, 211.0]]}})


def test_a_numeric_column_is_declared_numeric():
    """It was `CREATE TABLE t ("a", "b")` - no types - so schema() reported EVERY column
    as TEXT and the model was told a 1-5 score was text. The hedging followed from that."""
    columns = {c["name"]: c["type"] for t in _engine().schema()
               if t["table"] == "reviews" for c in t["columns"]}
    assert columns["review_score"] == "INTEGER", columns   # whole numbers stay whole
    assert columns["review_id"] == "TEXT", columns
    assert columns["note"] == "TEXT", columns          # a blank means not-every-value-numeric
    prices = {c["name"]: c["type"] for t in SqliteEngine.from_tables(
        {"p": {"columns": ["price"], "rows": [[1.5], [2.25]]}}).schema()
        for c in t["columns"]}
    assert prices["price"] == "REAL", prices


def test_a_quoted_numeric_literal_now_matches():
    """#475 for free: without an affinity SQLite compares a REAL column to '2015' as text
    and matches nothing - which is how B-003 answered 'the highest number of home runs any
    club hit in the 2015 season is None'."""
    _cols, rows = _engine().execute("SELECT MAX(hr) FROM seasons WHERE yearID = '2015'")
    assert rows[0][0] == 232.0, rows


# --- end to end ------------------------------------------------------------------------

def test_the_store_returns_the_real_average_not_zero():
    tables = {"reviews": {"columns": ["review_id", "review_score"],
                          "rows": [["r1", 5.0], ["r2", 4.0], ["r3", 3.0]]}}
    sql = "SELECT AVG(CAST(REPLACE(SUBSTR(review_score, 3), ',', '') AS REAL)) FROM reviews"
    store = FederatedSqlStore("s", "bu", "Reviews", "buyer review scores",
                              SqliteEngine.from_tables(tables),
                              sql_generator=lambda *a, **k: sql)
    evidence = store.retrieve(ACCESS, "how satisfied were shoppers?", top_k=5)
    assert len(evidence) == 1, evidence
    assert "4" in evidence[0].content, evidence[0].content        # (5+4+3)/3
    assert "0.0" not in evidence[0].content, evidence[0].content
    assert store.audit_trail[-1].get("normalized"), store.audit_trail[-1]


def main():
    test_the_query_the_model_actually_wrote_is_normalised()
    test_a_single_wrapper_is_stripped()
    test_a_qualified_column_is_stripped_too()
    print("  PASS  #481 string wrappers around a numeric column are stripped, nested too")
    test_a_text_column_is_left_alone()
    test_an_unknown_column_is_left_alone()
    test_an_expression_argument_is_left_alone()
    test_a_literal_argument_is_left_alone()
    test_a_column_ambiguous_across_tables_is_left_alone()
    test_non_string_functions_are_untouched()
    print("  PASS  #481 TEXT columns, unknown columns, expressions, literals, ambiguous "
          "names and non-string functions are all left alone")
    test_a_numeric_column_is_declared_numeric()
    test_a_quoted_numeric_literal_now_matches()
    print("  PASS  #481 root cause: the embedded engine now DECLARES column types, so the "
          "model is told REAL instead of TEXT - and #475's quoted literal matches too")
    test_the_store_returns_the_real_average_not_zero()
    print("  PASS  #481 end to end: the store returns the real average instead of 0.0, "
          "and records that it normalised the query")
    print("\nNUMERIC-STRING-OPS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
