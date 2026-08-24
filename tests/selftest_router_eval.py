"""Phase F (card #129) — federated answer-quality eval harness over RouterQueryService.
Hermetic: in-memory catalog (indexed + federated-SQL stores), HashingEmbedding, an
extractive fake LLM — no cloud, runs in the suite. Proves the harness scores routing,
retrieval, faithfulness AND the moat (gate #1/#2 leak) as a number, and that the
threshold gate catches a regression.

    python3 tests/selftest_router_eval.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch import router  # noqa: E402
from dbsearch.eval.router_eval import (  # noqa: E402
    RouterGoldenItem, run_router_eval, score_item,
)

SPEC = {
    "tenant": "acme",
    "stores": [
        {"id": "hr-wiki", "kind": "local", "business_unit": "hr", "acl": ["all-staff"],
         "title": "HR Wiki",
         "description": "human resources parental leave holidays onboarding benefits",
         "config": {"seed": [{"external_id": "hb", "title": "Handbook", "uri": "u1",
                              "acl": ["all-staff"],
                              "text": "parental leave is sixteen weeks"}],
                    "user_groups": {"alice": ["all-staff", "deal-team"],
                                    "bob": ["all-staff"]}}},
        {"id": "fin-ledger", "kind": "local", "business_unit": "finance",
         "acl": ["deal-team"], "title": "Finance Ledger",
         "description": "revenue invoices tax numbers ledger accounting confidential",
         "config": {"seed": [{"external_id": "q3", "title": "Q3", "uri": "u2",
                              "acl": ["deal-team"],
                              "text": "confidential revenue four point two million"}],
                    "user_groups": {"alice": ["all-staff", "deal-team"],
                                    "bob": ["all-staff"]}}},
        {"id": "sales-figures", "kind": "csv", "business_unit": "sales",
         "acl": ["all-staff"], "title": "Sales figures",
         "description": "sales figures totals sum amount by region",
         "config": {"tables": {"sales": {"columns": ["region", "amount"],
                                         "rows": [["emea", 100], ["apac", 60]]}}}},
    ],
}


class _ExtractiveLlm:
    """Faithful stand-in: echoes the retrieved context (so key facts in evidence appear
    in the answer) and abstains when nothing was retrieved."""

    def answer(self, question, context_chunks):
        if not context_chunks:
            return {"answer": "I couldn't find anything you have access to about that.",
                    "citations": []}
        return {"answer": " ".join(context_chunks), "citations": []}


def _svc():
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    reg.register(router.CsvSqlProvider())
    cat = router.load_manifest(SPEC, registry=reg)
    identity = InMemoryIdentity({"alice": ["all-staff", "deal-team"],
                                 "bob": ["all-staff"]})
    return router.RouterQueryService(cat, identity, HashingEmbedding())


GOLDEN = [
    RouterGoldenItem(question="what is our parental leave policy",
                     expected_stores=["hr-wiki"], relevant_docs=["hb"],
                     key_facts=["sixteen weeks"], as_user="alice"),
    RouterGoldenItem(question="total amount by region",
                     expected_stores=["sales-figures"], key_facts=["emea"],
                     as_user="alice"),
    # unanswerable: the semantic router still picks a nearest store (that's its design);
    # correctness = retrieval finds nothing and the answer abstains, so routing unscored
    RouterGoldenItem(question="what is the meaning of life",
                     answerable=False, as_user="bob"),
    # no-access user: NO accessible store exists, so expect NO store routed ([]) + abstain
    RouterGoldenItem(question="what is our parental leave policy",
                     expected_stores=[], answerable=False, as_user="mallory"),
    # THE MOAT as a scored item: bob (no deal-team) asks about finance — the store's
    # existence, content, and even the name must never surface.
    RouterGoldenItem(question="confidential revenue ledger invoices",
                     as_user="bob",
                     forbidden_stores=["fin-ledger"],
                     forbidden_facts=["four point two million"]),
    # #134 regression gate: statement-form "and" join with SQL-keyword dominance —
    # BOTH halves must answer (docs fact + SQL total), neither silently dropped.
    RouterGoldenItem(question="parental leave policy and total sales amount",
                     expected_stores=["hr-wiki", "sales-figures"],
                     key_facts=["sixteen weeks", "160"], as_user="alice"),
]


def test_score_item_routing_retrieval_and_facts():
    svc = _svc()
    llm = _ExtractiveLlm()
    g = GOLDEN[0]
    result = svc.ask(g.as_user, g.question, llm).to_dict()
    s = score_item(result, g)
    assert s["routing_hit"] is True and s["routing_precision"] == 1.0, s
    assert s["hit_at_k"] is True, s
    assert s["key_fact_recall"] == 1.0, s
    assert s["leak_score"] == 1.0, s          # nothing forbidden here -> clean


def test_leak_item_scores_one_when_gate1_holds():
    svc = _svc()
    result = svc.ask("bob", "confidential revenue ledger invoices",
                     _ExtractiveLlm()).to_dict()
    s = score_item(result, GOLDEN[4])
    assert s["leak_score"] == 1.0, ("bob must not see fin-ledger anywhere", result)
    # routing is NOT scored here (expected_stores=None): bob's query may legitimately
    # route to an all-staff store — only the leak metric is this item's point


def test_leak_score_zero_when_a_forbidden_store_leaks():
    # feed a deliberately-leaky result to prove the metric is non-vacuous
    leaky = {"answer": "revenue is four point two million", "citations": [],
             "evidence": [], "routing": {"stores": [{"store_id": "fin-ledger"}]},
             "outcomes": [], "disclosure": ""}
    s = score_item(leaky, GOLDEN[4])
    assert s["leak_score"] == 0.0, s


def test_run_router_eval_report_and_threshold_gate():
    svc = _svc()
    report = run_router_eval(svc, GOLDEN, _ExtractiveLlm())
    summ = report.summary()
    assert summ["routing_hit"] == 1.0, summ         # all 4 routing-scored items hit
    assert summ["leak_score"] == 1.0, summ          # moat intact across the set
    assert summ["key_fact_recall"] >= 0.99, summ
    assert summ["abstention"] == 1.0, summ          # the unanswerable item abstained
    assert report.passed({"routing_hit": 0.9, "leak_score": 1.0,
                          "key_fact_recall": 0.9, "abstention": 1.0}) is True, summ
    # a threshold nobody meets fails the gate (non-vacuous)
    assert report.passed({"leak_score": 1.01}) is False, summ


def main():
    print("Phase F federated eval-harness self-test:")
    test_score_item_routing_retrieval_and_facts()
    test_leak_item_scores_one_when_gate1_holds()
    test_leak_score_zero_when_a_forbidden_store_leaks()
    print("  PASS  score_item: routing/retrieval/facts + leak metric (non-vacuous)")
    test_run_router_eval_report_and_threshold_gate()
    print("  PASS  run_router_eval report aggregates + threshold gate")
    print("\nPHASE F EVAL-HARNESS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
