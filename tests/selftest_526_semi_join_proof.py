"""#526 - a key-carry answer's proof must be the SEMI-JOIN it actually computed.

Measured live (findings s27/s28) after #519 and #524 landed. D-002's chain completes and
the item was still red:

    filter   olist-catalog  WHERE product_category_name = 'beleza_saude'  -> 124 rows
    measure  olist-orders   COUNT(*) WHERE product_id IN (...124 keys...) -> COUNT(*)=213
    answer                  "I do not have that information."

213 is the gold. The decline is not obviously wrong, which is why this was a design gap
rather than a bug. The carry rows are correctly stripped as mechanics (s19), so the model
sees one row and a `[query]` block holding two DISCONNECTED statements - the second with
its carried list elided to "<124 carried values - the full list is in the shown query>".
That placeholder tells the model the values are somewhere it cannot see, so reporting
missing information is the honest reading of what it was given.

Two candidate fixes were measured against the real model before this one was written.
A `[plan]` line spelling out the two steps converted NOTHING on its own. Rewording the
placeholder DID convert 3/3 - and near-identical phrasings flipped straight back to a
decline, which is prompt overfitting rather than a fix, so it was rejected.

What ships instead is the accurate statement of what ran. A semi-join is what the two
halves computed, so nesting the filter half inside the measure query is not a hint - it
is the complete, self-contained proof, and it needs no placeholder:

    SELECT COUNT(*) FROM order_items WHERE product_id IN (
        SELECT product_id FROM products WHERE product_category_name = 'beleza_saude')

Dropping the now-nested statement from the proof list is load-bearing and measured:
keeping both fails 3/3, the single combined statement answers 3/3. The s18/s19 rule once
more - findings, not workings.

Prompt-only: evidence, citations and footnotes keep every executed statement verbatim
with its re-runnable token, so the reader's trail is unchanged.

Run: PYTHONPATH=src python3 tests/selftest_526_semi_join_proof.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.decision import RoutedStore, RoutingDecision, SubQuery  # noqa: E402
from dbsearch.router.evidence import ROW, Evidence  # noqa: E402
from dbsearch.router.executor import DispatchReport, OK, StoreOutcome  # noqa: E402
from dbsearch.router.synthesizer import synthesize  # noqa: E402

QUESTION = "How many order lines were for products in the health and beauty category?"
FILTER_Q = "List the distinct product_id values where product_category_name is 'health and beauty'."
MEASURE_Q = "How many order_items were for those product_id values?"

FILTER_SQL = "SELECT DISTINCT product_id FROM products WHERE product_category_name = 'beleza_saude'"
MEASURE_SQL = "SELECT COUNT(*) FROM order_items WHERE product_id IN ('a', 'b', 'c')"


class _Llm:
    def __init__(self):
        self.contexts = []

    def answer(self, question, context):
        self.contexts.append(list(context))
        return {"answer": "213 order lines."}


def _half(store_id, question):
    routed = [RoutedStore(store_id, "commerce", 1.0, why="named by the cross-store planner")]
    return SubQuery(question, RoutingDecision(
        query_type="analytical", stores=routed, candidates=routed, confidence=1.0,
        method="cross-store-rescue", reason=f"the planner placed this half on {store_id}"))


def _rescue_decision():
    subs = [_half("olist-catalog", FILTER_Q), _half("olist-orders", MEASURE_Q)]
    stores = [sq.decision.stores[0] for sq in subs]
    return RoutingDecision(query_type="compound", stores=stores, candidates=stores,
                           confidence=1.0, method="cross-store-rescue",
                           reason="cross-store key-carry rescue (#474, ADR 0014-B)",
                           sub_queries=subs)


def _rescue_report():
    """The live shape: the filter half's carry rows marked mechanics (so they never reach
    the prompt), and the measure half's single fact row."""
    report = DispatchReport()
    report.evidence_by_store["olist-catalog"] = [
        Evidence("olist-catalog", "commerce", ROW, f"product_id={c}",
                 provenance={"sql": FILTER_SQL, "mechanics": True}) for c in ("a", "b")]
    report.evidence_by_store["olist-orders"] = [
        Evidence("olist-orders", "commerce", ROW, "COUNT(*)=213",
                 provenance={"sql": MEASURE_SQL,
                             "bind": {"aligned": True, "column": "product_id"}})]
    report.outcomes = [StoreOutcome("olist-catalog", "commerce", OK, count=2,
                                    sub_question=FILTER_Q),
                       StoreOutcome("olist-orders", "commerce", OK, count=1,
                                    sub_question=MEASURE_Q)]
    return report


