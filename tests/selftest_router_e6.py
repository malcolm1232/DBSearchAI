"""Phase E E6 — compound query decomposition (card #103).

A compound question is split into sub-queries; EACH sub-query is routed independently
over the caller's visible catalog (gate #1 per sub), executed with its OWN question
text, and merged into one answer. A sub-query the caller has no source for is
DISCLOSED, never silently backfilled and never a leak (design §7 scenario C).

Run: python3 tests/selftest_router_e6.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import ExtractiveLlm, HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch import router  # noqa: E402
from dbsearch.router.decompose import decompose_query  # noqa: E402

# Two BUs with disjoint vocabularies (deterministic lexical routing).
SPEC = {
    "tenant": "acme",
    "stores": [
        {"id": "hr-wiki", "kind": "local", "business_unit": "hr", "acl": ["hr-staff"],
         "title": "HR Wiki", "description": "human resources parental leave holidays onboarding benefits",
         "config": {"seed": [{"external_id": "hb", "title": "Handbook", "uri": "u1",
                              "acl": ["hr-staff"], "text": "parental leave lasts sixteen weeks"}],
                    "user_groups": {"alice": ["hr-staff"], "carol": ["hr-staff", "fin-staff"]}}},
        {"id": "fin-ledger", "kind": "local", "business_unit": "finance", "acl": ["fin-staff"],
         "title": "Finance Ledger", "description": "revenue invoices tax numbers ledger accounting",
         "config": {"seed": [{"external_id": "q3", "title": "Q3 Ledger", "uri": "u2",
                              "acl": ["fin-staff"], "text": "quarterly revenue was nine million"}],
                    "user_groups": {"eve": ["fin-staff"], "carol": ["hr-staff", "fin-staff"]}}},
    ],
}

COMPOUND_Q = "parental leave holidays versus revenue invoices tax"


def _svc(**kw):
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    cat = router.load_manifest(SPEC, registry=reg)
    identity = InMemoryIdentity({"alice": ["hr-staff"], "eve": ["fin-staff"],
                                 "carol": ["hr-staff", "fin-staff"]})
    return router.RouterQueryService(cat, identity, HashingEmbedding(), **kw)


def test_decompose_splits():
    assert decompose_query("HR attrition versus sales growth") == \
        ["HR attrition", "sales growth"]
    assert decompose_query("HR attrition vs. sales growth?") == \
        ["HR attrition", "sales growth"]
    assert decompose_query("revenue compared to headcount") == ["revenue", "headcount"]
    assert decompose_query("compare parental leave and expense policy") == \
        ["parental leave", "expense policy"]
    # single topic -> NOT split (the classifier over-triggers; the decomposer is the check)
    assert decompose_query("what is our parental leave policy") == \
        ["what is our parental leave policy"]
    # capped fan-out
    assert len(decompose_query("a1 b1 versus a2 b2 versus a3 b3 versus a4 b4")) <= 3


def test_the_reason_count_cannot_be_any_other_count():
    """#789. The guard above is only worth something if the number it names is UNIQUE to
    sub-questions. A three-way ask across two business units gives 3 sub-queries but 2 stores, so
    a reason built from the store count now says 2 where the assertion demands 3 - which is the
    difference between checking a count and checking that some digit appeared."""
    svc = _svc()
    d = svc.route("carol", "parental leave holidays versus revenue invoices tax "
                           "versus onboarding benefits")
    assert len(d.sub_queries) == 3, d.to_dict()
    assert len({s.store_id for s in d.stores}) == 2, \
        f"the counts must DIFFER for this guard to discriminate: {d.to_dict()}"
    assert "decomposed into 3 sub-queries" in d.reason, \
        f"the reason names the wrong count, or names none: {d.reason!r}"
    assert "decomposed into 2 sub-queries" not in d.reason, d.reason


def test_route_compound_unions_stores():
    svc = _svc()
    d = svc.route("carol", COMPOUND_Q)
    assert d.query_type == "compound", d.to_dict()
    assert d.method == "decompose", d.to_dict()
    assert len(d.sub_queries) == 2, d.to_dict()
    assert {s.store_id for s in d.stores} == {"hr-wiki", "fin-ledger"}, d.to_dict()
    # each sub routed to ITS store, carrying its own type + reason
    by_q = {sq.question: sq.decision for sq in d.sub_queries}
    assert by_q["parental leave holidays"].stores[0].store_id == "hr-wiki", d.to_dict()
    assert by_q["revenue invoices tax"].stores[0].store_id == "fin-ledger", d.to_dict()
    # #763: `"sub-quer" in d.reason` alone went near-vacuous once the string was shortened - it
    # is satisfied by the word surviving in any form. The reason has a job: say the question was
    # SPLIT and say into HOW MANY, since that count is the one thing `sub_queries` cannot convey
    # to a surface that renders prose only.
    assert "sub-quer" in d.reason, d.reason
    # #789: `str(len(d.sub_queries)) in d.reason` looked like a count check and was not one. On
    # THIS fixture the sub-queries, the stores and the routed subs are all 2, so the assertion is
    # satisfied by any of the three - swap it for `len(d.stores)` and it stays green. The whole
    # phrase is asserted instead, and `test_the_reason_count_cannot_be_any_other_count` below
    # drives an ask where the three numbers genuinely differ.
    assert f"decomposed into {len(d.sub_queries)} sub-queries" in d.reason, \
        f"the reason does not say how many sub-questions the ask became: {d.reason!r}"
    # #753: the reason SAYS the question was split and stops there. It used to enumerate every
    # sub-question and its target store - the same facts `sub_queries` above carries
    # structurally - and its consumers render both, so each sub-question was printed twice on
    # one screen. The prose must not compete with the structured field it duplicates.
    for sq in d.sub_queries:
        assert sq.question not in d.reason, \
            f"the reason restates a sub-question the structured field already carries: {d.reason!r}"
    for store in ("hr-wiki", "fin-ledger"):
        # #753 round 2, CORRECTING THIS COMMENT. It used to read "which selftest_478 forbids".
        # That is false and I wrote it: selftest_478 forbids naming a store the caller CANNOT
        # SEE (a LAW 2 leak check), and selftest_router_route.py:47 requires the semantic path's
        # reason to name its store. Nothing in this repo forbids an authorised store in a reason.
        # The rule here is narrower and is about duplication only: on the COMPOUND path the
        # store is already in `sub_queries`, so repeating it in prose prints it twice.
        assert store not in d.reason, \
            f"the compound reason names a target store `sub_queries` already carries, which " \
            f"prints it twice on every surface that renders both: {d.reason!r}"


def test_sub_routing_never_leaks_invisible_stores():
    svc = _svc()
    d = svc.route("alice", COMPOUND_Q)          # alice: hr-staff only
    blob = repr(d.to_dict())
    assert "fin-ledger" not in blob, "sub-query routing leaked an invisible store (gate #1)"


def test_ask_compound_merges_both_stores():
    svc = _svc()
    res = svc.ask("carol", COMPOUND_Q, ExtractiveLlm())
    cited_docs = {c.get("doc") for c in res.citations}
    assert {"hb", "q3"} <= cited_docs, res.to_dict()
    assert res.disclosure == "", res.disclosure            # full coverage -> no disclosure
    # outcomes carry per-sub attribution (audit)
    subs = {o["sub_question"] for o in res.outcomes}
    assert subs == {"parental leave holidays", "revenue invoices tax"}, res.outcomes


def test_ask_partial_visibility_discloses_not_leaks():
    svc = _svc()
    res = svc.ask("alice", COMPOUND_Q, ExtractiveLlm())
    blob = repr(res.to_dict())
    assert "fin-ledger" not in blob, "invisible store leaked into the result"
    assert "nine million" not in blob, "finance content leaked to alice"
    assert "revenue invoices tax" in res.disclosure, res.disclosure
    assert "sixteen weeks" in res.answer, res.answer       # the covered half still answers


def test_non_compound_path_unchanged():
    svc = _svc()
    d = svc.route("carol", "what is our parental leave policy")
    assert d.query_type == "semantic" and d.sub_queries == [], d.to_dict()


def test_statement_form_and_join_still_decomposes():
    # #134: without a trailing "?" the classifier misses the "and" join — but the
    # halves route to DIFFERENT stores, so the question genuinely spans stores and
    # must be treated as compound, never routed whole to one store.
    svc = _svc()
    d = svc.route("carol", "parental leave holidays and revenue invoices tax")
    assert d.query_type == "compound", d.to_dict()
    assert {s.store_id for s in d.stores} == {"hr-wiki", "fin-ledger"}, d.to_dict()


def test_same_store_split_stays_single():
    # the rescue must NOT over-trigger: an "and"-joined phrase whose halves land on
    # the SAME store (idiom / one topic) keeps the plain single routing
    svc = _svc()
    d = svc.route("carol", "parental leave and holidays benefits")
    assert d.query_type != "compound" and d.sub_queries == [], d.to_dict()
    assert [s.store_id for s in d.stores] == ["hr-wiki"], d.to_dict()


def test_analytical_dominance_no_longer_drops_half():
    # THE #134 repro: SQL keywords ("total ... amount") made classify say
    # 'analytical', the whole question routed to the SQL store, and the parental-
    # leave half vanished with NO disclosure. Both halves must now answer.
    spec = {
        "tenant": "acme",
        "stores": [
            SPEC["stores"][0],                    # hr-wiki (docs)
            {"id": "sales-db", "kind": "csv", "business_unit": "sales",
             "acl": ["hr-staff"], "title": "Sales DB",
             "description": "sales figures totals sum amount by region",
             "config": {"tables": {"sales": {"columns": ["region", "amount"],
                                             "rows": [["emea", 100], ["apac", 60]]}}}},
        ],
    }
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    reg.register(router.CsvSqlProvider())
    cat = router.load_manifest(spec, registry=reg)
    identity = InMemoryIdentity({"alice": ["hr-staff"]})
    svc = router.RouterQueryService(cat, identity, HashingEmbedding())
    res = svc.ask("alice", "parental leave policy and total sales amount",
                  ExtractiveLlm())
    stores_answering = {o["store_id"] for o in res.outcomes if o["status"] == "ok"}
    assert stores_answering == {"hr-wiki", "sales-db"}, res.to_dict()
    assert "sixteen weeks" in res.answer, res.answer      # the half that used to vanish


def test_decomposer_is_injectable():
    calls = []

    def fake(question):
        calls.append(question)
        return ["parental leave holidays", "revenue invoices tax"]

    svc = _svc(decomposer=fake)
    d = svc.route("carol", "leave versus tax")
    assert calls == ["leave versus tax"], calls
    assert len(d.sub_queries) == 2, d.to_dict()


def main():
    print("Phase E E6 compound-decomposition self-test:")
    test_decompose_splits()
    print("  PASS  deterministic decomposer (versus/vs/compared/and, cap, no false split)")
    test_route_compound_unions_stores()
    test_sub_routing_never_leaks_invisible_stores()
    print("  PASS  per-sub routing + store union + gate #1 per sub")
    test_ask_compound_merges_both_stores()
    test_ask_partial_visibility_discloses_not_leaks()
    print("  PASS  merged compound answer + scenario-C disclosure without leak")
    test_non_compound_path_unchanged()
    test_decomposer_is_injectable()
    print("  PASS  non-compound unchanged + injectable decomposer seam")
    test_statement_form_and_join_still_decomposes()
    test_same_store_split_stays_single()
    test_analytical_dominance_no_longer_drops_half()
    print("  PASS  #134 under-trigger rescue: cross-store 'and' joins decompose, "
          "same-store idioms don't, no half silently dropped")
    print("\nE6 COMPOUND-DECOMPOSITION SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
