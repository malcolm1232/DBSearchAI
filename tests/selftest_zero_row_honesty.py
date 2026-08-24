"""#476 - a filtered aggregate that matched NOTHING must not be asserted as a zero.

`SELECT COUNT(*) FROM players WHERE LOWER(birthCountry)='dominican republic'` returns one
row containing 0, because the stored encoding is 'D.R.'. Nothing downstream can tell that
0 apart from a genuine zero, so the product answered "0 players in the register were born
in the Dominican Republic" - a confident falsehood, and one a user cannot detect.

The distinguishing evidence is cheap and lives in the tenant: re-run each predicate ALONE.
If a predicate matches no rows by itself, the filter is the reason the aggregate is empty,
and the aggregate is not an answer. If every predicate matches rows, a zero is real and is
still asserted - this must not trade false assertions for false declines.

Run: PYTHONPATH=src python3 tests/selftest_zero_row_honesty.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.store import AccessContext  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    FederatedSqlStore, SqliteEngine, empty_aggregate, predicate_probes,
)

ACCESS = AccessContext(user_oid="u1", principals=[])

TABLES = {
    "players": {
        "columns": ["playerID", "nameLast", "birthCountry", "birthYear"],
        "rows": [["a1", "Ortiz", "D.R.", 1975], ["a2", "Jeter", "USA", 1974],
                 ["a3", "Pujols", "D.R.", 1980]],
    },
    "orders": {
        "columns": ["order_id", "status", "amount"],
        "rows": [["o1", "delivered", 10.0], ["o2", "canceled", 20.0]],
    },
}


def _store(sql: str):
    """A store whose generator always emits `sql` - the point under test is what happens
    to the RESULT, not how the query was written."""
    return FederatedSqlStore("s1", "bu", "Baseball", "player register and orders",
                             SqliteEngine.from_tables(TABLES),
                             sql_generator=lambda *a, **k: sql)


def _answers(sql: str) -> list:
    return _store(sql).retrieve(ACCESS, "how many?", top_k=5)


# --- the pure halves ------------------------------------------------------------------

def test_empty_aggregate_recognises_zero_and_null():
    assert empty_aggregate(["c"], [(0,)])
    assert empty_aggregate(["c"], [(None,)])
    assert empty_aggregate(["c"], [(0.0,)])
    assert not empty_aggregate(["c"], [(3,)])
    assert not empty_aggregate(["c"], [])                    # no rows at all: already EMPTY
    assert not empty_aggregate(["a", "b"], [(0, 1)])         # a breakdown, not a scalar
    assert not empty_aggregate(["c"], [(0,), (1,)])          # more than one row


def test_predicate_probes_isolate_each_condition():
    probes = predicate_probes(
        "SELECT COUNT(*) FROM players WHERE LOWER(birthCountry) = 'dominican republic' "
        "AND birthYear > 1970")
    assert len(probes) == 2, probes
    assert all(p.startswith("SELECT 1 FROM players WHERE ") for _, p in probes), probes
    assert all(p.endswith(" LIMIT 1") for _, p in probes), probes


def test_predicate_probes_are_a_no_op_without_a_where_clause():
    assert predicate_probes("SELECT COUNT(*) FROM players") == []


def test_an_unprobeable_term_does_not_disqualify_its_neighbours():
    """A generator that hedges one condition into a parenthesised OR used to make every
    OTHER condition unprobeable too (#477 shows it does this routinely), so a query that
    matched nothing because of a plain equality sailed through unexamined."""
    probes = predicate_probes(
        "SELECT COUNT(*) FROM orders WHERE birthCountry = 'x' "
        "AND (delivered IS NULL OR delivered > estimated)")
    assert [p for p, _ in probes] == ["birthCountry = 'x'"], probes


# --- the behaviour that matters -------------------------------------------------------

def test_unmatched_literal_yields_no_evidence_rather_than_a_zero():
    """The bug, exactly: 'dominican republic' is not how the value is stored."""
    evidence = _answers("SELECT COUNT(*) FROM players "
                        "WHERE LOWER(birthCountry) = 'dominican republic'")
    assert evidence == [], f"expected no evidence, got {[e.content for e in evidence]}"


def test_a_genuine_zero_is_still_asserted():
    """Every predicate matches rows on its own; the combination really is empty. This is a
    true zero and must survive - the fix must not trade falsehoods for false declines."""
    evidence = _answers("SELECT COUNT(*) FROM players "
                        "WHERE birthCountry = 'USA' AND birthYear = 1980")
    assert len(evidence) == 1, f"a real zero was suppressed: {evidence}"
    assert "0" in evidence[0].content, evidence[0].content


def test_a_numeric_literal_quoted_as_a_string_no_longer_misses_at_all():
    """This assertion is INVERTED from how it was first written, and the inversion is the
    point.

    When #476 shipped, `WHERE birthYear = '1980'` matched nothing - the embedded engine
    declared no column types, so SQLite compared a number against a quoted literal as text
    - and the honest outcome was a decline. #481 found the cause one layer down and
    declared the types, so the affinity now converts the literal and the query simply
    works. There is no miss left to be honest about, which is strictly better than
    declining gracefully."""
    evidence = _answers("SELECT MAX(birthYear) FROM players WHERE birthYear = '1980'")
    assert len(evidence) == 1, f"expected the real answer, got {evidence}"
    assert "1980" in evidence[0].content, evidence[0].content


def test_a_non_empty_aggregate_is_never_probed():
    evidence = _answers("SELECT COUNT(*) FROM players WHERE birthCountry = 'D.R.'")
    assert len(evidence) == 1
    assert "2" in evidence[0].content, evidence[0].content


def test_a_breakdown_is_untouched():
    """Only a single-cell aggregate is a candidate; a GROUP BY breakdown that happens to
    contain a zero row is a different shape and must not be suppressed."""
    evidence = _answers("SELECT status, COUNT(*) FROM orders GROUP BY status")
    assert len(evidence) == 2, [e.content for e in evidence]


def main():
    test_empty_aggregate_recognises_zero_and_null()
    test_predicate_probes_isolate_each_condition()
    test_predicate_probes_are_a_no_op_without_a_where_clause()
    test_an_unprobeable_term_does_not_disqualify_its_neighbours()
    print("  PASS  #476 the pure halves: empty_aggregate and predicate_probes")
    test_unmatched_literal_yields_no_evidence_rather_than_a_zero()
    test_a_numeric_literal_quoted_as_a_string_no_longer_misses_at_all()
    print("  PASS  #476 a filter that matched nothing yields NO evidence, so the store "
          "reports EMPTY and the answer declines instead of asserting 0")
    test_a_genuine_zero_is_still_asserted()
    test_a_non_empty_aggregate_is_never_probed()
    test_a_breakdown_is_untouched()
    print("  PASS  #476 a genuine zero, a non-empty aggregate and a breakdown are all "
          "untouched - no false declines")
    print("\nZERO-ROW-HONESTY SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
