"""#524 - a cross-store plan must carry a key that BOTH stores actually have.

`llm_cross_store_planner` already refuses a plan naming a store the caller cannot see
(LAW 2) or naming one store twice. It did not check the thing the plan exists to do:
hand a key from the filter half to the measure half.

Measured live (findings s26), D-002's plan carries `product_category_name` into
`olist-orders`, which has no such column - `order_items(order_id, order_item_id,
product_id, seller_id, price, freight_value)`. The real shared key is `product_id`, two
hops inside the catalog store (`categories` -> `products`). The measure half then binds
nothing, and the rescue is rejected with "the measure half could not be aligned to the
carried keys" after paying for two model calls and two store round-trips.

The planner's own system prompt already says "Find the SHARED KEY: a column name that
appears in BOTH stores". Nothing enforced it.

The guard is derived, not declared: the caller-visible metadata already carries every
store's column names, so the factory can intersect them and confirm the filter half names
a key from that intersection. No new field in the JSON contract - which matters, because
F-005 answers correctly today through this exact parser and a contract change would put
a working item at risk to fix two broken ones.

LAW 1 holds: this reads column NAMES only, never a value.

Run: PYTHONPATH=src python3 tests/selftest_524_planner_carry_key.py
"""
import json
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.decompose import llm_cross_store_planner  # noqa: E402

# The real golden-pack shape, trimmed to the columns that matter. olist-orders and
# olist-catalog share product_id and customer_id - and NOT product_category_name.
PACK_META = [
    {"id": "olist-orders", "tables": [
        {"table": "order_items", "columns": ["order_id", "order_item_id", "product_id",
                                             "seller_id", "price", "freight_value"]},
        {"table": "orders", "columns": ["order_id", "customer_id", "order_status"]},
        {"table": "reviews", "columns": ["review_id", "order_id", "review_score"]}]},
    {"id": "olist-catalog", "tables": [
        {"table": "products", "columns": ["product_id", "product_category_name"]},
        {"table": "categories", "columns": ["product_category_name",
                                            "product_category_name_english"]},
        {"table": "customers", "columns": ["customer_id", "customer_state"]}]},
    {"id": "movielens", "tables": [
        {"table": "ratings", "columns": ["userId", "movieId", "rating"]}]},
]


class _RawLlm:
    def __init__(self, reply):
        self._reply = reply

    def plan_cross_store(self, question, stores):
        return self._reply


def _plan(**fields) -> str:
    return json.dumps(fields)


# --- the plans measured live (findings s26) -------------------------------------------

D002_BAD = _plan(
    filter_store="olist-catalog",
    filter=("List the distinct product_category_name values where product_category_name "
            "is 'health and beauty'. One column only, no joins."),
    measure_store="olist-orders",
    measure="How many order lines were for those product_category_name values?")

D002_GOOD = _plan(
    filter_store="olist-catalog",
    filter=("List the distinct product_id values where product_category_name_english is "
            "health_beauty. One column only."),
    measure_store="olist-orders",
    measure="How many order lines were for those product_id values?")

F005_GOOD = _plan(
    filter_store="olist-catalog",
    filter=("List the distinct customer_id values where customer_state is Rio de "
            "Janeiro. One column only, no joins."),
    measure_store="olist-orders",
    measure="What is the total order_items price for those customer_id values?")

D003_WRONG_STORE = _plan(
    filter_store="olist-catalog",
    filter="List the distinct customer_id values where customer_state is SP.",
    measure_store="movielens",
    measure="What is the average rating for those customer_id values?")


def test_the_measured_d002_plan_is_refused():
    """product_category_name is not a column of olist-orders, so nothing can cross."""
    assert llm_cross_store_planner(_RawLlm(D002_BAD))("q", PACK_META) is None


def test_the_correct_d002_plan_is_accepted():
    """product_id IS shared, and reaching it needs a join inside the filter store - which
    the guard must not forbid, only the missing KEY is disqualifying."""
    assert llm_cross_store_planner(_RawLlm(D002_GOOD))("q", PACK_META) == [
        ("olist-catalog", json.loads(D002_GOOD)["filter"]),
        ("olist-orders", json.loads(D002_GOOD)["measure"])]


def test_f005_the_item_that_answers_today_is_unaffected():
    """THE regression that matters. F-005 reaches gold through this parser right now."""
    assert llm_cross_store_planner(_RawLlm(F005_GOOD))("q", PACK_META) == [
        ("olist-catalog", json.loads(F005_GOOD)["filter"]),
        ("olist-orders", json.loads(F005_GOOD)["measure"])]


def test_a_measure_store_no_key_reaches_is_REPAIRED_when_the_schema_settles_it():
    """#518 cause (b), subsumed. movielens shares no column with olist-catalog, so
    'average review score' can never have meant its ratings table - and exactly one
    visible store DOES receive customer_id, so the schema settles the choice outright.

    This is the F-005 case too (findings s26): the plan is right about the hard part -
    customer_id from olist-catalog is the gold join key - and wrong only about the thing
    the metadata already decides. Every prompt variant that taught D-002's two-hop filter
    broke this store choice instead, so the split of labour moved: the model picks the
    key and the phrasing, the schema picks the store."""
    assert llm_cross_store_planner(_RawLlm(D003_WRONG_STORE))("q", PACK_META) == [
        ("olist-catalog", json.loads(D003_WRONG_STORE)["filter"]),
        ("olist-orders", json.loads(D003_WRONG_STORE)["measure"])]


