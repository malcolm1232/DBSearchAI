"""#724 - the document block must not append itself to an answer it did not contribute to.

THE DEFECT, as the owner met it on prod. A revenue question routed correctly to Azure SQL and
came back right. Underneath it the canvas then printed:

    Documents · edition index · drew on 2 docs
    I do not have that information.
    SOURCES — where this answer came from
    [1] F&N directorship.pdf
    [2] hr-leave-policy.txt

- "huh? how is this related". Three separate things are wrong in those five lines:

  1. A decline is rendered as an ANSWER, beneath an answer that already succeeded.
  2. A Sources list is a PROVENANCE CLAIM, and there is no answer for it to be provenance of.
     Those two documents were retrieved and rejected; printing them numbered says the opposite.
  3. [1] and [2] already meant two Azure SQL rows further up the SAME screen. One marker, two
     meanings - a reader tracing a citation lands on the wrong evidence.

THE ROOT CAUSE is a distinction the server already draws and this surface did not read:
`citations` is the set the model was SHOWN ("from trimmed hits only"), and the canvas used it
to answer "did the documents contribute?". Those are different questions. The fix adds
`referenced` - the set the answer POINTS AT - and the suppression hangs off that. It is
deliberately structural (markers in the final answer) and not a match on the decline's
WORDING: #233 is the standing lesson that phrasing moves with the model.

WHAT EVERY ASSERTION HERE IS MADE AGAINST. Two kinds, and each test names which:

  UNIT   `_referenced` over an answer string - the server-side fact the surface consumes.
  DOM    the canvas MOUNTED in a real DOM (jsdom), the ask dock DRIVEN the way a person drives
         it (type, click), and the resulting elements read back. Not a string search over a
         file: whether the block renders depends on state that only exists after two fetches
         resolve in order, which no grep can see.

WHAT THIS DOES NOT PROVE, and the real-browser pass in the done-gate still owes: jsdom has no
layout and no CSS, so "not in the document" is provable here and "not visible" is not.

    PYTHONPATH=src python3 tests/selftest_724_doc_block.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import _domgate  # noqa: E402  the shared jsdom gate (#792)
from dbsearch.query.service import QueryService  # noqa: E402

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
JSDOM = _domgate.JSDOM
PROBE = ROOT / "tests/canvas_doc_block_dom_probe.mjs"

_ref = QueryService._referenced


# ---- UNIT: which citations does the answer actually point at? --------------------------------

def test_unit_a_decline_references_nothing():
    """The exact prod answer. Two documents were shown; the answer points at neither."""
    assert _ref("I do not have that information.", 2) == []


def test_unit_both_marker_spellings_count():
    """[n] and the model's own 【n†Lx-Ly】 both put a resolvable marker on screen (#555), so
    both are references. An earlier cut read only the first and would have called a fully
    cited answer 'no contribution'."""
    assert _ref("leave is sixteen weeks [1] and meals are 65 euros 【2†L4-L6】", 2) == [1, 2]
    assert _ref("laptops come from the Hamburg desk 【1】.", 1) == [1], "the bare 【n】 form"


def test_unit_grouped_markers_count_as_references():
    """FOUND BY INDEPENDENT REVIEW, and the failure mode was worse than the original bug.

    The prompt says "Cite passages by their [n] markers" and does not forbid grouping, so a
    model writes "[1, 2]". The first cut matched a lone digit run only, reported nothing
    referenced, and the canvas — which REPLACES the panel's innerHTML on that signal — deleted a
    genuinely cited answer and printed "none of them answered this" in its place, while the
    markers were still visible in the text it had just thrown away."""
    for spelling in ("[1, 2]", "[1,2]", "[1;2]", "[1 , 2]", "[1-2]", "[1–2]"):
        assert _ref(f"Leave is sixteen weeks {spelling}.", 2) == [1, 2], spelling


def test_unit_a_range_is_not_expanded():
    """"[1-3]" names 1 and 3. Filling in 2 would attach the claim to a source the model never
    pointed at — the same invention `_drop_dangling_markers` refuses to make when it DROPS a
    marker rather than renumbering it."""
    assert _ref("see [1-3]", 3) == [1, 3]


def test_unit_bracketed_prose_is_not_a_citation():
    """Only digits and separators inside the brackets. Otherwise "[see notes]" or "[a-1]"
    becomes evidence that the documents contributed."""
    for s in ("[see notes]", "[TBD]", "[a-1]", "[]", "[ ]"):
        assert _ref(f"text {s} more", 3) == [], s


def test_unit_out_of_range_markers_are_not_references():
    """The model lifts numbers out of the CONTENT - numbered headings become fake footnotes
    (#257). Counting one would manufacture the contribution this function measures."""
    assert _ref("see section [4] and [7]", 2) == []
    assert _ref("revenue rose [1], see clause [9]", 1) == [1]


def test_unit_duplicates_collapse_and_order_is_stable():
    assert _ref("[2] then [1] then [2] again", 2) == [1, 2]


def test_unit_no_answer_references_nothing():
    for empty in ("", None):
        assert _ref(empty, 3) == []


# ---- DOM: what the owner sees under the answer -----------------------------------------------

_cache: dict = {}


def _probe(scenario):
    """Mount the canvas, drive the ask dock, read the DOM back.

    Returns None only when node or jsdom is unavailable AND `DBSEARCH_ALLOW_DOM_SKIP=1` was set;
    the DOM assertions then skip, and `tests/_domgate.py` counts the skip into the runner's ledger.
    Without that opt-out, missing tooling now FAILS here rather than passing silently (#792) -
    these guards were green no-ops on every clean clone and in CI for their whole life.

    A CRASH IS NOT A SKIP, and conflating them is how this helper first shipped: the assert
    below fired, the cache had already been seeded with None, and every later test in the run
    read that None, printed "skipping" and reported ok - so a canvas that threw on mount
    produced a green line for each surviving test. The unavailable case and the broken case
    are still different values, and the broken one re-raises for every test that asks."""
    if scenario not in _cache:
        if not _domgate.gate(f"the canvas DOM check ({scenario})"):
            _cache[scenario] = None                    # permitted skip, already counted
        else:
            _cache[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(JSDOM), str(CANVAS), scenario],
                f"the canvas ({scenario})")
    return _domgate.resolve(_cache[scenario])


