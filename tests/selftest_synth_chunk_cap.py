"""#500 stage 1 - the synthesizer's chunk context is capped at the top K.

Measured (findings s15/s21): with the fact chunk IN context and ranked FIRST, the 8B
synthesizer still declines when noisy neighbours sit beside it - order irrelevant,
volume decisive. After #499 tripled the chunk pool, B-009 and B-011 both fail 3/3 at
synthesis with perfect retrieval (doc-MRR 1.00). The prompt carried up to the merge
cap (12) chunks: four times the volume the model tolerates.

The fix, third application of the s18/s19 rule (pass FINDINGS not WORKINGS): only the
top K chunk-kind evidence items reach the MODEL's context. `merged` stays whole, so
evidence, citations and footnotes keep every chunk - the reader loses nothing, and the
cap is DISCLOSED (LAW 8).

Safety properties pinned here:
- ROW evidence never consumes a cap slot and is never dropped (SQL rail byte-identical);
- citations/evidence keep every chunk when the cap fires;
- the condensed pass (#493) still sees the FULL chunk list, so the cap can never
  reduce rescue coverage;
- mechanics rows (#474/s19) are excluded BEFORE the cap counts;
- cap 0 restores the pre-#500 context exactly.

DEFAULT: OFF (s24). The gate measured cap 3 and a CONTROL at cap 0 over the same doc
pack: both 27/33, differing on exactly two items in opposite directions (the cap
converts B-007, breaks A-003). A one-for-one trade is not an improvement, and #500's
targets B-009/B-011 stayed red at BOTH settings with doc-MRR 1.00 - the volume
hypothesis is falsified, not merely unconfirmed. The mechanism is kept behind
DBSEARCH_SYNTH_CHUNK_CAP because stage 2 needs it to experiment against.

Run: PYTHONPATH=src python3 tests/selftest_synth_chunk_cap.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.core.copy import NO_EVIDENCE_ANSWER  # noqa: E402
from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402
from dbsearch.router.evidence import CHUNK, RECORD, ROW, Evidence  # noqa: E402
from dbsearch.router.executor import DispatchReport, OK, StoreOutcome  # noqa: E402
from dbsearch.router.synthesizer import synthesize  # noqa: E402

CHUNKS = [f"passage number {i} about the staff directory" for i in range(1, 8)]


class _Llm:
    """Records every context it is handed; answers plainly (never a refusal, so the
    condensed pass stays out of the way unless a test asks for it)."""

    def __init__(self, answer_text="The contact is in the directory."):
        self._answer = answer_text
        self.answer_calls = []
        self.extract_calls = []

    def answer(self, question, context):
        self.answer_calls.append(list(context))
        return {"answer": self._answer}

    def extract_relevant(self, question, chunk):
        self.extract_calls.append(chunk)
        return "NONE"


def _decision():
    routed = [RoutedStore("docs", "hr", 1.0)]
    return RoutingDecision(query_type="semantic", stores=routed, candidates=routed)


def _report(contents=tuple(CHUNKS), kind=CHUNK, provenance=None):
    report = DispatchReport()
    report.evidence_by_store["docs"] = [
        Evidence("docs", "hr", kind, c, provenance=dict(provenance or {}))
        for c in contents]
    report.outcomes.append(StoreOutcome("docs", "hr", OK, count=len(contents)))
    return report


def _evidence_lines(context):
    """The context lines that are EVIDENCE - the bracketed instruction lines
    ([coverage]/[query]/[style]) are instructions, not evidence volume."""
    return [c for c in context if not c.lstrip().startswith(("[coverage]", "[query]",
                                                             "[style]"))]


def test_only_the_top_three_chunks_reach_the_model():
    llm = _Llm()
    synthesize("who is the key account manager?", _report(), _decision(), llm, chunk_cap=3)
    lines = _evidence_lines(llm.answer_calls[0])
    assert len(lines) == 3, lines
    for i, expected in enumerate(CHUNKS[:3]):
        assert expected in lines[i], (i, lines)


def test_rows_do_not_consume_cap_slots_and_are_never_dropped():
    """A federated answer can carry ROW evidence beside chunks: the SQL rail's rows
    are compact facts, not the volume the model drowns in."""
    report = DispatchReport()
    report.evidence_by_store["docs"] = [
        Evidence("docs", "hr", CHUNK, c) for c in CHUNKS[:5]]
    report.evidence_by_store["sales"] = [
        Evidence("sales", "fin", ROW, f"region=emea, amount={n}") for n in (100, 60)]
    report.outcomes.append(StoreOutcome("docs", "hr", OK, count=5))
    report.outcomes.append(StoreOutcome("sales", "fin", OK, count=2))
    routed = [RoutedStore("docs", "hr", 1.0), RoutedStore("sales", "fin", 0.9)]
    decision = RoutingDecision(query_type="hybrid", stores=routed, candidates=routed)

    llm = _Llm()
    synthesize("revenue and the manager?", report, decision, llm, chunk_cap=3)
    lines = _evidence_lines(llm.answer_calls[0])
    chunk_lines = [ln for ln in lines if "passage number" in ln]
    row_lines = [ln for ln in lines if "region=emea" in ln]
    assert len(chunk_lines) == 3, chunk_lines
    assert len(row_lines) == 2, "every ROW must survive the chunk cap"


def test_the_reader_still_gets_every_chunk_in_evidence_and_citations():
    llm = _Llm()
    result = synthesize("q", _report(provenance={"doc": "staff.json"}), _decision(),
                        llm, chunk_cap=3)
    assert len(result.evidence) == 7, len(result.evidence)
    assert len(_evidence_lines(llm.answer_calls[0])) == 3


def test_the_cap_is_disclosed_when_it_fires():
    llm = _Llm()
    result = synthesize("q", _report(), _decision(), llm, chunk_cap=3)
    d = (result.disclosure or "").lower()
    assert "7 passages" in d and "3 most relevant" in d, result.disclosure
    assert "cited" in d, result.disclosure


def test_no_disclosure_when_the_cap_does_not_fire():
    llm = _Llm()
    result = synthesize("q", _report(contents=CHUNKS[:3]), _decision(), llm, chunk_cap=3)
    assert "passages" not in (result.disclosure or "").lower(), result.disclosure


def test_cap_zero_restores_the_pre_500_context():
    llm = _Llm()
    result = synthesize("q", _report(), _decision(), llm, chunk_cap=0)
    assert len(_evidence_lines(llm.answer_calls[0])) == 7
    assert "passages" not in (result.disclosure or "").lower(), result.disclosure


def test_the_env_knob_sets_the_cap():
    os.environ["DBSEARCH_SYNTH_CHUNK_CAP"] = "2"
    try:
        llm = _Llm()
        synthesize("q", _report(), _decision(), llm)
        assert len(_evidence_lines(llm.answer_calls[0])) == 2
    finally:
        del os.environ["DBSEARCH_SYNTH_CHUNK_CAP"]


def test_the_condensed_pass_still_sees_every_chunk():
    """The cap trims the PROMPT, never the rescue: #493's second pass extracts from
    the full merged list, so a fact in chunk 5 is still reachable on a decline."""
    class _Declines(_Llm):
        def answer(self, question, context):
            self.answer_calls.append(list(context))
            if len(_evidence_lines(context)) > 1:
                return {"answer": NO_EVIDENCE_ANSWER}
            return {"answer": "Manager, Key Account."}

        def extract_relevant(self, question, chunk):
            self.extract_calls.append(chunk)
            return "passage number 7" if chunk == CHUNKS[6] else "NONE"

    llm = _Declines()
    result = synthesize("q", _report(), _decision(), llm)
    assert len(llm.extract_calls) == 7, llm.extract_calls
    assert result.answer == "Manager, Key Account.", result.answer


def test_mechanics_rows_are_excluded_before_the_cap_counts():
    """s19: rescue carry-key rows are WORKINGS - excluded from the prompt entirely.
    They must not silently consume the chunk budget either."""
    report = DispatchReport()
    report.evidence_by_store["docs"] = (
        [Evidence("docs", "hr", CHUNK, c, provenance={"mechanics": True})
         for c in ("carry key A", "carry key B")]
        + [Evidence("docs", "hr", CHUNK, c) for c in CHUNKS[:4]])
    report.outcomes.append(StoreOutcome("docs", "hr", OK, count=6))
    llm = _Llm()
    synthesize("q", report, _decision(), llm, chunk_cap=3)
    lines = _evidence_lines(llm.answer_calls[0])
    assert len(lines) == 3, lines
    assert all("carry key" not in ln for ln in lines), lines
    assert all("passage number" in ln for ln in lines), lines


def test_the_sql_rail_context_is_byte_identical():
    """Rows only: no cap, no reordering, no cap sentence - #474's measured shape."""
    llm = _Llm()
    result = synthesize("total revenue?",
                        _report(contents=tuple(f"region=r{i}, amount={i}"
                                               for i in range(6)), kind=ROW),
                        _decision(), llm)
    assert len(_evidence_lines(llm.answer_calls[0])) == 6
    assert "passages" not in (result.disclosure or "").lower(), result.disclosure