def _query_line(context):
    return next((c for c in context if c.startswith("[query]")), "")


def test_the_proof_is_the_semi_join_the_two_halves_computed():
    """THE #526 defect. The model was shown two disconnected statements, the second with
    its carried list elided into a placeholder that says the values are somewhere it
    cannot see - so it reported missing information, which is the honest reading of what
    it was given. The proof is now the semi-join that was actually computed."""
    llm = _Llm()
    synthesize(QUESTION, _rescue_report(), _rescue_decision(), llm)
    line = _query_line(llm.contexts[0])
    assert f"product_id IN ({FILTER_SQL})" in line, line
    assert "carried values" not in line, line          # no placeholder survives
    assert "'a', 'b', 'c'" not in line, line           # no raw key list either


def test_the_nested_filter_statement_is_not_also_shown_standalone():
    """Load-bearing and measured: keeping BOTH the standalone filter statement and the
    semi-join fails 3/3 on the real model, while the single combined statement answers
    3/3. Same s18/s19 rule - findings, not workings."""
    llm = _Llm()
    synthesize(QUESTION, _rescue_report(), _rescue_decision(), llm)
    line = _query_line(llm.contexts[0])
    assert line.count(FILTER_SQL) == 1, line
    assert not line.split("database: ")[1].startswith(FILTER_SQL), line


def test_the_carry_rows_still_never_reach_the_prompt():
    """s19 is not traded away for this. The plan is ONE line ABOUT the workings; the
    workings themselves stay out - that distinction is the whole finding."""
    llm = _Llm()
    synthesize(QUESTION, _rescue_report(), _rescue_decision(), llm)
    context = llm.contexts[0]
    assert not any("product_id=a" in c or "product_id=b" in c for c in context), context
    assert any("COUNT(*)=213" in c for c in context), context


def test_a_plain_single_store_answer_is_untouched():
    """No carry, nothing to nest - the ordinary proof must be byte-identical."""
    routed = [RoutedStore("olist-orders", "commerce", 1.0)]
    report = DispatchReport()
    report.evidence_by_store["olist-orders"] = [
        Evidence("olist-orders", "commerce", ROW, "COUNT(*)=2346",
                 provenance={"sql": "SELECT COUNT(*) FROM orders"})]
    report.outcomes = [StoreOutcome("olist-orders", "commerce", OK, count=1)]
    llm = _Llm()
    synthesize("How many orders?", report,
               RoutingDecision(query_type="analytical", stores=routed, candidates=routed),
               llm)
    assert "SELECT COUNT(*) FROM orders" in _query_line(llm.contexts[0])
    assert "IN (" not in _query_line(llm.contexts[0])


def test_an_unaligned_report_falls_back_to_the_s19_collapse():
    """Without a bind there is no semi-join to state, and a long literal list must still
    be collapsed rather than dumped into the prompt (s19)."""
    report = _rescue_report()
    long_sql = ("SELECT COUNT(*) FROM order_items WHERE product_id IN ("
                + ", ".join(f"'{i:032d}'" for i in range(30)) + ")")
    report.evidence_by_store["olist-orders"] = [
        Evidence("olist-orders", "commerce", ROW, "COUNT(*)=213",
                 provenance={"sql": long_sql})]          # no bind -> not a key carry
    llm = _Llm()
    synthesize(QUESTION, report, _rescue_decision(), llm)
    line = _query_line(llm.contexts[0])
    assert "carried values" in line, line
    assert "'00000000000000000000000000000005'" not in line, line


def main():
    test_the_proof_is_the_semi_join_the_two_halves_computed()
    test_the_nested_filter_statement_is_not_also_shown_standalone()
    test_the_carry_rows_still_never_reach_the_prompt()
    print("  PASS  #526 a key-carry proof is the semi-join actually computed - no "
          "placeholder, no raw keys, and the nested half is not also shown standalone")
    test_a_plain_single_store_answer_is_untouched()
    test_an_unaligned_report_falls_back_to_the_s19_collapse()
    print("  PASS  #526 a plain query is untouched and an unbound long list still "
          "collapses (s19 intact)")
    print("\n#526 SEMI-JOIN PROOF SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
