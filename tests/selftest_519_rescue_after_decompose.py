"""#519 - a bad decomposition must not lock out the cross-store rescue.

D-002 and E-004 are the same shape as F-005: a measure in one store filtered by a column
in another. F-005 passes, they do not, and the live probe (findings s25) showed the only
difference is which routing method the planner chose. Both failing items are DECOMPOSED
first, and the decomposer tears the value phrase mid-conjunction:

    "How many order lines were for products in the health and beauty category?"
        -> "How many order lines were for products in the health"   (olist-orders)
        -> "beauty category"                                        (olist-catalog)

#503 tried to rejoin such a tear when both halves route to the same store. They do not -
each fragment routes on its own words - so that test can never fire here. It is aimed at
the wrong layer: the store-agreement signal is measured on the fragments the tear
produced, so it is downstream of the tear it is meant to detect.

The mechanism that DOES answer this shape is the #474 rescue, and it was gated on
`not decision.sub_queries` - so a bad decomposition locked out the one thing that could
recover from it, even though the planner's plan for these questions is the gold join.

#474's three safety properties do not depend on that gate and are re-pinned here: the
rescue still fires only on a REFUSAL (never over a delivered answer), still sees only
caller-visible metadata, and is still trusted only when a half is genuinely BOUND to
carried keys.

Run: PYTHONPATH=src python3 tests/selftest_519_rescue_after_decompose.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch.core.copy import NO_EVIDENCE_ANSWER  # noqa: E402
from dbsearch.router.catalog import STORE, CatalogNode, StoreCatalog  # noqa: E402
from dbsearch.router.router_service import RouterQueryService  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    CannotAnswerFromSchema, FederatedSqlStore, SqliteEngine,
)

# D-002's shape, and the tear the live decomposer actually produces on it.
ORIGINAL_Q = "How many order lines were for products in the health and beauty category?"
TORN = ["How many order lines were for products in the health", "beauty category"]

# What the cross-store planner returns for it - verified against the live planner in s25.
FILTER_Q = ("List the distinct product_category_name values where product_category_name "
            "is 'health and beauty'. One column only, no joins.")
MEASURE_Q = "How many order lines were for those product_category_name values?"

CATALOG_SQL = ("SELECT product_category_name FROM categories "
               "WHERE product_category_name_english = 'health_beauty'")
ORDERS_SQL = "SELECT COUNT(*) AS lines FROM order_items"


class _DecliningLlm:
    """Answers nothing: every synthesis is the canonical decline. The torn halves DO
    return rows - each fragment is answerable alone, just not usefully - so 'no evidence'
    never fires and the product's own final judgment is the only honest trigger (#474)."""

    def answer(self, question, context):
        return {"answer": NO_EVIDENCE_ANSWER, "context": context}


class _AnswerLlm:
    def answer(self, question, context):
        return {"answer": "ok", "context": context}


class _RefusingThenAnsweringLlm:
    """The live shape (#474): the plain path's evidence is junk, so the model refuses in
    its OWN words; the rescue's bound evidence is answerable, so it answers. Keyed on the
    call, not on the text, so the test does not depend on how a row is rendered."""

    def __init__(self):
        self.calls = 0

    def answer(self, question, context):
        self.calls += 1
        return {"answer": "I do not have that information." if self.calls == 1
                else "213 order lines."}


def _gen(mapping):
    def gen(question, schema):
        if question in mapping:
            return mapping[question]
        raise CannotAnswerFromSchema("this store holds nothing of that kind")
    return gen


def _fixture(planner, decomposer, catalog_map=None, orders_map=None):
    catalog = FederatedSqlStore(
        "olist-catalog", "commerce", "Catalog",
        "products categories category names beauty health catalogue",
        SqliteEngine.from_tables({"categories": {
            "columns": ["product_category_name", "product_category_name_english"],
            "rows": [["saude_beleza", "health_beauty"], ["cama_mesa", "bed_bath_table"]]}}),
        sql_generator=_gen(catalog_map if catalog_map is not None
                           else {FILTER_Q: CATALOG_SQL, TORN[1]: CATALOG_SQL}))
    orders = FederatedSqlStore(
        "olist-orders", "commerce", "Orders",
        "orders order lines items revenue price totals counts",
        SqliteEngine.from_tables({"order_items": {
            "columns": ["order_id", "product_category_name"],
            "rows": [["o1", "saude_beleza"], ["o2", "saude_beleza"], ["o3", "cama_mesa"]]}}),
        sql_generator=_gen(orders_map if orders_map is not None
                           else {MEASURE_Q: ORDERS_SQL, TORN[0]: ORDERS_SQL}))
    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind="tenant", parent_id=None, acl=["p"]))
    for s in (catalog, orders):
        cat.register(CatalogNode(id=s._store_id, kind=STORE, parent_id="t", acl=["p"],
                                 profile=s.profile(), store=s))
    svc = RouterQueryService(cat, InMemoryIdentity({"u1": ["p"]}), HashingEmbedding(),
                             cross_store_planner=planner, decomposer=decomposer)
    return svc, catalog, orders