def test_an_ambiguous_repair_is_refused_rather_than_guessed():
    """Two stores could receive the key, so which one is a routing judgment this guard has
    no business making silently. Fail closed to today's honest decline."""
    meta = [{"id": "src", "tables": [{"table": "c", "columns": ["k", "state"]}]},
            {"id": "one", "tables": [{"table": "a", "columns": ["k", "amount"]}]},
            {"id": "two", "tables": [{"table": "b", "columns": ["k", "amount"]}]},
            {"id": "none", "tables": [{"table": "z", "columns": ["unrelated"]}]}]
    plan = _plan(filter_store="src", filter="List the distinct k values where state is X.",
                 measure_store="none", measure="Total amount for those k values?")
    assert llm_cross_store_planner(_RawLlm(plan))("q", meta) is None


def test_a_repair_never_names_a_store_the_caller_cannot_see():
    """LAW 2: the repair searches the SAME caller-visible metadata the plan was made on,
    so it cannot reach a store the model itself could not have named."""
    visible = [{"id": "src", "tables": [{"table": "c", "columns": ["k", "state"]}]},
               {"id": "ok", "tables": [{"table": "a", "columns": ["k", "amount"]}]}]
    plan = _plan(filter_store="src", filter="List the distinct k values where state is X.",
                 measure_store="secret-hr", measure="Total amount for those k values?")
    # an invisible measure store is refused outright, before any repair is considered
    assert llm_cross_store_planner(_RawLlm(plan))("q", visible) is None


def test_a_declared_key_is_honoured_when_it_is_genuinely_shared():
    """Additive, not required: a model that names its key explicitly is believed, but
    only after the same intersection check."""
    good = _plan(filter_store="olist-catalog", filter="List the ids for health_beauty.",
                 measure_store="olist-orders", measure="How many order lines?",
                 key="product_id")
    assert llm_cross_store_planner(_RawLlm(good))("q", PACK_META) is not None


def test_a_declared_key_that_is_not_shared_is_still_refused():
    """A declaration is not evidence - it is checked against the metadata like anything
    else, or the guard would be bypassable by asserting the thing being verified."""
    bad = _plan(filter_store="olist-catalog",
                filter="List the distinct product_category_name values.",
                measure_store="olist-orders", measure="How many order lines?",
                key="product_category_name")
    assert llm_cross_store_planner(_RawLlm(bad))("q", PACK_META) is None


def test_a_substring_of_a_shared_column_does_not_count_as_naming_it():
    """'product_id' must be named as a word. Loose matching would let 'product_identity'
    or a chatty sentence satisfy the guard by accident."""
    sneaky = _plan(filter_store="olist-catalog",
                   filter="List the distinct xproduct_idx values where category is X.",
                   measure_store="olist-orders", measure="How many order lines?")
    assert llm_cross_store_planner(_RawLlm(sneaky))("q", PACK_META) is None


def test_stores_that_share_no_column_at_all_are_refused():
    meta = [{"id": "a", "tables": [{"table": "t", "columns": ["x"]}]},
            {"id": "b", "tables": [{"table": "u", "columns": ["y"]}]}]
    plan = _plan(filter_store="a", filter="List the distinct x values.",
                 measure_store="b", measure="Total for those x values?")
    assert llm_cross_store_planner(_RawLlm(plan))("q", meta) is None


def test_every_pre_existing_guard_still_holds():
    """LAW 2 and the one-store check must not have been traded away for the new one."""
    for reply in ("SINGLE", "", "not json",
                  _plan(filter_store="olist-catalog", filter="List customer_id values.",
                        measure_store="olist-catalog", measure="m"),
                  _plan(filter_store="secret-hr", filter="List customer_id values.",
                        measure_store="olist-orders", measure="m")):
        assert llm_cross_store_planner(_RawLlm(reply))("q", PACK_META) is None, reply


def main():
    test_the_measured_d002_plan_is_refused()
    test_the_correct_d002_plan_is_accepted()
    test_f005_the_item_that_answers_today_is_unaffected()
    test_a_measure_store_no_key_reaches_is_REPAIRED_when_the_schema_settles_it()
    test_an_ambiguous_repair_is_refused_rather_than_guessed()
    test_a_repair_never_names_a_store_the_caller_cannot_see()
    print("  PASS  #524 a plan is refused unless the filter half names a key BOTH stores "
          "have; the correct D-002 plan and today's working F-005 plan both pass")
    test_a_declared_key_is_honoured_when_it_is_genuinely_shared()
    test_a_declared_key_that_is_not_shared_is_still_refused()
    test_a_substring_of_a_shared_column_does_not_count_as_naming_it()
    test_stores_that_share_no_column_at_all_are_refused()
    test_every_pre_existing_guard_still_holds()
    print("  PASS  #524 a declared key is checked not trusted, matching is word-wise, "
          "and the LAW 2 / one-store guards are intact")
    print("\n#524 PLANNER CARRY-KEY SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