def _skip():
    """The DOM half of this test did not run. `_probe` has already printed and counted why."""
    return True


def test_dom_the_probe_drives_the_module_that_ships():
    """A probe against a stale copy proves nothing about the served surface."""
    assert CANVAS.exists()
    src = CANVAS.read_text()
    assert "export function mountCanvas" in src, "the probe imports a mount that is not there"


def test_dom_a_declining_document_half_leaves_no_answer_and_no_sources():
    """THE BUG. Router answered; documents did not. The decline paragraph and the numbered
    Sources list must both be gone."""
    r = _probe("declines")
    if r is None:
        return _skip()
    assert r["doc_panel_present"], \
        "the panel vanished entirely - a user who knows the answer is in their documents can " \
        "no longer tell a search that missed from a search that never ran"
    assert not r["doc_sources_heading"], \
        "a SOURCES heading is a provenance claim and there is no answer for it to belong to"
    assert r["doc_source_rows"] == [], \
        f"retrieved-and-rejected documents are still listed as evidence: {r['doc_source_rows']}"
    assert "I do not have that information" not in r["doc_panel_text"], \
        f"the decline is still rendered as an answer: {r['doc_panel_text']!r}"
    assert "F&N directorship" not in r["doc_panel_text"], \
        "an unrelated document is still named under a revenue answer"


def test_dom_the_decline_still_says_the_documents_were_searched():
    """Suppression must not become silence - the honest denominator survives."""
    r = _probe("declines")
    if r is None:
        return _skip()
    t = r["doc_panel_text"].lower()
    assert "searched your documents" in t, f"no trace the documents were consulted: {t!r}"
    assert "12" in t, "the denominator (docs this caller can access) is gone"
    assert "none answered" in t, f"the outcome is not stated: {t!r}"


def test_dom_the_nothing_found_line_sits_with_the_routing_notes():
    """Found by looking at prod rather than at the DOM. The line was honest and correctly
    suppressed, and it sat at the very BOTTOM - under three source cards, a scroll away from
    the "also matched ... not consulted" line saying the same kind of thing. Two halves of one
    thought, separated by the evidence for a different one."""
    r = _probe("declines")
    if r is None:
        return _skip()
    order = r["child_order"]
    qsp = next(i for i, c in enumerate(order) if "qsp" in c)
    trace = next(i for i, c in enumerate(order) if "tracefoot" in c)
    srcs = [i for i, c in enumerate(order) if c == "sources"]
    assert qsp < trace, f"the line is still below the trace: {order}"
    assert all(qsp < i for i in srcs), f"the line is still below the source cards: {order}"


def test_dom_a_document_answer_stays_below_the_evidence():
    """The move is for the suppressed line only. A real document ANSWER belongs after the
    evidence for the answer above it - hoisting it into the routing notes would put a second
    answer above the first one's sources."""
    r = _probe("answers")
    if r is None:
        return _skip()
    order = r["child_order"]
    qsp = next(i for i, c in enumerate(order) if "qsp" in c)
    trace = next(i for i, c in enumerate(order) if "tracefoot" in c)
    assert qsp > trace, f"a document answer was hoisted into the routing notes: {order}"


