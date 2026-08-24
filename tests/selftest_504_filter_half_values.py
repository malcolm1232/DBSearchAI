"""#504 - the value-repair ladder never fires on a zero-row PROJECTION (F-005, D-003).

Findings s20 named the symptom: the cross-store rescue discloses "the filter half
carried no key values", and F-005 is D-001's exact twin in the wrong vocabulary -
"Rio de Janeiro's state" against a column encoded 'RJ'.

The card's hypothesis was that the rescue bypasses #462's wiring. It does not: the
halves dispatch through the SAME store object with the SAME `_value_llm`. The real
cause is the ladder's TRIGGER. `_execute_and_build` only looks for an unmatched
predicate when `empty_aggregate(cols, rows)` is true - one row, one column, holding
0 or NULL - because #476's reasoning was "zero rows is already EMPTY and needs no
help". That is right for the plain path, where an empty projection is an honest "no
matching rows" the user reads directly.

It is exactly wrong for the rescue's filter half, whose whole job is to PROJECT keys
for the next half to bind to. There, zero rows is not an answer: nothing carries, the
aligned-trust rule rejects the rescue, and a question the mechanism was built for
dies with a decline - while the plain path would have repaired the identical literal
had the question been phrased as an aggregate.

Every existing repair test (#479, selftest_value_dictionary.py) uses COUNT or SUM, so
the projection shape has no coverage at all - which is why this survived nine live-
traced fixes.

Run: PYTHONPATH=src python3 tests/selftest_504_filter_half_values.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.store import AccessContext  # noqa: E402
from dbsearch.router.structured import FederatedSqlStore, SqliteEngine  # noqa: E402

ACCESS = AccessContext(user_oid="u1", principals=[])

#: The olist shape F-005 actually fails on: state stored as the two-letter code.
TABLES = {
    "customers": {"columns": ["customer_id", "customer_state"],
                  "rows": [["c1", "RJ"], ["c2", "SP"], ["c3", "RJ"], ["c4", "MG"]]},
    "payments": {"columns": ["order_id", "payment_type", "payment_value"],
                 "rows": [["o1", "credit_card", 10.0], ["o2", "debit_card", 5.0]]},
}


class _StubEmbed:
    """'rio de janeiro' sits next to 'RJ' and far from the other states - the real
    embedder is exercised by the gate; what this pins is the TRIGGER."""

    _POINTS = {"rio de janeiro": 1.0, "rj": 0.97, "sp": -1.0, "mg": -0.9,
               "debit cards": 0.5, "debit_card": 0.48}

    def embed(self, texts: list) -> list:
        out = []
        for t in texts:
            x = self._POINTS.get(t.strip().lower(), 0.0)
            out.append([x, (1.0 - x * x) ** 0.5])
        return out


def _store(sql: str, embedder=None, value_llm=None):
    return FederatedSqlStore("olist", "retail", "Orders", "customer and order data",
                             SqliteEngine.from_tables(TABLES),
                             sql_generator=lambda *a, **k: sql, embedder=embedder,
                             value_llm=value_llm)


def test_a_projection_carry_source_repairs_its_literal_and_carries_keys():
    """THE #504 CASE. The rescue's filter half projects the keys the measure half will
    bind to, and reaches the store through retrieve_ranked (the carry-source seam).
    Written in the user's vocabulary it matches nothing; repaired, it carries the two
    RJ customers."""
    evidence = _store("SELECT customer_id FROM customers "
                      "WHERE customer_state = 'Rio de Janeiro'",
                      embedder=_StubEmbed()).retrieve_ranked(
        ACCESS, "which customers are in Rio de Janeiro's state?", top_k=50)
    assert evidence, "the filter half carried no key values - #504's exact symptom"
    carried = " ".join(ev.content for ev in evidence)
    assert "c1" in carried and "c3" in carried, carried
    assert "c2" not in carried, "SP must not carry - the repair is not a widening"
    resolved = (evidence[0].provenance or {}).get("resolved")
    assert resolved == {"column": "customer_state", "written": "Rio de Janeiro",
                        "resolved_to": "RJ"}, resolved


def test_the_aggregate_shape_is_unchanged():
    """Regression guard: #479's trigger must keep working exactly as it does today."""
    evidence = _store("SELECT COUNT(*) FROM customers "
                      "WHERE customer_state = 'Rio de Janeiro'",
                      embedder=_StubEmbed()).retrieve(ACCESS, "how many?", top_k=5)
    assert len(evidence) == 1, evidence
    assert "2" in evidence[0].content, evidence[0].content


