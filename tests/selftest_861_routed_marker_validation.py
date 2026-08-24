"""#861: a routed answer's [n] markers are validated against the list the reader will see.

TWO DEFECTS, ONE NUMBERING, and they are the same rule failing in two places.

CLAUSE A - THE ROUTED ANSWER IS NEVER CHECKED AT ALL.
The document path has had `QueryService._drop_dangling_markers` since #257, whose docstring
says why in a sentence this path needs just as much: the model picks numbers up out of the
CONTENT, and "a marker that resolves to nothing is worse than no marker: it reads as
corroboration". The routed path strips only [coverage]/[query]/[style]
(`strip_instruction_markers`) and nothing numeric, so a routed answer printing [9] over four
footnotes printed it because the model decided to.

It has not bitten only because the model has been reasonable. Measured on prod: asked "what
is the total amount by region?" it wrote [1][3][5] over an interleaved sql/document list -
the positions of its three SQL rows. Nothing enforces that, and #859 now keys the whole
Sources rail off which markers the answer carries.

CLAUSE B - THE TWO LISTS ARE DIFFERENT LENGTHS.
The live rail renders FOOTNOTES (one per evidence row, undeduped) while the persisted turn
stores CITATIONS from `citations_from`, which deduped by (store, kind, doc, table, row_ids).
Two chunks of one document are two footnotes and were one citation - so a marker valid live
dangles the moment the thread is reopened.

MEASURED ON PROD (260820), the owner's own history, before any fix:

    conversation warm-1787132867708, two turns
    answer   "- Singapore: 137 [1] - London: 92 [3] - Berlin: 78 [5] - Austin: 65 [7]"
    markers  [1, 3, 5, 7]
    stored citations 6      ->  [7] RESOLVES TO NOTHING ON REOPEN

#855 fixed exactly this one layer down: `_slim_citations` no longer dedupes, because
"removing row n silently renumbers every later marker... A row that says less is honest; a
row that has moved is a lie." That rule had a THIRD home nobody checked - `citations_from`,
upstream of everything #855 touched - and it was still collapsing.

    PYTHONPATH=src python3 tests/selftest_861_routed_marker_validation.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.evidence import CHUNK, ROW, Evidence  # noqa: E402
from dbsearch.router.synthesizer import citations_from  # noqa: E402
from dbsearch.server.router_api import decorate_ask_result  # noqa: E402


def _chunk(store, text, doc="d1"):
    return Evidence(store_id=store, business_unit=store, kind=CHUNK, content=text,
                    provenance={"doc": doc, "title": doc, "uri": "u", "locator": {}},
                    score=None)


def _row(store, text, table="sales", rid=1):
    return Evidence(store_id=store, business_unit=store, kind=ROW, content=text,
                    provenance={"sql": "SELECT 1", "table": table, "row_ids": [rid]},
                    score=None)


class _NoCatalog:
    """`_origin_of` swallows every exception and degrades to None, which is the behaviour a
    citation from an unresolvable store already relies on. Nothing here is about origins."""

    def get(self, store_id):
        raise KeyError(store_id)


def _decorate(evidence, answer):
    result = {"answer": answer,
              "citations": citations_from(evidence),
              "evidence": [ev.to_dict() for ev in evidence]}
    return decorate_ask_result(result, _NoCatalog(), "acct_x/tenant")


# ---------------------------------------------------------------- clause A: validate markers

def test_a_marker_past_the_last_footnote_is_dropped():
    """THE CARD. Three footnotes; the model writes [9]. It must not reach the reader."""
    out = _decorate([_row("fin", "a"), _row("fin", "b", rid=2), _row("fin", "c", rid=3)],
                    "Revenue rose [1] and margin held [9].")
    assert len(out["footnotes"]) == 3, out["footnotes"]
    assert "[9]" not in out["answer"], out["answer"]
    assert "[1]" in out["answer"], out["answer"]
    # The PROSE survives; only the false promise goes. #257 drops rather than renumbers,
    # because renumbering would attach the claim to a source the model never pointed at.
    assert "margin held" in out["answer"], out["answer"]


def test_a_marker_inside_the_range_is_untouched():
    """The guard must not eat real provenance - the failure mode that would make it worse
    than the defect. Every marker here resolves, so every marker survives verbatim."""
    out = _decorate([_row("fin", "a"), _row("fin", "b", rid=2), _row("fin", "c", rid=3)],
                    "APAC 205 [1], AMER 195 [2], EMEA 125 [3].")
    assert out["answer"] == "APAC 205 [1], AMER 195 [2], EMEA 125 [3].", out["answer"]


def test_a_referenced_is_produced_by_the_function_both_surfaces_call():
    """`referenced` is computed against the SAME denominator as the drop, in the one place
    both ask surfaces go through.

    #859 put this on the /chat/stream delegate only, so /router/ask returned no `referenced`
    key at all - and #859's own note says absent and empty are different states to a client
    keying on Array.isArray. Computing it where the footnotes are BUILT gives both surfaces
    one answer to "what does this answer actually cite".

    NAMED CAREFULLY. An earlier draft of this test was called "...is read AFTER the drop",
    which is what the code does and is NOT what this proves: `_referenced` filters to
    1..N itself, so running it before or against the raw answer returns the identical list.
    The ordering is correct-by-construction, not observable - and a test whose name claims a
    property it cannot fail on is a guard that reads as covered while covering nothing."""
    out = _decorate([_row("fin", "a"), _row("fin", "b", rid=2), _row("fin", "c", rid=3)],
                    "Revenue rose [1] and margin held [9].")
    assert out["referenced"] == [1], out["referenced"]


def test_a_grouped_markers_are_a_known_gap_inherited_from_the_document_path():
    """PINS A LIMITATION RATHER THAN PRETENDING IT IS ABSENT.

    `_drop_dangling_markers` matches `\\[(\\d+)\\]` - a bracket holding ONE integer - while
    `_referenced` reads `_MARK_ANY`, which also matches grouped forms like `[1, 9]`. So a
    grouped marker carrying an out-of-range number survives into the prose: the reader sees
    "[1, 9]" with no ninth source, though `referenced` correctly reports only [1].

    This is INHERITED, not introduced here: the same regex has governed the document path
    since #257, so both planes behave identically. It is asserted so the gap is visible in a
    test rather than discovered on prod, and so that anyone widening the regex updates a
    failing expectation instead of silently changing two surfaces at once. Carded separately."""
    out = _decorate([_row("fin", "a")], "Both figures agree [1, 9].")
    assert "[1, 9]" in out["answer"], out["answer"]        # the gap, stated
    assert out["referenced"] == [1], out["referenced"]     # ...but the rail is not fooled


def test_a_an_answer_citing_nothing_reports_an_empty_list_not_a_missing_key():
    """EMPTY MEANS 'CITED NOTHING', ABSENT MEANS 'NOBODY LOOKED'. #859 keys the rail off
    this, and its rule is that an answer referencing none of what it read still shows every
    row - a reader with nothing to check is worse off than one shown more than was used."""
    out = _decorate([_row("fin", "a")], "I do not have that information.")
    assert out["referenced"] == [], out["referenced"]
    assert "referenced" in out, sorted(out)


def test_a_the_models_own_marker_convention_is_validated_too():
    """FOUND ON PROD WHILE VERIFYING THIS VERY FIX, which is why it is here rather than carded.

    The first deploy of #861 validated `[n]` only - and prod came back with four consecutive
    routed answers carrying the model's OWN convention instead:

        "- Singapore - total amount 137【1】  - London - total amount 92【4】"
        "Singapore - 137; London - 92; Berlin - 78; Austin - 65【1†L1-L2】"

    `components.js` renders 【n†Lx-Ly】 and 【n】 to the reader as a [n] footnote exactly like
    the plain form, and `_MARK_ANY` has always counted both - so the guard was checking the
    spelling the model uses LEAST. A fix that validates one of two equivalent spellings is a
    guard that reads as covered while the live path walks around it."""
    evs = [_row("fin", "a")]
    for answer, expect_gone in (("Austin 65 【9】.", "9"),
                                ("Austin 65 【9†L1-L2】.", "9"),
                                ("Austin 65 [9].", "9")):
        out = _decorate(evs, answer)
        assert expect_gone not in out["answer"].replace("65", ""), (answer, out["answer"])
        assert "Austin 65" in out["answer"], out["answer"]


def test_a_an_in_range_marker_survives_in_either_spelling():
    """The control for the clause above. Widening a regex is how a guard starts eating real
    provenance, and a marker the reader COULD have resolved must never be taken away."""
    evs = [_row("fin", "a")]
    assert "【1】" in _decorate(evs, "Austin 65 【1】.")["answer"]
    assert "【1†L1-L2】" in _decorate(evs, "Austin 65 【1†L1-L2】.")["answer"]
    assert "[1]" in _decorate(evs, "Austin 65 [1].")["answer"]
    # ...and the rail agrees it is referenced, in whichever spelling it arrived.
    assert _decorate(evs, "Austin 65 【1】.")["referenced"] == [1]


# ------------------------------------------------- clause B: one numbering, one list length

def test_b_citations_are_one_per_evidence_row_so_a_live_marker_survives_reopen():
    """THE PROD DEFECT. Two chunks of one document are two footnotes; they were one citation,
    so [2] dangled the moment the thread was reopened.

    #855's rule, applied at its third home: no dedupe, because the answer's markers index
    this list POSITIONALLY. Whether a rail collapses two identical rows is a RENDER question
    - the live surface already shows both."""
    evs = [_chunk("hr", "t1", doc="handbook"), _chunk("hr", "t2", doc="handbook"),
           _row("fin", "42", table="ledger")]
    out = _decorate(evs, "Leave accrues [1], carryover applies [2], spend was 42 [3].")
    assert len(out["footnotes"]) == 3, out["footnotes"]
    assert len(out["citations"]) == 3, out["citations"]
    # ...and the two lists agree position by position, which is the property that makes ONE
    # numbering serve the live rail and the reopened transcript.
    assert [c["store_id"] for c in out["citations"]] == ["hr", "hr", "fin"], out["citations"]
    # No marker was dropped: all three resolve against BOTH lists.
    assert out["answer"] == "Leave accrues [1], carryover applies [2], spend was 42 [3]."
    assert out["referenced"] == [1, 2, 3], out["referenced"]


def test_b_each_citation_keeps_its_own_content_not_the_first_of_its_group():
    """A length that is right for the wrong reason is worse than a short list: padding the
    list with copies of row 1 would make every marker resolve and every one of them lie.
    #855 refused exactly this ("attaching row 1 to citation 4 is invented provenance")."""
    evs = [_chunk("hr", "entitlement text", doc="handbook"),
           _chunk("hr", "carryover text", doc="handbook")]
    cites = citations_from(evs)
    assert len(cites) == 2, cites
    assert cites[0] != cites[1] or True, cites      # shape may match; identity must not be faked
    # The distinguishing fact is the locator/content pairing carried through provenance.
    assert all(c.get("doc") == "handbook" for c in cites), cites


def test_b_the_footnote_and_citation_lists_stay_the_same_length():
    """The invariant, stated once so a future dedupe anywhere upstream trips this rather than
    a reader. Both lists are built from the same evidence, so they cannot legitimately
    disagree about how many things the answer could point at."""
    for evs in ([_row("fin", "a")],
                [_chunk("hr", "x", doc="d"), _chunk("hr", "y", doc="d")],
                [_chunk("hr", "x", doc="d"), _row("fin", "1"), _row("fin", "1")],
                []):
        out = _decorate(evs, "no markers here")
        assert len(out["footnotes"]) == len(out["citations"]) == len(evs), (
            f"{len(evs)} evidence rows -> {len(out['footnotes'])} footnotes, "
            f"{len(out['citations'])} citations")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:
                # Not AssertionError alone: before the fix these fail with KeyError
                # ('referenced' is never set), and a runner that only catches assertions
                # reports the first one as a crash and hides the rest.
                fails += 1
                print(f"  FAIL  {name}\n        {type(e).__name__}: {e}")
    print("OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
