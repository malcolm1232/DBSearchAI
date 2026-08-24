"""Phase E E3 — synthesizer self-test (merge / citations / disclosure / synthesize).
Run: python3 tests/selftest_router_synth.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import ExtractiveLlm  # noqa: E402
from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402
from dbsearch.router.evidence import CHUNK, ROW, Evidence  # noqa: E402
from dbsearch.router.executor import (  # noqa: E402
    ERROR, OK, TIMEOUT, DispatchReport, StoreOutcome,
)
from dbsearch.router.synthesizer import (  # noqa: E402
    RouterResult, citations_from, disclosure_from, merge_evidence, synthesize,
)


def _chunk(store, text, doc="d1", score=None):
    return Evidence(store_id=store, business_unit=store, kind=CHUNK, content=text,
                    provenance={"doc": doc, "title": doc, "uri": "u", "locator": {}},
                    score=score)


def _row(store, text, table="sales"):
    return Evidence(store_id=store, business_unit=store, kind=ROW, content=text,
                    provenance={"sql": "SELECT 1", "table": table, "row_ids": [1]},
                    score=None)


def test_merge_interleaves_by_rank_not_score():
    # Store A scores are HUGE, store B scores tiny — rank interleave must still alternate
    # (ADR 0008: cross-store scores are not comparable).
    a = [_chunk("a", "a0", score=100.0), _chunk("a", "a1", score=99.0)]
    b = [_row("b", "b0"), _row("b", "b1")]
    merged = merge_evidence([a, b])
    assert [e.content for e in merged] == ["a0", "b0", "a1", "b1"], [e.content for e in merged]


def test_merge_is_subtractive_and_capped():
    a = [_chunk("a", "a%d" % i) for i in range(10)]
    b = [_row("b", "b%d" % i) for i in range(10)]
    merged = merge_evidence([a, b], cap=6)
    assert len(merged) == 6, len(merged)
    inputs = {e.content for e in a + b}
    assert all(e.content in inputs for e in merged), "gate #3: merge may only drop/reorder"


def test_citations_polymorphic_and_one_per_evidence_row():
    """REVERSED BY #861, and the reason belongs here rather than in a commit nobody reads.

    This asserted `len(cites) == 2` for three evidence rows - the two handbook chunks
    deduped - and it was true, and it was measuring the wrong thing. The answer's [n] markers
    index this list POSITIONALLY, and the live rail renders FOOTNOTES (one per evidence row,
    undeduped) while a reopened transcript renders these. Collapsing two rows into one meant
    a marker that resolved live pointed at nothing on reopen. Measured on prod: an answer
    carrying [1][3][5][7] was stored against 6 citations.

    Same rule #855 restored one layer down in `_slim_citations`: "A row that says less is
    honest; a row that has moved is a lie." The list is now length-preserving here too.

    The polymorphic half of the original claim is unchanged and still asserted below - a
    chunk cites its document, a row cites its table and query."""
    evs = [_chunk("hr", "t1", doc="handbook"), _chunk("hr", "t2", doc="handbook"),
           _row("fin", "42", table="ledger")]
    cites = citations_from(evs)
    assert len(cites) == 3, cites                      # one per evidence row, positional
    chunk_cites = [c for c in cites if c["kind"] == CHUNK]
    row_cite = next(c for c in cites if c["kind"] == ROW)
    assert len(chunk_cites) == 2, chunk_cites
    assert all(c["doc"] == "handbook" and c["store_id"] == "hr" for c in chunk_cites), chunk_cites
    assert row_cite["table"] == "ledger" and row_cite["sql"] == "SELECT 1", row_cite
    # ORDER IS THE CONTRACT: marker [3] must be the ledger row, not "some row that is a
    # ledger row somewhere in the list". Position is what the numbering means.
    assert [c["kind"] for c in cites] == [CHUNK, CHUNK, ROW], cites


def test_disclosure_names_dropped_stores_only():
    outcomes = [StoreOutcome("hr", "hr", OK, count=3),
                StoreOutcome("fin", "finance", TIMEOUT),
                StoreOutcome("legal", "legal", ERROR, error="boom")]
    d = disclosure_from(outcomes)
    assert "fin" in d and "legal" in d and "hr" not in d, d


def test_no_disclosure_when_full_coverage():
    assert disclosure_from([StoreOutcome("hr", "hr", OK, count=1)]) == ""


def test_truncated_result_is_disclosed_with_the_true_row_count():
    """#206: an answer built from 5 of 295 rows must SAY so. A cost cap is already disclosed
    ('Capped by query budget'); a row cap is the same promise — the reader cannot otherwise
    tell a complete answer from a 2% sample, and the prose will happily claim completeness
    ('here is the total revenue for EACH product SKU')."""
    d = disclosure_from([StoreOutcome("aw", "sales", OK, count=5, total=295)])
    assert "5" in d and "295" in d, d
    assert "aw" in d, d


def test_untruncated_result_discloses_nothing():
    # total == count (or unknown/0) is full coverage — must stay silent, or the disclosure
    # line becomes noise on every ask and gets ignored when it matters.
    assert disclosure_from([StoreOutcome("aw", "sales", OK, count=5, total=5)]) == ""
    assert disclosure_from([StoreOutcome("aw", "sales", OK, count=5)]) == ""
    assert disclosure_from([StoreOutcome("d", "d", OK, count=0, total=0)]) == ""


class SpyLlm(ExtractiveLlm):
    """Records the exact context handed to generation (gate-#3 evidence)."""
    def __init__(self):
        self.seen_context = None

    def answer(self, question, context_chunks):
        self.seen_context = list(context_chunks)
        return super().answer(question, context_chunks)


def _report_two_stores():
    hr = [_chunk("hr-wiki", "parental leave is 16 weeks", doc="handbook")]
    hr[0].business_unit = "hr"
    fin = [_row("fin-ledger", "Q3 revenue 4.2M", table="ledger")]
    fin[0].business_unit = "finance"
    report = DispatchReport(
        evidence_by_store={"hr-wiki": hr, "fin-ledger": fin},
        outcomes=[StoreOutcome("hr-wiki", "hr", OK, 1),
                  StoreOutcome("fin-ledger", "finance", OK, 1)],
    )
    decision = RoutingDecision(
        query_type="semantic",
        stores=[RoutedStore("hr-wiki", "hr", 0.9), RoutedStore("fin-ledger", "finance", 0.8)],
        reason="fanned out to hr-wiki (hr), fin-ledger (finance)",
    )
    return report, decision


def test_synthesize_returns_cited_answer_with_routing():
    report, decision = _report_two_stores()
    llm = SpyLlm()
    res = synthesize("q3 revenue and parental leave?", report, decision, llm)
    assert isinstance(res, RouterResult), res
    assert "16 weeks" in res.answer or "4.2M" in res.answer, res.answer
    assert len(res.citations) == 2, res.citations
    assert res.routing["reason"].startswith("fanned out"), res.routing
    assert res.disclosure == "", res.disclosure
    d = res.to_dict()
    assert set(d) == {"answer", "citations", "evidence", "routing", "outcomes",
                      "disclosure"}, d.keys()


def test_synthesize_context_is_labeled_and_only_merged_evidence():
    report, decision = _report_two_stores()
    llm = SpyLlm()
    synthesize("q", report, decision, llm)
    assert llm.seen_context is not None
    assert any(c.startswith("[hr-wiki · hr]") for c in llm.seen_context), llm.seen_context
    assert any(c.startswith("[fin-ledger · finance]") for c in llm.seen_context), llm.seen_context
    # The guard this test exists for (gate #3 / LAW 2): the only EVIDENCE the model sees is the
    # merged, permission-trimmed evidence - nothing untrimmed leaks in. #227/#231 added
    # INSTRUCTION lines (the [query] that produced the evidence, and #206's [coverage] sample
    # warning). Those are not evidence and carry no store content, so assert on the evidence
    # lines specifically rather than on a raw length, which would have made this test forbid any
    # instruction at all - including the #206 warning it was already living beside.
    # Instruction lines are addressed to the MODEL and carry no store content; evidence lines are
    # prefixed with their own "[store · bu]". Recognise instructions by their reserved prefixes so
    # adding one (#449's [style]) doesn't fail this test for a reason it was never about - what it
    # guards is that no UNTRIMMED EVIDENCE appears, not that the prompt never grows.
    INSTRUCTION_PREFIXES = ("[query]", "[coverage]", "[style]")
    ev_lines = [c for c in llm.seen_context if not c.startswith(INSTRUCTION_PREFIXES)]
    assert len(ev_lines) == 2, ("context must carry EXACTLY the merged evidence", ev_lines)
    for line in ev_lines:
        assert line.startswith("[hr-wiki · hr]") or line.startswith("[fin-ledger · finance]"), line


def test_a_giant_in_list_is_collapsed_in_the_prompt_proof():
    """Measured live (D-001, 260805, findings s18/s19): the rescue's measure SQL carries
    296 carried keys in its IN list - a 10,789-char [query] line. llama3.1:8b declines on
    it in EVERY variant (full, capped, alongside the fact row); collapse the list and the
    same model answers with the exact gold. The PROMPT gets the readable proof; the
    result's provenance keeps the full re-runnable SQL untouched."""
    seen = {}

    class _SpyLlm:
        def answer(self, question, context):
            seen["context"] = context
            return {"answer": "ok"}

    ids = ", ".join(f"'c{i:03d}aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'" for i in range(296))
    sql = f"SELECT SUM(price) FROM order_items WHERE customer_id IN ({ids})"
    ev = Evidence(store_id="s", business_unit="b", kind=ROW, content="SUM(price)=39180.04",
                  provenance={"sql": sql, "table": "order_items", "row_ids": [0], "total_rows": 1})
    report = DispatchReport(evidence_by_store={"s": [ev]},
                            outcomes=[StoreOutcome(store_id="s", business_unit="b", status="ok",
                                                   count=1, total=1)])
    decision = RoutingDecision(query_type="analytical", stores=[RoutedStore("s", "b", 0.9)],
                               method="prefilter", confidence=0.9, reason="")
    res = synthesize("total revenue for RJ customers", report, decision, _SpyLlm())

    qline = next(c for c in seen["context"] if c.startswith("[query]"))
    assert "c005" not in qline, "the raw id soup must not reach the prompt"
    assert "296" in qline and "IN (" in qline, qline
    assert "SUM(price)" in qline, qline
    assert len(qline) < 2000, len(qline)
    assert "c005" in res.evidence[0]["provenance"]["sql"], "the full proof must survive in provenance"


def test_mechanics_rows_stay_out_of_the_model_context():
    """Findings s19: with even TWO carry-key sample rows beside the fact row, llama3.1:8b
    declines; with the fact row and the proof alone, it answers. Rows marked
    provenance.mechanics are workings the machine already consumed - they stay visible in
    the result's evidence/citations for the reader, and stay OUT of the prompt."""
    seen = {}

    class _SpyLlm:
        def answer(self, question, context):
            seen["context"] = context
            return {"answer": "ok"}

    key = Evidence(store_id="crm", business_unit="sales", kind=ROW, content="customer_id=c1",
                   provenance={"sql": "SELECT customer_id FROM customers", "mechanics": True})
    fact = Evidence(store_id="orders", business_unit="sales", kind=ROW, content="SUM(price)=150.0",
                    provenance={"sql": "SELECT SUM(price) FROM order_items"})
    report = DispatchReport(
        evidence_by_store={"crm": [key], "orders": [fact]},
        outcomes=[StoreOutcome("crm", "sales", "ok", count=1, total=1),
                  StoreOutcome("orders", "sales", "ok", count=1, total=1)])
    decision = RoutingDecision(query_type="compound",
                               stores=[RoutedStore("crm", "sales", 1.0),
                                       RoutedStore("orders", "sales", 1.0)],
                               method="cross-store-rescue", confidence=1.0, reason="")
    res = synthesize("q", report, decision, _SpyLlm())

    ev_lines = [c for c in seen["context"] if not c.startswith(("[query]", "[coverage]", "[style]"))]
    assert ev_lines == ["[orders · sales] SUM(price)=150.0"], ev_lines
    assert any("customer_id=c1" in e["content"] for e in res.evidence), \
        "mechanics rows must stay visible to the READER"


def test_synthesize_no_evidence_answers_safely_without_llm_context():
    decision = RoutingDecision(query_type="semantic")
    res = synthesize("anything", DispatchReport(), decision, ExtractiveLlm())
    assert "couldn't find" in res.answer.lower() or "no accessible" in res.answer.lower(), res.answer
    assert res.citations == [] and res.evidence == [], res.to_dict()


def test_synthesize_discloses_dropped_store():
    report, decision = _report_two_stores()
    report.evidence_by_store.pop("fin-ledger")
    report.outcomes[1] = StoreOutcome("fin-ledger", "finance", TIMEOUT)
    res = synthesize("q", report, decision, ExtractiveLlm())
    assert "fin-ledger" in res.disclosure and "timeout" in res.disclosure, res.disclosure


def test_the_generating_query_reaches_the_model_not_just_the_rows():
    """#227/#231: the context was ev.content ALONE, so a number arrived stripped of its meaning.
    `Touring Bikes=220655` from `SELECT TOP 1 ... ORDER BY revenue DESC` made the model answer
    "I don't have enough information to say which is highest - I only see one category", to a
    query that had already ranked them. The database did the work and the answer was thrown away.
    The query must travel with the evidence."""
    seen = {}

    class _SpyLlm:
        def answer(self, question, context):
            seen["context"] = context
            return {"answer": "ok"}

    ev = Evidence(store_id="s", business_unit="b", kind=ROW,
                  content="Name=Touring Bikes, TotalRevenue=220655.38",
                  provenance={"sql": "SELECT TOP 1 Name, TotalRevenue FROM r ORDER BY TotalRevenue DESC",
                              "table": "r", "row_ids": [0], "total_rows": 1})
    report = DispatchReport(evidence_by_store={"s": [ev]},
                            outcomes=[StoreOutcome(store_id="s", business_unit="b", status="ok",
                                                   count=1, total=1)])
    decision = RoutingDecision(query_type="analytical", stores=[RoutedStore("s", "b", 0.9)],
                               method="prefilter", confidence=0.9, reason="")
    synthesize("which category has the highest revenue", report, decision, _SpyLlm())

    blob = " ".join(seen["context"])
    assert "ORDER BY TotalRevenue DESC" in blob, blob      # the QUERY reached the model
    assert "TOP 1" in blob
    assert "IS the maximum" in blob                        # ...and it was told what that MEANS



def test_no_evidence_says_WHY_and_never_leaks_an_invisible_store(): 
    """#218: every empty result used to produce the same permissions sentence - "I couldn't find
    anything you have access to about that." - whether nothing was composed, the store honestly
    DECLINED (#211), the store errored, or the query simply matched no rows. A user whose real
    problem was "you have not pressed Compose up yet" went hunting for an access problem. In a
    product built on honest, permission-faithful answers, a decline that misattributes its own
    reason is the same class of dishonesty as inventing a column, aimed at the user."""
    from dbsearch.router.executor import DECLINED, EMPTY, ERROR
    from dbsearch.router.synthesizer import (
        DECLINED_ANSWER, EMPTY_RESULT_ANSWER, FAILED_ANSWER, NOT_COMPOSED_ANSWER,
        NO_EVIDENCE_ANSWER, no_evidence_answer,
    )
    routed = RoutingDecision(query_type="analytical", stores=[RoutedStore("s", "b", 0.5)],
                             method="prefilter")

    nothing = RoutingDecision(query_type="analytical", method="fallback",
                              reason="no store is composed yet")
    assert no_evidence_answer(nothing, []) == NOT_COMPOSED_ANSWER

    assert no_evidence_answer(routed, [StoreOutcome("s", "b", DECLINED, 0, 0)]) == DECLINED_ANSWER
    assert no_evidence_answer(routed, [StoreOutcome("s", "b", ERROR, 0, 0)]) == FAILED_ANSWER
    assert no_evidence_answer(routed, [StoreOutcome("s", "b", EMPTY, 0, 0)]) == EMPTY_RESULT_ANSWER

    # LAW 2, and this is the line that must not move: when stores EXIST but none are visible to
    # this caller, the wording is EXACTLY what it always was. An invisible store has to stay
    # indistinguishable from a nonexistent one (design section 8, scenario G) - so the new
    # "nothing is composed" wording must NEVER appear here, or its absence/presence becomes an
    # existence oracle a user could probe.
    hidden = RoutingDecision(query_type="analytical", method="fallback",
                             reason="no accessible store for this user")
    assert no_evidence_answer(hidden, []) == NO_EVIDENCE_ANSWER
    assert "composed" not in no_evidence_answer(hidden, []).lower()


def main():
    test_no_evidence_says_WHY_and_never_leaks_an_invisible_store()
    test_the_generating_query_reaches_the_model_not_just_the_rows()
    print("Phase E E3 synth self-test:")
    test_merge_interleaves_by_rank_not_score()
    test_merge_is_subtractive_and_capped()
    test_citations_polymorphic_and_one_per_evidence_row()
    test_disclosure_names_dropped_stores_only()
    test_no_disclosure_when_full_coverage()
    test_truncated_result_is_disclosed_with_the_true_row_count()
    test_untruncated_result_discloses_nothing()
    print("  PASS  rank interleave / subtractive+cap / polymorphic citations / disclosure "
          "(incl. #206: a truncated result discloses 'showing k of N')")
    test_synthesize_returns_cited_answer_with_routing()
    test_synthesize_context_is_labeled_and_only_merged_evidence()
    test_a_giant_in_list_is_collapsed_in_the_prompt_proof()
    test_mechanics_rows_stay_out_of_the_model_context()
    test_synthesize_no_evidence_answers_safely_without_llm_context()
    test_synthesize_discloses_dropped_store()
    print("  PASS  synthesize: cited answer / labeled context / safe empty / disclosure")
    print("\nE3 SYNTH SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