def test_an_unresolvable_projection_literal_still_declines():
    """Uncertainty stays a decline - the repair may never invent a key set."""
    evidence = _store("SELECT customer_id FROM customers "
                      "WHERE customer_state = 'Atlantis'",
                      embedder=_StubEmbed()).retrieve_ranked(ACCESS, "which customers?",
                                                             top_k=50)
    assert evidence == [], f"expected a decline, got {[e.content for e in evidence]}"


def test_the_PLAIN_path_never_repairs_a_zero_row_projection():
    """THE HONESTY GUARD - the first cut of #504 failed exactly here.

    Triggering on any zero-row result meant a user asking for something that does not
    exist got the NEAREST STORED NEIGHBOUR's rows: 'Widget Pro' -> 'Widget Plus'. The
    substituted column need not even be in the SELECT list, so neither the prose nor the
    screen carries a trace (`provenance.resolved` is rendered nowhere). An honest empty
    list is a correct answer the reader can judge; silently answering about a different
    entity is the confidently-wrong class this architecture exists to remove.

    The aggregate precedent (#476/#479) does NOT extend here: there the alternative was a
    printed 0 - a falsehood the reader could not detect. Here the alternative is correct.
    """
    products = {"orders": {"columns": ["order_id", "product"],
                           "rows": [["o1", "Widget Plus"], ["o2", "Widget Plus"],
                                    ["o3", "Gadget Max"]]}}

    class _Near:
        _P = {"widget pro": 1.0, "widget plus": 0.985, "gadget max": -1.0}

        def embed(self, texts):
            out = []
            for t in texts:
                x = self._P.get(t.strip().lower(), 0.0)
                out.append([x, (1.0 - x * x) ** 0.5])
            return out

    store = FederatedSqlStore("s", "bu", "Orders", "order data",
                              SqliteEngine.from_tables(products),
                              sql_generator=lambda *a, **k:
                              "SELECT order_id FROM orders WHERE product = 'Widget Pro'",
                              embedder=_Near())
    evidence = store.retrieve(ACCESS, "which orders were for the Widget Pro?", top_k=10)
    assert evidence == [], (
        "the plain path answered about a DIFFERENT product: "
        f"{[e.content for e in evidence]} / "
        f"{[(e.provenance or {}).get('resolved') for e in evidence]}")


def test_the_kill_switch_restores_the_pre_504_behaviour():
    import os as _os
    _os.environ["DBSEARCH_REPAIR_EMPTY_CARRY"] = "0"
    try:
        evidence = _store("SELECT customer_id FROM customers "
                          "WHERE customer_state = 'Rio de Janeiro'",
                          embedder=_StubEmbed()).retrieve_ranked(
            ACCESS, "which customers?", top_k=50)
        assert evidence == [], f"kill switch did not disable the repair: {evidence}"
    finally:
        del _os.environ["DBSEARCH_REPAIR_EMPTY_CARRY"]


def test_a_projection_that_genuinely_matches_nothing_is_left_alone():
    """A real empty result must stay empty: 'MG' IS the stored encoding, so there is
    no literal to repair - the honest answer is no rows, not a widened query."""
    evidence = _store("SELECT customer_id FROM customers WHERE customer_state = 'MG' "
                      "AND customer_id = 'nobody'",
                      embedder=_StubEmbed()).retrieve_ranked(ACCESS, "which customers?",
                                                             top_k=50)
    assert evidence == [], f"expected a decline, got {[e.content for e in evidence]}"


def test_a_working_projection_is_never_touched():
    """Nothing may fire on a query that already returns rows."""
    evidence = _store("SELECT customer_id FROM customers WHERE customer_state = 'SP'",
                      embedder=_StubEmbed()).retrieve_ranked(ACCESS, "which customers?",
                                                             top_k=50)
    assert evidence, "a working projection must answer"
    assert "c2" in " ".join(ev.content for ev in evidence)
    assert (evidence[0].provenance or {}).get("resolved") is None, "no repair was needed"


def main():
    test_a_projection_carry_source_repairs_its_literal_and_carries_keys()
    test_the_aggregate_shape_is_unchanged()
    test_an_unresolvable_projection_literal_still_declines()
    test_the_PLAIN_path_never_repairs_a_zero_row_projection()
    test_the_kill_switch_restores_the_pre_504_behaviour()
    test_a_projection_that_genuinely_matches_nothing_is_left_alone()
    test_a_working_projection_is_never_touched()
    print("  PASS  #504: the repair ladder fires on a zero-row PROJECTION too - the "
          "filter half carries its keys, and a real empty stays empty")
    print("\nFILTER-HALF-VALUES SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
