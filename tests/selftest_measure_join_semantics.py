"""#271 — a measure per entity must not be padded with entities that have no measure.

Found by an independent audit agent. Asked "What is the total revenue for each product sku?"
the generator wrote a LEFT JOIN from SalesLT.Product: 295 rows, of which 153 had
TotalRevenue = NULL. Only 142 products have any revenue.

Two silent harms, plus one loud one:
  - the answer quotes the padded count as its denominator ("a partial sample of 5 products out
    of 295 total product SKUs"), inviting the reader to treat 295 as the revenue-bearing set;
  - the 153 empty rows compete for the row budget of a truncated result;
  - and it led the model to assert "The complete result set contains revenue data for all
    products", which is false of a majority of its own rows.

The instruction to be honest already existed — #206 tells the model "never imply it is
complete or exhaustive" — and was ignored. That is why this fix works on the DATA rather than
on the prose: remove the empty rows and the false claim has nothing to be false about.

An outer join stays available, because "show me the products that never sold" is a real
question. The default narrows; the shape is not forbidden.

Run: python3 tests/selftest_measure_join_semantics.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.adapters.anthropic import _SQL_SYSTEM  # noqa: E402


def test_the_generator_is_told_to_use_an_inner_join_for_a_measure():
    low = _SQL_SYSTEM.lower()
    assert "inner join" in low, "no join guidance for measure questions at all"
    assert "measure per entity" in low or "measure" in low, "the rule is not scoped to measures"


def test_the_outer_join_escape_hatch_survives():
    """Forbidding outer joins outright would break a legitimate question — 'which products
    never sold' NEEDS the empty rows. The rule must narrow the default, not remove the shape."""
    low = _SQL_SYSTEM.lower()
    assert "outer join only when" in low or "only when the question explicitly asks" in low, \
        "no escape hatch — the model can no longer answer 'including those with none'"
    assert "never sold" in low or "even if zero" in low, \
        "the escape hatch gives the model no example of when it applies"


def test_the_rule_says_WHY_not_just_what():
    """A rule the model cannot reason about gets applied literally and wrongly. It should know
    the harm, so it can recognise the case rather than pattern-match the wording."""
    low = _SQL_SYSTEM.lower()
    assert "no measure" in low or "misleading" in low, \
        "the rule states a prohibition without the reason behind it"


def test_the_existing_grounding_rules_are_untouched():
    """This prompt carries hard-won rules (#230 case-insensitive filters, #211 decline). A
    later edit must not quietly drop one while adding another."""
    low = _SQL_SYSTEM.lower()
    assert "lower(col)" in low, "#230 case-insensitive value comparison rule was lost"
    assert "read-only select" in low, "the read-only constraint was lost"
    assert "only tables" in low or "present in the schema" in low, \
        "the schema-grounding constraint was lost"


def main():
    print("#271 measure joins exclude entities with no measure:")
    test_the_generator_is_told_to_use_an_inner_join_for_a_measure()
    test_the_rule_says_WHY_not_just_what()
    print("  PASS  measure questions are told to inner-join, with the reason attached")
    test_the_outer_join_escape_hatch_survives()
    print("  PASS  'including those with none' is still answerable — the default narrowed, the "
          "shape was not forbidden")
    test_the_existing_grounding_rules_are_untouched()
    print("  PASS  #230 casing, read-only and schema-grounding rules all still present")
    print("\nMEASURE-JOIN-SEMANTICS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
