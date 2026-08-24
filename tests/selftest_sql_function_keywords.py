"""#477 - a scalar FUNCTION named like a statement keyword is not a write.

`SELECT MAX(CAST(REPLACE(salary, ',', '') AS REAL)) FROM salaries` is valid, correct,
read-only SQL: the model strips thousands separators before casting. `validate_sql`
rejected it because `replace` sits in the write/DDL blocklist - as in `REPLACE INTO`, the
upsert STATEMENT. A read-only function was being read as a write.

The consequence was invisible, which is why it survived: `llm_sql_generator` swallowed the
rejection and degraded to `keyword_sql_generator`, so the store answered a question about
salaries with `SELECT * FROM batting LIMIT 5` and the reason never surfaced. Measured on
the #473 real pack, this is what capability F's paraphrased questions actually hit -
paraphrase makes the model hedge defensively (CAST, REPLACE, TRIM), and the hedge tripped
the guard.

The distinction is structural, not a list of exceptions: a keyword followed by `(` is a
function call. No write statement in any dialect puts a parenthesis directly after its
leading keyword.

Run: PYTHONPATH=src python3 tests/selftest_sql_function_keywords.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.structured import validate_sql  # noqa: E402

TABLES = ["salaries", "orders", "reviews"]


def _rejected(sql: str) -> bool:
    try:
        validate_sql(sql, TABLES)
        return False
    except ValueError:
        return True


def test_the_query_the_model_actually_wrote_is_accepted():
    """Captured verbatim from llama3.1:8b on the real pack, for F-004."""
    assert not _rejected(
        "SELECT MAX(CAST(REPLACE(salary, ',', '') AS REAL)) FROM salaries")


def test_the_f003_query_is_accepted_too():
    assert not _rejected(
        "SELECT AVG(CAST(REPLACE(SUBSTR(review_score, 3), ',', '') AS REAL)) FROM reviews "
        "INNER JOIN orders ON reviews.order_id = orders.order_id "
        "WHERE order_status = 'delivered'")


def test_a_space_before_the_paren_is_still_a_function():
    assert not _rejected("SELECT REPLACE (salary, ',', '') FROM salaries")


def test_other_dialects_function_forms_are_accepted():
    """MySQL has TRUNCATE(n, d) and INSERT(str, pos, len, new) as scalar functions."""
    assert not _rejected("SELECT TRUNCATE(salary, 2) FROM salaries")
    assert not _rejected("SELECT INSERT(salary, 1, 2, 'xx') FROM salaries")


# --- the guard must not have been weakened --------------------------------------------

def test_the_write_statements_are_still_refused():
    for sql in ("SELECT 1 FROM salaries; REPLACE INTO salaries VALUES (1)",
                "SELECT * FROM salaries WHERE x = 1 UNION SELECT * FROM salaries; DROP TABLE t"):
        assert _rejected(sql), sql


def test_a_write_keyword_not_followed_by_a_paren_is_still_refused():
    for sql in ("SELECT * FROM salaries WHERE id IN (SELECT id FROM salaries) AND 1=1 "
                "AND EXISTS (SELECT 1) AND replace INTO x",
                "WITH c AS (SELECT 1) SELECT * FROM salaries WHERE drop TABLE y",
                "SELECT * FROM salaries WHERE pragma table_info(x)"):
        assert _rejected(sql), sql


def test_the_leading_keyword_rule_is_untouched():
    assert _rejected("REPLACE INTO salaries VALUES (1)")
    assert _rejected("DELETE FROM salaries")
    assert _rejected("UPDATE salaries SET salary = 0")


def test_the_visible_schema_rule_is_untouched():
    assert _rejected("SELECT REPLACE(x, 'a', 'b') FROM secret_table")


def main():
    test_the_query_the_model_actually_wrote_is_accepted()
    test_the_f003_query_is_accepted_too()
    test_a_space_before_the_paren_is_still_a_function()
    test_other_dialects_function_forms_are_accepted()
    print("  PASS  #477 REPLACE/TRUNCATE/INSERT as scalar FUNCTIONS are accepted")
    test_the_write_statements_are_still_refused()
    test_a_write_keyword_not_followed_by_a_paren_is_still_refused()
    test_the_leading_keyword_rule_is_untouched()
    test_the_visible_schema_rule_is_untouched()
    print("  PASS  #477 write STATEMENTS, bare write keywords, and the visible-schema "
          "rule are all still refused - the guard is narrowed, not weakened")
    print("\nSQL-FUNCTION-KEYWORD SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
