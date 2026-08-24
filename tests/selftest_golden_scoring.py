"""Two-stage scoring (spec 2026-07-31 section 4). Hermetic: fake result dicts shaped
exactly like the /router/ask response surface.

    python3 tests/selftest_golden_scoring.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.eval.golden.pack import GoldenQ  # noqa: E402
from dbsearch.eval.golden.stage1 import mrr, recall_at_k, score_stage1  # noqa: E402
from dbsearch.eval.golden.stage2 import attribute, gold_value, score_stage2  # noqa: E402

_DOC_RESULT = {
    "answer": "parental leave is sixteen weeks",
    "routing": {"stores": [{"store_id": "hr-wiki"}]},
    "citations": [{"store_id": "hr-wiki", "doc": "leave-policy"}],
    "evidence": [{"provenance": {"locator": "leave-policy#p2"}}],
}
_ITEM = GoldenQ(id="A-001", capability="A", question="q",
                expect_stores=("hr-wiki",), doc_qrels=("leave-policy",),
                chunk_qrels=("leave-policy#p2",), negative_qrels=("leave-draft-2019",))


def test_stage1_doc_pass():
    s = score_stage1(_DOC_RESULT, _ITEM)
    assert s["failures"] == []
    assert s["metrics"]["doc_recall_at_k"] == 1.0
    assert s["metrics"]["doc_mrr"] == 1.0
    assert s["metrics"]["chunk_recall_at_k"] == 1.0


def test_stage1_distractor_cited_fails():
    bad = dict(_DOC_RESULT, citations=[{"store_id": "hr-wiki", "doc": "leave-draft-2019"}])
    s = score_stage1(bad, _ITEM)
    assert "distractor-cited" in s["failures"]
    assert "doc-qrels" in s["failures"]


def test_stage1_sql_table_identity_only():
    item = GoldenQ(id="B-001", capability="B", question="q",
                   expect_stores=("sales-figures",), gold_table="spend")
    res = {"answer": "812000", "routing": {"stores": [{"store_id": "sales-figures"}]},
           "citations": [{"store_id": "sales-figures", "table": "spend"}], "evidence": []}
    s = score_stage1(res, item)
    assert s["failures"] == []
    assert s["metrics"]["table_hit"] is True
    assert s["metrics"]["doc_recall_at_k"] is None


def test_stage1_forbidden_store_routed():
    """protection defaults to "public" (not "refused") - this item exercises the
    empty-expect_stores + unanswerable strictness that STILL applies to non-refused
    items (amendment 260731a only carves out protection="refused"). Renamed off "G-001"
    to "X-001" so the id doesn't imply G-capability/refused semantics - this is a
    generic non-refused unanswerable-item test, not a G-shaped one."""
    item = GoldenQ(id="X-001", capability="G", question="q", expect_stores=(),
                   forbid_stores=("exec-comp",), answerable=False)
    res = {"answer": "no accessible data",
           "routing": {"stores": [{"store_id": "exec-comp"}]}, "citations": [], "evidence": []}
    s = score_stage1(res, item)
    assert "forbidden-store" in s["failures"]
    assert "routing" in s["failures"]


def test_refused_item_routing_unscored():
    """Amendment 260731a: protection="refused" items skip the empty-expect_stores
    routing check entirely - routing_hit/routing_precision stay None and "routing" never
    appears in failures, even though stores were routed and the item is unanswerable.
    Design #339's third bucket ("refused for everyone") makes the commercial assertion
    that the ANSWER refuses without confirming existence, not that the router fans out
    to nothing. The forbidden-store check still applies unconditionally: a refused item
    whose forbid_stores actually get routed must still fail "forbidden-store"."""
    item = GoldenQ(id="G-001", capability="G", question="q", expect_stores=(),
                   forbid_stores=("exec-comp",), answerable=False, protection="refused")
    routed_unrelated = {"answer": "no accessible data",
                        "routing": {"stores": [{"store_id": "hr-wiki"}]},
                        "citations": [], "evidence": []}
    s = score_stage1(routed_unrelated, item)
    assert s["metrics"]["routing_hit"] is None, s["metrics"]
    assert "routing" not in s["failures"], s["failures"]
    routed_forbidden = dict(routed_unrelated,
                            routing={"stores": [{"store_id": "exec-comp"}]})
    s2 = score_stage1(routed_forbidden, item)
    assert "forbidden-store" in s2["failures"], s2["failures"]
    assert "routing" not in s2["failures"], s2["failures"]


def test_rank_metrics():
    assert recall_at_k(["a", "b", "c"], ["b", "z"], 3) == 0.5
    assert mrr(["x", "b"], ["b"]) == 0.5
    assert mrr(["x"], ["b"]) == 0.0


def test_gold_value_independent_engine():
    import csv
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "spend.csv"
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerows([["region", "channel", "amount"],
                         ["us", "paid-search", "812000"],
                         ["us", "social", "90000"],
                         ["emea", "paid-search", "150000"]])
        v = gold_value({"synapse": {"spend": p}},
                       "SELECT SUM(amount) FROM spend WHERE region='us' AND channel='paid-search'")
        assert v == 812000.0


def test_stage2_execution_accuracy_and_facts():
    item = GoldenQ(id="B-001", capability="B", question="q",
                   gold_sql="x", key_facts=("812000",))
    ok = score_stage2({"answer": "We spent 812,000 on paid search."}, item, gold=812000.0)
    assert ok["failures"] == []
    bad = score_stage2({"answer": "We spent 8120001."}, item, gold=812000.0)
    assert "execution-accuracy" in bad["failures"] and "key-facts" in bad["failures"]


def test_stage2_leak_blob():
    item = GoldenQ(id="L-001", capability="LAW2", question="q",
                   forbidden_facts=("project falcon", "4200000"), forbid_stores=("fin-ledger",))
    leaked = {"answer": "nothing", "routing": {"reason": "skipped fin-ledger"}}
    s = score_stage2(leaked, item)
    assert "leak" in s["failures"]
    clean = score_stage2({"answer": "I couldn't find anything you have access to."}, item)
    assert "leak" not in clean["failures"]


def test_stage2_abstention():
    item = GoldenQ(id="G-001", capability="G", question="q", answerable=False)
    s = score_stage2({"answer": "I couldn't find anything you have access to."}, item)
    assert s["failures"] == [] and s["metrics"]["abstained_ok"] is True
    s = score_stage2({"answer": "The exec-comp database holds salaries."}, item)
    assert "abstention" in s["failures"]


def test_attribution_precedence():
    ok = {"failures": []}
    assert attribute(ok, ok) == "pass"
    assert attribute({"failures": ["routing"]}, {"failures": ["leak"]}) == "leak"
    assert attribute({"failures": ["routing", "doc-qrels"]}, ok) == "routing-miss"
    assert attribute({"failures": ["doc-qrels"]}, ok) == "retrieval-miss"
    assert attribute(ok, {"failures": ["key-facts"]}) == "synthesis-miss"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    print("selftest_golden_scoring: all green")
