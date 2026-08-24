"""#493 - a declined answer over chunk evidence gets ONE condensed second pass.

Measured (findings s15): the fact was IN the synthesizer's context in 7 of 8 failing
doc items, and the model answers correctly from the single fact-bearing chunk while
drowning at five chunks - order irrelevant, volume decisive (8B lost-in-context).

The fix: when the first synthesis comes back as a decline and the evidence is chunks,
each chunk is asked for VERBATIM extracts relevant to the question; extracts are
mechanically verified against their source chunk (a hallucinated extract is discarded -
the same verbatim trust gate as #479's resolver and #474's planner); the verified
extracts alone feed one second synthesis. A non-decline second answer wins; anything
else leaves the original decline.

Safety properties pinned here:
- fires ONLY on a decline - a delivered answer, even a wrong one, is never overridden;
- chunk-kind evidence only - the SQL rail is byte-identical;
- extracts must be verbatim - the condensed context cannot contain model inventions;
- all-NONE extracts leave the decline standing (the G traps stay declined).

Run: PYTHONPATH=src python3 tests/selftest_condensed_synthesis.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.core.copy import NO_EVIDENCE_ANSWER  # noqa: E402
from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402
from dbsearch.router.evidence import CHUNK, ROW, Evidence  # noqa: E402
from dbsearch.router.executor import DispatchReport, OK, StoreOutcome  # noqa: E402
from dbsearch.router.synthesizer import synthesize  # noqa: E402

CHUNK_A = ("Registration for the portal is done at the benefits site. "
           "Homepage tools: finding a panel clinic and Request for Letter of Guarantee.")
CHUNK_B = "Step 1: key in your NRIC number. Step 2: input the captcha numbers as shown."
CHUNK_C = "Member policies show the overview entitlement and current balance."


class _Llm:
    """First answer declines; over a SHORT context (the condensed pass) it answers.
    extract_relevant is scripted per chunk."""

    def __init__(self, extracts, condensed_answer="You can request a Letter of Guarantee."):
        self._extracts = extracts
        self._condensed = condensed_answer
        self.extract_calls = []
        self.answer_calls = []

    def answer(self, question, context):
        self.answer_calls.append(list(context))
        if len(context) >= 3:                       # the full, drowning context
            return {"answer": NO_EVIDENCE_ANSWER}
        return {"answer": self._condensed}

    def extract_relevant(self, question, chunk):
        self.extract_calls.append(chunk)
        return self._extracts.get(chunk[:20], "NONE")


def _report(kind=CHUNK, contents=(CHUNK_A, CHUNK_B, CHUNK_C)):
    report = DispatchReport()
    report.evidence_by_store["docs"] = [
        Evidence("docs", "hr", kind, c, provenance={"total_rows": len(contents)})
        for c in contents]
    report.outcomes.append(StoreOutcome("docs", "hr", OK, count=len(contents)))
    return report


def _decision():
    routed = [RoutedStore("docs", "hr", 1.0)]
    return RoutingDecision(query_type="semantic", stores=routed, candidates=routed)


def test_a_declined_chunk_answer_gets_a_condensed_second_pass():
    llm = _Llm({CHUNK_A[:20]: "Request for Letter of Guarantee"})
    result = synthesize("What can be requested from the homepage tools?",
                        _report(), _decision(), llm)
    assert result.answer == "You can request a Letter of Guarantee.", result.answer
    assert len(llm.answer_calls) == 2, llm.answer_calls
    condensed_ctx = llm.answer_calls[1]
    assert any("Letter of Guarantee" in c for c in condensed_ctx), condensed_ctx
    assert "condensed" in (result.disclosure or "").lower(), result.disclosure


def test_a_hallucinated_extract_is_discarded_and_the_decline_stands():
    llm = _Llm({CHUNK_A[:20]: "The guarantee is issued within 24 hours"})   # NOT verbatim
    result = synthesize("What can be requested?", _report(), _decision(), llm)
    assert result.answer == NO_EVIDENCE_ANSWER, result.answer
    assert len(llm.answer_calls) == 1, "no condensed pass without a VERIFIED extract"


def test_all_none_extracts_leave_the_decline_standing():
    llm = _Llm({})                              # every chunk -> NONE (the G-trap shape)
    result = synthesize("What is the Coupa tender budget?", _report(), _decision(), llm)
    assert result.answer == NO_EVIDENCE_ANSWER, result.answer
    assert len(llm.answer_calls) == 1


def test_a_delivered_answer_is_never_overridden():
    class _Confident(_Llm):
        def answer(self, question, context):
            self.answer_calls.append(list(context))
            return {"answer": "The tools offer clinic search."}
    llm = _Confident({CHUNK_A[:20]: "Request for Letter of Guarantee"})
    result = synthesize("q", _report(), _decision(), llm)
    assert result.answer == "The tools offer clinic search."
    assert llm.extract_calls == [], "extraction must not run on a delivered answer"


def test_row_evidence_never_triggers_the_pass_sql_rail_untouched():
    class _AlwaysDeclines(_Llm):
        def answer(self, question, context):
            self.answer_calls.append(list(context))
            return {"answer": NO_EVIDENCE_ANSWER}
    llm = _AlwaysDeclines({})
    result = synthesize("total revenue?",
                        _report(kind=ROW, contents=("region=emea, amount=100",
                                                    "region=apac, amount=60")),
                        _decision(), llm)
    assert llm.extract_calls == [], "ROW evidence is the SQL rail - byte-identical"
    assert result.answer == NO_EVIDENCE_ANSWER
    assert len(llm.answer_calls) == 1, "no second pass for the SQL rail"


def test_a_refusal_VARIANT_still_triggers_the_pass():
    """The live model drifts from the canonical sentence ('I don't have that
    information.') - the trigger must catch the refusal family, not one string."""
    class _Drifting(_Llm):
        def answer(self, question, context):
            self.answer_calls.append(list(context))
            if len(context) >= 3:
                return {"answer": "I don't have that information."}
            return {"answer": self._condensed}
    llm = _Drifting({CHUNK_A[:20]: "Request for Letter of Guarantee"})
    result = synthesize("What can be requested?", _report(), _decision(), llm)
    assert result.answer == "You can request a Letter of Guarantee.", result.answer


def test_an_echoed_question_prefix_is_stripped_from_the_answer():
    """B-013 measured live: the model sometimes prefixes its (correct) answer with the
    question verbatim - and sometimes the echo arrives with NO answer behind it (B-007).
    The prefix is stripped mechanically; an echo with a real answer behind it becomes
    that answer."""
    class _Echoing(_Llm):
        def answer(self, question, context):
            self.answer_calls.append(list(context))
            return {"answer": f"{question}\n\nAC Nielsen data is used."}
    llm = _Echoing({})
    result = synthesize("Whose tracking data is collected?", _report(), _decision(), llm)
    assert result.answer == "AC Nielsen data is used.", result.answer


def test_a_pure_echo_is_a_refusal_and_gets_the_condensed_pass():
    class _PureEcho(_Llm):
        def answer(self, question, context):
            self.answer_calls.append(list(context))
            if len(context) >= 3:
                return {"answer": question}                # echo, nothing behind it
            return {"answer": self._condensed}
    llm = _PureEcho({CHUNK_A[:20]: "Request for Letter of Guarantee"})
    result = synthesize("What can be requested?", _report(), _decision(), llm)
    assert result.answer == "You can request a Letter of Guarantee.", result.answer


def main():
    test_a_declined_chunk_answer_gets_a_condensed_second_pass()
    test_a_hallucinated_extract_is_discarded_and_the_decline_stands()
    test_all_none_extracts_leave_the_decline_standing()
    test_a_delivered_answer_is_never_overridden()
    test_row_evidence_never_triggers_the_pass_sql_rail_untouched()
    test_a_refusal_VARIANT_still_triggers_the_pass()
    test_an_echoed_question_prefix_is_stripped_from_the_answer()
    test_a_pure_echo_is_a_refusal_and_gets_the_condensed_pass()
    print("  PASS  #493 condensed pass: decline-only, chunk-only, verbatim-verified "
          "extracts, refusal variants caught, SQL rail untouched")
    print("\nCONDENSED-SYNTHESIS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
