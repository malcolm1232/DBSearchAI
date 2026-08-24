"""#503 - ' and ' inside a VALUE phrase must not be taken as a clause joint (D-002, E-004).

Findings s20 recorded the symptom: "How many order lines were for products in the health
and beauty category?" reached the stores as "...in the health" + "beauty category" - a
stored category VALUE torn in half, so both halves failed. Worse, a decomposed question
is rescue-INELIGIBLE by design (#474's trigger requires an un-decomposed question), so
the tear also removed the cross-store rescue as a second chance.

Two layers produced it, and neither is the regex alone:
- `decompose_query` is a deliberately dumb fallback and does split on any " and ";
- in production an LLM decomposer is wired (#215) and for this question correctly
  answers "not compound" by returning ONE part - but `llm_decomposer`'s dropped-half
  guard (`len(out) == 1 and len(fallback(q)) > 1`) cannot tell a model that DROPPED a
  half from a model that rightly declined to split, so it overrode the model's correct
  answer with the regex's torn halves.

The fix is neither of those layers: it is the missing STORE test in `route()`. The #134
under-trigger rescue has always required that a split's halves route to DIFFERENT stores
before treating a question as compound ("terms and conditions" is one topic, not two);
the classifier-triggered branch trusted its split unconditionally. Applying the same test
to both branches decides the question on DATA rather than on grammar, and it repairs any
bad split - regardless of which decomposer produced it.

Run: PYTHONPATH=src python3 tests/selftest_503_value_phrase_split.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch import router  # noqa: E402

VALUE_PHRASE = "How many order lines were for products in the health and beauty category?"
CROSS_STORE = ("Which products generate the most support tickets, and how much revenue "
               "do they bring?")

#: Two BUs with disjoint vocabularies, so routing is deterministic under the lexical
#: HashingEmbedding: every word of the torn value phrase belongs to the CATALOG store.
SPEC = {
    "tenant": "acme",
    "stores": [
        {"id": "catalog", "kind": "local", "business_unit": "retail", "acl": ["retail"],
         "title": "Product catalog",
         "description": "products product categories order lines health beauty items "
                        "support tickets raised about products",
         "config": {"seed": [{"external_id": "p1", "title": "Categories", "uri": "u1",
                              "acl": ["staff"],
                              "text": "health and beauty is a product category"}],
                    "user_groups": {"u1": ["staff"]}}},
        {"id": "ledger", "kind": "local", "business_unit": "finance", "acl": ["fin"],
         "title": "Finance ledger",
         "description": "revenue invoices tax payments billing money",
         "config": {"seed": [{"external_id": "q1", "title": "Ledger", "uri": "u2",
                              "acl": ["staff"], "text": "revenue was nine million"}],
                    "user_groups": {"u1": ["staff"]}}},
    ],
}


def _svc(**kw):
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    cat = router.load_manifest(SPEC, registry=reg)
    # u1 sees both stores; retail_only sees the catalog alone (the LAW 2 case below).
    return router.RouterQueryService(
        cat, InMemoryIdentity({"u1": ["retail", "fin"], "retail_only": ["retail"]}),
        HashingEmbedding(), **kw)


def test_a_value_phrase_is_not_treated_as_compound():
    """THE #503 CASE: both torn halves belong to the SAME store, so the question is
    routed whole - which is also what makes it rescue-eligible again."""
    d = _svc().route("u1", VALUE_PHRASE)
    assert d.query_type != "compound", d.to_dict()
    assert d.sub_queries == [], d.to_dict()


def test_the_whole_question_reaches_a_store():
    """Routed whole is only useful if it still routes somewhere."""
    d = _svc().route("u1", VALUE_PHRASE)
    assert [s.store_id for s in d.stores] == ["catalog"], d.to_dict()


def test_a_genuine_cross_store_compound_still_decomposes():
    d = _svc().route("u1", CROSS_STORE)
    assert d.query_type == "compound", d.to_dict()
    assert {s.store_id for s in d.stores} == {"catalog", "ledger"}, d.to_dict()


def test_a_torn_split_survives_even_when_the_decomposer_is_wrong():
    """The store test repairs ANY bad split - here a decomposer that tears the value
    exactly as the live llm_decomposer guard did."""
    torn = ["How many order lines were for products in the health", "beauty category"]
    d = _svc(decomposer=lambda q: torn).route("u1", VALUE_PHRASE)
    assert d.query_type != "compound", d.to_dict()
    assert d.sub_queries == [], d.to_dict()


def test_an_uncovered_half_keeps_the_compound_so_it_is_still_disclosed():
    """The honesty guard on this fix, and the reason it is not just a store COUNT.

    A half no visible source can answer contributes no store id, so a naive "fewer than
    two stores means not compound" test collapses the question and silently drops
    compound_disclosure's "Not covered: '...'". Under LAW 2 that half is often unrouted
    precisely BECAUSE the store is invisible to this caller - so the same question would
    decompose for a privileged user and collapse for everyone else, and the person with
    LESS access would be the one told less about what went unanswered.
    """
    # The second half shares no vocabulary with any store this caller can see, which is
    # exactly the shape LAW 2 produces when the store that WOULD answer it is invisible.
    # "versus" puts this on the CLASSIFIER-triggered branch - the one this card changed.
    question = "product categories versus revenue invoices tax"
    halves = ["products categories order lines", "revenue invoices tax billing"]
    d = _svc(decomposer=lambda q: halves).route("retail_only", question)
    assert d.query_type == "compound", \
        f"an uncovered half must keep the compound alive so it is disclosed: {d.to_dict()}"
    assert [sq.decision.stores for sq in d.sub_queries][1] == [], d.to_dict()
    # and no store this caller cannot see is ever named (LAW 2)
    assert "ledger" not in repr(d.to_dict()), d.to_dict()


def main():
    test_a_value_phrase_is_not_treated_as_compound()
    test_the_whole_question_reaches_a_store()
    test_a_genuine_cross_store_compound_still_decomposes()
    test_a_torn_split_survives_even_when_the_decomposer_is_wrong()
    test_an_uncovered_half_keeps_the_compound_so_it_is_still_disclosed()
    print("  PASS  #503: a same-store split is not a compound - the value phrase routes "
          "whole (and stays rescue-eligible), a real cross-store ask still decomposes")
    print("\nVALUE-PHRASE-SPLIT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