def test_dom_the_router_answer_is_left_alone():
    """The correct answer above must survive untouched - #256's retraction is for the case
    where the documents ANSWER, and firing it on mere retrieval replaced a true abstention
    with 'the answer below comes from your documents' pointing at a decline."""
    r = _probe("declines")
    if r is None:
        return _skip()
    assert "4.2M" in r["router_answer_text"], "the router's answer was overwritten"
    assert "comes from your documents" not in r["router_answer_text"], \
        "the router's answer was retracted in favour of documents that answered nothing"


def test_dom_numbering_does_not_collide_when_both_halves_answer():
    """[1] and [2] belong to the router. The document half continues at [3], so no number on
    this screen carries two meanings."""
    r = _probe("answers")
    if r is None:
        return _skip()
    nums = r["rail_numbers"]
    assert nums == sorted(set(nums), key=nums.index), f"a source number repeats: {nums}"
    assert len(nums) == len(set(nums)), f"two sources share a number: {nums}"
    assert "[3]" in nums and "[4]" in nums, \
        f"the document half restarted its numbering instead of continuing: {nums}"


def test_dom_every_marker_on_screen_resolves_to_a_source():
    """A marker that lands nowhere reads as corroboration the answer has not got (#257).

    #763: this ran on `answers` only - the one shape that never exercises the BLOCK path. A
    _blockify change that dropped every <sup> would have left `answer_blocks` reading
    [p, p, ul], `answer_list_items` still naming EMEA and AMER, and the injection guard still
    green, with the markers gone. The structured shapes cost nothing to add: the field is
    already in the probe output, and #751's own fixes this session rewrote all three of them."""
    for scenario in ("answers", "structured", "structured_edge", "trailing_newline"):
        r = _probe(scenario)
        if r is None:
            return _skip()
        assert r["dangling_markers"] == [], \
            f"{scenario}: markers with no source to land on: {r['dangling_markers']}"
        # ...and they must still BE there. "No dangling markers" is also true of no markers.
        assert "sup" in str(r["answer_blocks"]) or "<sup" in (r["answer_html"] or ""), \
            f"{scenario}: the citation markers vanished from the answer entirely, which " \
            f"satisfies the dangling check vacuously: {r['answer_html']!r}"


def test_dom_the_models_own_marker_is_normalised_in_the_document_half_too():
    """#555 fixed this for the router half only; the document half kept a hand-rolled [n]
    replace, so 【1†L4-L6】 reached the reader verbatim there."""
    r = _probe("answers")
    if r is None:
        return _skip()
    assert "【" not in (r["doc_panel_text"] or ""), \
        f"the raw model marker is still on screen: {r['doc_panel_text']!r}"
    assert r["nested_markers"] == 0, \
        "a marker was wrapped twice - the 【n】 pass emits [n], which the plain-[n] pass then " \
        "matched again, leaving one marker with two click handlers"


def test_dom_a_read_but_uncited_document_is_labelled_not_dressed_as_evidence():
    """Half and half: the answer points at [1] only. [2] was read and stays listed - honestly
    part of what the model was handed - but must not wear the same clothes as support."""
    r = _probe("partial")
    if r is None:
        return _skip()
    rows = r["doc_source_rows"]
    assert len(rows) == 2, f"a document that was read went missing: {rows}"
    cited = [d for d in rows if not d["unused"]]
    uncited = [d for d in rows if d["unused"]]
    assert len(cited) == 1 and len(uncited) == 1, f"cited and read are not distinguished: {rows}"
    assert "not cited" in uncited[0]["tag"], \
        f"the uncited row claims to be a source: {uncited[0]}"


def test_dom_a_silent_router_is_not_retracted_by_documents_that_also_declined():
    """THE GAP MUTATION TESTING FOUND. #256 rewrites the router's abstention into "the answer
    below comes from your documents" - and it fires ONLY when the router produced no evidence,
    which is a branch no other scenario here reaches. Swapping its gate back to the SHOWN set
    left all fourteen earlier tests green.

    Both halves came up empty. The abstention is the true statement on the page and must
    survive; retracting it points the reader at documents that answered nothing, one line
    above a panel that says exactly that."""
    r = _probe("silent_router")
    if r is None:
        return _skip()
    assert "comes from your documents" not in r["router_answer_text"], \
        f"the router's true abstention was retracted in favour of documents that declined: " \
        f"{r['router_answer_text']!r}"
    assert "No visible source holds" in r["router_answer_text"], \
        f"the abstention was overwritten: {r['router_answer_text']!r}"
    assert "none answered" in r["doc_panel_text"].lower(), \
        "the document half should still report that it searched and found nothing"


def test_dom_a_silent_router_IS_retracted_when_documents_really_answer():
    """The other half of the same gate - proving the fix narrowed the condition rather than
    disabling the behaviour. A guard that never fires would pass the test above too."""
    r = _probe("silent_router_docs_answer")
    if r is None:
        return _skip()
    assert "comes from your documents" in r["router_answer_text"], \
        f"the abstention is now FALSE - the documents answered - and it was left standing: " \
        f"{r['router_answer_text']!r}"