def test_record_evidence_is_capped_too_the_sharepoint_rail():
    """Review caught the first cut counting CHUNK only. GraphSearchStore - the shipped
    SharePoint connector - emits RECORD, so a SharePoint-native tenant got NO cap at all
    and, worse, a mixed catalog biased the model's context toward the uncapped store
    while the disclosure quoted chunk-only numbers."""
    report = DispatchReport()
    report.evidence_by_store["graph"] = [
        Evidence("graph", "hr", RECORD, f"record {i} from sharepoint") for i in range(6)]
    report.outcomes.append(StoreOutcome("graph", "hr", OK, count=6))
    routed = [RoutedStore("graph", "hr", 1.0)]
    decision = RoutingDecision(query_type="semantic", stores=routed, candidates=routed)
    llm = _Llm()
    result = synthesize("q", report, decision, llm, chunk_cap=3)
    lines = _evidence_lines(llm.answer_calls[0])
    assert len(lines) == 3, lines
    d = (result.disclosure or "").lower()
    assert "6 passages" in d and "3 most relevant" in d, result.disclosure


def test_the_cap_note_is_dropped_when_the_condensed_pass_answers():
    """Two disclosures that contradict each other are worse than one missing: the
    condensed pass reads the FULL list, so 'written from the 3 most relevant' is untrue
    of the answer that shipped."""
    class _Declines(_Llm):
        def answer(self, question, context):
            self.answer_calls.append(list(context))
            if len(_evidence_lines(context)) > 1:
                return {"answer": NO_EVIDENCE_ANSWER}
            return {"answer": "Manager, Key Account."}

        def extract_relevant(self, question, chunk):
            return "passage number 7" if chunk == CHUNKS[6] else "NONE"

    result = synthesize("q", _report(), _decision(), _Declines(), chunk_cap=3)
    assert result.answer == "Manager, Key Account.", result.answer
    d = (result.disclosure or "").lower()
    assert "condensed" in d, result.disclosure
    assert "most relevant" not in d, f"contradictory cap note survived: {result.disclosure}"


