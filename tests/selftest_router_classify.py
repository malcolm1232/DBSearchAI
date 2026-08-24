"""Phase E E2 — query classifier + RoutingDecision self-test.
Run: python3 tests/selftest_router_classify.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.classify import classify_query  # noqa: E402
from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402


def test_analytical():
    for q in ["What is the total revenue in Q3?", "How many tickets are open?",
              "average deal size by region", "count of employees per business unit"]:
        assert classify_query(q) == "analytical", q


def test_exact():
    for q in ["show invoice #4471", "look up employee id 90210", "get record 12345"]:
        assert classify_query(q) == "exact", q


def test_compound():
    for q in ["HR attrition versus sales headcount growth",
              "compare AI revenue and the post-mortem findings"]:
        assert classify_query(q) == "compound", q


def test_semantic_default():
    for q in ["what is our parental leave policy", "summarise the onboarding guide"]:
        assert classify_query(q) == "semantic", q


def test_decision_to_dict():
    d = RoutingDecision(query_type="semantic",
                        stores=[RoutedStore("hr-wiki", "hr", 0.82)],
                        candidates=[RoutedStore("hr-wiki", "hr", 0.82),
                                    RoutedStore("fin-ledger", "finance", 0.10)],
                        confidence=0.82, reason="best match: HR Wiki (hr)",
                        method="prefilter")
    j = d.to_dict()
    assert j["query_type"] == "semantic" and j["method"] == "prefilter", j
    assert j["stores"][0]["store_id"] == "hr-wiki", j
    assert len(j["candidates"]) == 2, j


def main():
    print("Phase E E2 classify self-test:")
    test_analytical()
    test_exact()
    test_compound()
    test_semantic_default()
    test_decision_to_dict()
    print("  PASS  analytical / exact / compound / semantic / decision.to_dict")
    print("\nE2 CLASSIFY SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