def test_dom_a_second_ask_does_not_inherit_the_first_ones_numbering():
    """FOUND BY INDEPENDENT REVIEW, reproduced in a real DOM before being fixed.

    #qresult is persistent and assigning innerHTML does not clear its dataset, so the footnote
    count — written ONLY on the router's success path — survived into the next question. Ask a
    question, then ask another whose router call errors, and the document half numbered its
    sources [3] and [4] with no [1] or [2] anywhere on screen: markers pointing at evidence that
    was never rendered, which is exactly the unresolvable-citation failure #257 exists to stop.

    Every other scenario here does one ask per process, so this class was unrepresentable — the
    same blind-spot shape as a rig that never had two users."""
    r = _probe("stale_offset")
    if r is None:
        return _skip()
    nums = [d["num"] for d in r["doc_source_rows"]]
    assert nums, f"the document half rendered no sources at all: {r['doc_panel_text']!r}"
    assert nums[0] == "[1]", \
        f"the second ask inherited the first ask's offset — sources start at {nums[0]}: {nums}"
    assert r["dangling_markers"] == [], \
        f"markers point at sources that are not on screen: {r['dangling_markers']}"


def test_dom_nothing_retrieved_still_removes_the_panel_entirely():
    """#255's rule, which this change must not regress: no retrieval at all → no stub."""
    r = _probe("empty")
    if r is None:
        return _skip()
    assert not r["doc_panel_present"], \
        "an empty '0 docs' stub is back on every SQL-only ask"




# ---- #747: WHAT the bridge is asked, once the router has split the question ------------------

def test_dom_a_router_answered_sub_question_is_never_put_to_the_documents():
    """#747, and the case that shows why #724 was not enough.

    #724 asks "did the documents contribute?" and suppresses the block when the answer is no.
    On a COMPOUND question that gate cannot fire: the documents contribute to one half, so
    `referenced` is non-empty, the block renders - and it brings along the decline for the OTHER
    half, which the router had already answered. Live, that read as "Freight cost for each region
    - I do not have that information in the provided context", one screen below the freight costs
    the product had just retrieved correctly.

    The fix is upstream of the gate: ask the documents only what is still open. Here the router
    covered "what did EMEA bill?" and left the Krakow half uncovered."""
    r = _probe("compound")
    if r is None:
        return _skip()
    asked = r["bridge_asked"]
    assert len(asked) == 1, f"the bridge was asked {len(asked)} times: {asked}"
    assert "krakow" in asked[0].lower(), \
        f"the bridge was not asked the uncovered sub-question: {asked[0]!r}"
    assert "emea" not in asked[0].lower() and "bill" not in asked[0].lower(), \
        f"a sub-question the ROUTER answered was put to the documents as well: {asked[0]!r}"


def test_dom_a_fully_covered_compound_ask_does_not_reach_the_documents_at_all():
    """The live shape. Both halves answered by the router, and the bridge was still handed the
    whole question - so it answered the documentary half a SECOND time from an uploaded copy
    (the duplicate #689 predicts) and declined the other one.

    Nothing is open, so there is nothing to ask. Asserted on the REQUEST rather than on the
    rendered panel, because a bridge that runs and is then hidden still costs the user a
    retrieval over documents they did not need searched - and a later render change would
    quietly bring the duplicate back."""
    r = _probe("compound_covered")
    if r is None:
        return _skip()
    assert r["bridge_asked"] == [], \
        f"the documents were searched for a question the router had fully answered: {r['bridge_asked']}"
    assert "qsp" not in r["child_order"], \
        f"an empty document panel is still on screen: {r['child_order']}"


def test_dom_a_simple_ask_still_reaches_the_documents_with_the_whole_question():
    """The other side of #747, and the one that matters most: the bridge is the only way an
    uploaded document is ever read on this surface, so narrowing what it is asked must not
    narrow WHEN it is asked. A non-compound question has no sub-queries to be covered by, and
    must go through exactly as it did before."""
    r = _probe("answers")
    if r is None:
        return _skip()
    assert len(r["bridge_asked"]) == 1, f"the bridge was not asked once: {r['bridge_asked']}"
    assert r["bridge_asked"][0] == "what was revenue for EMEA in Q2", \
        f"a simple ask no longer reaches the documents verbatim: {r['bridge_asked'][0]!r}"


if __name__ == "__main__":
    fails = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                fails.append((name, e))
                print(f"  FAIL {name}: {e}")
    print(f"\n{'FAILED' if fails else 'PASSED'} - "
          f"{sum(1 for n in globals() if n.startswith('test_')) - len(fails)} ok, {len(fails)} failed")
    sys.exit(1 if fails else 0)