def test_a_misconfigured_cap_keeps_the_measured_default():
    """Review's mutation battery found both of these unpinned, and one of them inverted
    the operator's intent: `max(0, -1)` collided with the '0 disables' sentinel, so an
    operator TIGHTENING the cap removed it instead."""
    for bad in ("-1", "ten", "1e3", "3.0", ""):
        os.environ["DBSEARCH_SYNTH_CHUNK_CAP"] = bad
        try:
            llm = _Llm()
            synthesize("q", _report(), _decision(), llm)
            assert len(_evidence_lines(llm.answer_calls[0])) == 7, \
                f"{bad!r} changed the policy: {llm.answer_calls[0]}"
        finally:
            del os.environ["DBSEARCH_SYNTH_CHUNK_CAP"]
    # an explicit bad kwarg is the same story (bool is an int in Python - it must not
    # silently cap at 1)
    llm = _Llm()
    synthesize("q", _report(), _decision(), llm, chunk_cap=True)
    assert len(_evidence_lines(llm.answer_calls[0])) == 7, llm.answer_calls[0]


def test_the_cap_is_OFF_by_default_measured_not_cautious():
    """s24: the gate ran the doc pack at cap 3 AND a control at cap 0. Both scored
    27/33, differing on exactly two items in OPPOSITE directions - the cap converts
    B-007 and breaks A-003. A one-for-one trade is not an improvement, and #500's two
    actual targets stayed red at both settings with doc-MRR 1.00, which falsifies the
    volume hypothesis rather than merely failing to confirm it. The mechanism stays
    available behind the env knob for stage-2 work; it is just not ON by default."""
    llm = _Llm()
    result = synthesize("q", _report(), _decision(), llm)
    assert len(_evidence_lines(llm.answer_calls[0])) == 7, llm.answer_calls[0]
    assert "passages" not in (result.disclosure or "").lower(), result.disclosure


def test_a_store_the_cap_silenced_is_named():
    """merge_evidence is round-robin by per-store rank, so with 4+ document stores the
    cap is reached before the 4th store's BEST passage is offered - while that store
    still reports OK and still shows citations, so the reader has every reason to think
    it was read."""
    report = DispatchReport()
    routed = []
    for i, sid in enumerate(("alpha", "beta", "gamma", "delta")):
        report.evidence_by_store[sid] = [
            Evidence(sid, "hr", CHUNK, f"{sid} passage {j}") for j in range(2)]
        report.outcomes.append(StoreOutcome(sid, "hr", OK, count=2))
        routed.append(RoutedStore(sid, "hr", 1.0 - i * 0.01))
    decision = RoutingDecision(query_type="semantic", stores=routed, candidates=routed)
    result = synthesize("q", report, decision, _Llm(), chunk_cap=3)
    assert "delta" in (result.disclosure or ""), result.disclosure
    assert "cited" in (result.disclosure or "").lower(), result.disclosure


def main():
    test_only_the_top_three_chunks_reach_the_model()
    test_a_misconfigured_cap_keeps_the_measured_default()
    test_the_cap_is_OFF_by_default_measured_not_cautious()
    test_a_store_the_cap_silenced_is_named()
    test_record_evidence_is_capped_too_the_sharepoint_rail()
    test_the_cap_note_is_dropped_when_the_condensed_pass_answers()
    test_rows_do_not_consume_cap_slots_and_are_never_dropped()
    test_the_reader_still_gets_every_chunk_in_evidence_and_citations()
    test_the_cap_is_disclosed_when_it_fires()
    test_no_disclosure_when_the_cap_does_not_fire()
    test_cap_zero_restores_the_pre_500_context()
    test_the_env_knob_sets_the_cap()
    test_the_condensed_pass_still_sees_every_chunk()
    test_mechanics_rows_are_excluded_before_the_cap_counts()
    test_the_sql_rail_context_is_byte_identical()
    print("  PASS  #500 chunk cap: top-3 to the model, rows untouched, full trail "
          "kept, disclosed, condensed pass unrestricted, cap 0 = pre-#500")
    print("\nSYNTH-CHUNK-CAP SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