def _plan(seen=None):
    def planner(question, stores):
        if seen is not None:
            seen["question"] = question
            seen["stores"] = stores
        return [("olist-catalog", FILTER_Q), ("olist-orders", MEASURE_Q)]
    return planner


def test_a_torn_decomposition_that_declines_still_reaches_the_rescue():
    """THE #519 defect. The question is decomposed (badly), every half answers something
    useless, the synthesizer declines - and before this fix the rescue was skipped purely
    because sub_queries was non-empty."""
    seen = {}
    svc, _catalog, orders = _fixture(_plan(seen), lambda q: TORN)
    result = svc.ask("u1", ORIGINAL_Q, _RefusingThenAnsweringLlm())

    assert seen.get("question") == ORIGINAL_Q, (
        "the rescue never ran: a decomposed decline still locks out the planner")
    bound = orders.audit_trail[-1]["sql"]
    assert "IN ('saude_beleza')" in bound, bound
    assert "213" in result.answer, result.answer


def test_the_rescue_plans_on_the_whole_question_not_a_fragment():
    """The tear must not reach the planner - a fragment plan would be a second wrong
    answer built on the first one's mistake."""
    seen = {}
    svc, _c, _o = _fixture(_plan(seen), lambda q: TORN)
    svc.ask("u1", ORIGINAL_Q, _DecliningLlm())
    assert seen["question"] == ORIGINAL_Q


# --- #474's safety properties, re-pinned now that the gate is gone --------------------

def test_a_decomposed_question_that_ANSWERS_never_rescues():
    """The property that made the rescue safe to bolt on: it fires on a refusal, so a
    delivered answer - even a wrong one - is never overridden. Removing the sub_queries
    gate must not weaken this."""
    seen = {}
    svc, _c, _o = _fixture(_plan(seen), lambda q: TORN)
    result = svc.ask("u1", ORIGINAL_Q, _AnswerLlm())
    assert result.answer == "ok"
    assert "question" not in seen, "the rescue overrode a delivered answer"


def test_a_decomposed_decline_the_planner_declines_to_split_is_unchanged():
    """A genuinely compound question is not a cross-store question. The planner says so,
    the decline stands, and the reason is disclosed (LAW 8)."""
    svc, _c, _o = _fixture(lambda q, s: None, lambda q: TORN)
    result = svc.ask("u1", ORIGINAL_Q, _DecliningLlm())
    assert result.answer == NO_EVIDENCE_ANSWER
    assert "single-store question" in result.disclosure, result.disclosure


def test_an_unaligned_rescue_after_a_decompose_is_still_rejected():
    """The #495/G-001 trust rule: a rescue whose halves never bound to carried keys is an
    ordinary unbound answer to a question the plain path already declined. Reject it."""
    svc, _c, _o = _fixture(_plan(), lambda q: TORN,
                           catalog_map={TORN[1]: CATALOG_SQL},   # filter half unanswerable
                           orders_map={MEASURE_Q: ORDERS_SQL, TORN[0]: ORDERS_SQL})
    result = svc.ask("u1", ORIGINAL_Q, _DecliningLlm())
    assert result.answer == NO_EVIDENCE_ANSWER
    assert "rescue attempted and rejected" in result.disclosure, result.disclosure


def test_no_planner_still_means_todays_behaviour_exactly():
    svc, _c, _o = _fixture(None, lambda q: TORN)
    result = svc.ask("u1", ORIGINAL_Q, _DecliningLlm())
    assert result.answer == NO_EVIDENCE_ANSWER
    assert "rescue" not in result.disclosure.lower(), result.disclosure


def main():
    test_a_torn_decomposition_that_declines_still_reaches_the_rescue()
    test_the_rescue_plans_on_the_whole_question_not_a_fragment()
    print("  PASS  #519 a torn decomposition no longer locks out the #474 rescue, and "
          "the rescue plans on the WHOLE question")
    test_a_decomposed_question_that_ANSWERS_never_rescues()
    test_a_decomposed_decline_the_planner_declines_to_split_is_unchanged()
    test_an_unaligned_rescue_after_a_decompose_is_still_rejected()
    test_no_planner_still_means_todays_behaviour_exactly()
    print("  PASS  #474's safety properties survive the wider trigger: fires only on a "
          "refusal, trusted only when aligned, silent with no planner")
    print("\n#519 RESCUE-AFTER-DECOMPOSE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
