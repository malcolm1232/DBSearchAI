"""#689 / ADR 0025 slice 3 - what a person actually SEES under a routed answer on /ask.

#689 was found by TYPING THE SAME QUESTION INTO TWO SURFACES and reading two different
answers. Slices 1 and 2 made the two agree on the answer; this file is about the other half
of the promise, which is that they agree on the EXPLANATION - and that /ask does not explain
one answer twice.

Driven through tests/ask_proofs_dom_probe.mjs, which mounts the real `mountAsk` in jsdom,
serves a real SSE body, and clicks real buttons. Asserting on the file would prove nothing
here: every claim below is about what is on screen.

  PYTHONPATH=src python3 tests/selftest_689_ask_proofs_dom.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import _domgate  # noqa: E402  the shared jsdom gate (#792)

PROBE = ROOT / "tests/ask_proofs_dom_probe.mjs"
ASK = ROOT / "src/dbsearch/server/static/js/surfaces/ask.js"
JSDOM = ROOT / "tests/node_modules/jsdom/lib/api.js"

_CACHE: dict = {}


def _report(scenario="routed"):
    if scenario in _CACHE:
        return _CACHE[scenario]
    if not _domgate.gate(f"the ask proofs DOM probe ({scenario})"):
        return None
    out = subprocess.run(["node", str(PROBE), str(JSDOM), str(ASK), scenario],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"probe failed:\n{out.stderr[-2000:]}")
    _CACHE[scenario] = json.loads(out.stdout)
    return _CACHE[scenario]


def test_a_routed_answer_shows_the_same_sources_rail_the_canvas_shows():
    r = _report()
    if r is None:
        return
    assert r["has_rail"], "a routed answer has no Sources rail at all"
    rows = r["source_rows"]
    assert len(rows) == 2, f"expected both planes in one rail, got {rows}"
    assert rows[0]["sys"] == "Azure SQL" and rows[0]["tag"] == "query", rows[0]
    assert "table regional_totals" in (rows[0]["loc"] or ""), rows[0]
    assert "✓ Verify data" in rows[0]["actions"], rows[0]
    assert rows[1]["sys"] == "Documents" and rows[1]["tag"] == "document", rows[1]
    assert "↗ Open source" in rows[1]["actions"], rows[1]


def test_one_answer_gets_ONE_provenance_surface():
    """The router's footnotes already cover BOTH planes - the caller's documents are a store
    in the ask scope - so rendering the citation pill beside the rail would put two provenance
    surfaces, with two numbering schemes, on one answer. That is #755's defect (two identical
    SOURCES headings on one screen) re-created on a new surface."""
    r = _report()
    if r is None:
        return
    assert not r["has_pill"], "the document pill rendered beside the routed rail"
    assert len(r["headings"]) == 1, f"more than one SOURCES heading on one answer: {r['headings']}"


def test_the_document_only_answer_is_untouched():
    """The flag-off path, and every deployment that has connected nothing. It must look
    exactly as it did before this card."""
    r = _report("docs_only")
    if r is None:
        return
    assert r["has_pill"], "the document answer lost its sources pill"
    assert not r["has_rail"], "a document-only answer grew a router rail"
    assert r["headings"] == [], r["headings"]
    assert r["disclosure"] is None, r["disclosure"]


def test_the_rail_arrives_closed_on_a_thread():
    """#629's discipline, which matters more here than on the canvas: the canvas shows one
    answer, a thread shows ten, and ten open SQL rails is a wall."""
    r = _report()
    if r is None:
        return
    assert r["rail_closed_by_default"], "the rail arrives open and a thread becomes a wall"
    assert "Sources (2)" in (r["rail_summary"] or ""), r["rail_summary"]


def test_a_marker_opens_the_rail_and_lands_on_its_own_source():
    """A marker is a promise that the source is reachable. A click that silently did nothing -
    because the container happened to be closed - breaks that promise in the one place the
    reader is checking it."""
    r = _report()
    if r is None:
        return
    assert r["dangling_markers"] == [], \
        f"a marker points at a source that is not in the DOM: {r['dangling_markers']}"
    assert r["opened_by_marker"], "clicking [1] left the rail closed"
    assert r["highlighted"] == ["fn1"], \
        f"clicking [1] highlighted {r['highlighted']} instead of its own source"


def test_verify_data_actually_reaches_the_source():
    """A button that renders and does nothing is worse than no button: it is a provenance
    claim the product cannot honour. Proven by SPENDING the token and painting the rows."""
    r = _report()
    if r is None:
        return
    assert r["rerun_body"] == {"store_id": "azure_sql-1", "sql": "SELECT region, SUM(amount)",
                               "token": "tok-1"}, \
        f"Verify data sent the wrong request: {r['rerun_body']}"
    assert "verified live" in (r["verified"] or ""), r["verified"]
    assert "apac" in (r["verified"] or ""), \
        f"the returned rows were not painted: {r['verified']!r}"


def test_the_rendered_answer_is_the_final_one_not_the_streamed_draft():
    """#257, made load-bearing by #689. The streamed tokens are a DRAFT: the marker sweep, the
    echo strip, the #493 condensed pass and the #474 rescue all rewrite AFTER the last token.
    The fixture streams an instruction marker the server strips, so a surface rendering its
    accumulator shows text the product already decided was wrong."""
    r = _report()
    if r is None:
        return
    assert "[coverage]" not in (r["answer"] or ""), \
        f"the streamed draft was rendered instead of done.answer: {r['answer']!r}"
    assert "25 days of leave" in (r["answer"] or ""), \
        f"the final answer never reached the bubble: {r['answer']!r}"


def test_ask_never_fetches_the_router_itself():
    """ADR 0025's FIRST REJECTED ALTERNATIVE, and the thing it says twice to get right. The
    document bridge already rides on every router ask, so a client-side `/router/ask` beside
    the stream would render two competing answers over overlapping evidence every turn. The
    delegation is server-side; this surface talks to `/chat/stream` and nothing else."""
    r = _report()
    if r is None:
        return
    assert r["streamed_calls"] == ["/chat/stream"], \
        f"the Ask surface called the router directly: {r['streamed_calls']}"


def test_the_disclosure_sits_with_the_answer():
    """#218/#799: what the router could not cover, in the user's own words, above the
    apparatus - never buried inside a collapsed rail nobody opens."""
    r = _report()
    if r is None:
        return
    assert "not used: bigquery-1" in (r["disclosure"] or ""), r["disclosure"]


def test_a_routed_answer_keeps_its_feedback_control():
    """The routed path returns early in `renderResult`, which is exactly how a surface quietly
    loses the controls that come after the branch."""
    r = _report()
    if r is None:
        return
    assert r["has_feedback"], "the routed answer lost its Helpful / Not helpful buttons"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}\n        {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
