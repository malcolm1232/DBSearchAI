"""#937 - /ask must not tell a connected caller that nothing is indexed.

FOUND ON PROD 260823 by a real user whose only source is a public Google Drive
folder. The page said, on first paint and six seconds after a hard reload:

    "No documents have been indexed yet, so document questions will come back empty.
     Connect a source to get started."

and the Sources panel said, as a header sitting directly on top of the three sources it had
just returned:

    "No documents are indexed yet, so there was nothing to search."

Both sentences are read off `corpus`, which counts the UPLOADED-document index. A connector
store builds its own index (router/providers/connector.py: `index = InMemoryIndex(obj)`), so
those counters can never see a Drive folder's chunks no matter how long ingest runs. The user
reported the product as broken; it was answering their questions the whole time.

WHAT THIS FILE PINS, and why it takes five scenarios rather than one:

    connected : corpus empty + a composed source        -> both sentences GONE
    empty     : corpus empty + nothing composed         -> the boot sentence KEPT (control)
    unshared  : indexed:true + authorized_docs:0        -> isolates the authorized_docs clause
    norows    : nothing retrieved, nothing composed     -> isolates the `retrieved &&` clause
    dryrun    : nothing retrieved, a source COMPOSED    -> the round-2 case (see below)
    unknown   : connected_sources null (unmeasured)     -> isolates `!== 0` from `> 0`

`empty` is the control: deleting the copy passes the first test and fails it, and that is the
cheap wrong fix this pair exists to catch - the sentence is CORRECT for a genuinely new user
and is the only thing telling them what to do next.

The last three exist because the first cut had only `connected` and `empty`, and in BOTH of
them `indexed:false` and `authorized_docs:0` were true at once. Three separate mutations
survived that pair. A fixture that satisfies both halves of a condition together proves
neither half, so each of these makes exactly one clause the thing that decides.

Driven through tests/ask_corpus_contradiction_dom_probe.mjs, which mounts the real `mountAsk`
in jsdom, serves a real SSE body and presses the real pill. Asserting on the source files would
prove nothing: every claim here is about a sentence on screen and what it sits above.

  PYTHONPATH=src python3 tests/selftest_937_ask_corpus_contradiction_dom.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import _domgate  # noqa: E402  the shared jsdom gate (#792)

PROBE = ROOT / "tests/ask_corpus_contradiction_dom_probe.mjs"
ASK = ROOT / "src/dbsearch/server/static/js/surfaces/ask.js"
JSDOM = ROOT / "tests/node_modules/jsdom/lib/api.js"

_CACHE: dict = {}

# The two sentences, verbatim from the surfaces that render them. Matched on a distinctive
# FRAGMENT rather than the whole string so a copy edit does not silently disarm the guard.
BOOT_LIE = "No documents have been indexed yet"
PANEL_LIE = "there was nothing to search"


def _report(scenario):
    if scenario in _CACHE:
        return _CACHE[scenario]
    if not _domgate.gate(f"the ask corpus-contradiction DOM probe ({scenario})"):
        return None
    out = subprocess.run(["node", str(PROBE), str(JSDOM), str(ASK), scenario],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"probe failed:\n{out.stderr[-2000:]}")
    _CACHE[scenario] = json.loads(out.stdout)
    return _CACHE[scenario]


def test_a_connected_caller_is_not_told_their_corpus_is_empty():
    """The front door. This is the sentence the reporting user read and believed."""
    r = _report("connected")
    if r is None:
        return
    assert BOOT_LIE not in r["boot_banner"], (
        "/ask told a caller with a composed source that nothing is indexed: "
        f"{r['boot_banner']!r}")


def test_the_sources_panel_does_not_deny_the_sources_it_is_showing():
    """The contradiction, at its sharpest: the denial and the sources are in the same box."""
    r = _report("connected")
    if r is None:
        return
    assert r["panel_present"], "the Sources panel never opened - the probe proves nothing"
    assert r["source_rows"] > 0, (
        "the panel opened with no source rows, so there is no contradiction to catch and "
        f"this assertion cannot fail for the right reason: {r}")
    assert PANEL_LIE not in r["panel_note"], (
        "the Sources panel claims nothing was searched, directly above "
        f"{r['source_rows']} sources: {r['panel_note']!r}")


def test_the_panel_does_not_swap_one_false_sentence_for_another():
    """`authorized_docs` is 0 in the same block, so the obvious near-miss fix falls through to
    'None of the indexed documents are shared with you yet' - equally false above three rows,
    and equally a permissions accusation the caller cannot act on."""
    r = _report("connected")
    if r is None:
        return
    assert "shared with you yet" not in r["panel_note"], (
        f"one false sentence replaced by another: {r['panel_note']!r}")
    assert "No matching documents" not in r["panel_note"], (
        f"the panel reports no matches while listing {r['source_rows']} sources: "
        f"{r['panel_note']!r}")


def test_a_caller_who_has_connected_nothing_is_STILL_told_to_connect_something():
    """THE CONTROL. Without this, deleting the copy passes every test above.

    For a genuinely new user the sentence is true and it is the only thing on the page that
    tells them what to do next. #392 exists because this message was missing."""
    r = _report("empty")
    if r is None:
        return
    assert BOOT_LIE in r["boot_banner"], (
        "the empty-corpus guidance is gone for a caller who has connected nothing - "
        f"the fix deleted the message instead of qualifying it: {r['boot_banner']!r}")


def test_the_control_keeps_the_panel_sentence_too():
    """The panel half of the control: a denial must never sit above visible rows, even for a
    caller who has composed nothing. Both halves of the fix are conditional, not removed."""
    r = _report("empty")
    if r is None:
        return
    assert r["source_rows"] > 0, "the control scenario lost its sources - it can no longer fail"
    assert PANEL_LIE not in r["panel_note"], (
        f"denial above {r['source_rows']} visible sources: {r['panel_note']!r}")


def test_documents_exist_but_none_are_yours_is_still_false_above_your_sources():
    """ISOLATES THE `authorized_docs` CLAUSE.

    `indexed:true, authorized_docs:0` is the state the first cut of this fix fell through to.
    It is a real state - an operator's uploads exist and none are shared with this caller -
    and it arrives alongside three connector-store sources the caller CAN see. Saying "none of
    the indexed documents are shared with you" on top of them is the same lie in a different
    sentence, and without this scenario a guard reading `indexed` alone passed every test."""
    r = _report("unshared")
    if r is None:
        return
    assert r["source_rows"] > 0, "no sources rendered - this assertion cannot fail correctly"
    assert "shared with you yet" not in r["shown_note"], (
        f"a permissions accusation above {r['source_rows']} visible sources: "
        f"{r['shown_note']!r}")
    assert "No matching documents" not in r["shown_note"], r["shown_note"]


def test_with_nothing_retrieved_the_denial_is_correct_and_must_survive():
    """ISOLATES THE `retrieved &&` CLAUSE.

    No citations, so no pill and no panel - ask.js renders the note inline. Here the corpus
    counters are the only thing anyone has, nothing on screen contradicts them, and the
    sentence is TRUE. A fix that suppressed it unconditionally would pass every other test in
    this file while deleting the product's only honest empty-state."""
    r = _report("norows")
    if r is None:
        return
    assert not r["panel_present"], "expected no panel without citations - fixture drifted"
    assert r["shown_note"], "no provenance note rendered at all"
    assert "Connect a source" in r["shown_note"], (
        "the connect-a-source guidance vanished for a caller who has composed NOTHING and "
        f"retrieved nothing - the one case where it is entirely true: {r['shown_note']!r}")
    assert "0 document" not in r["shown_note"], (
        "the retrieval-only sentence leaked into the no-sources case and now reports "
        f"'0 documents' as if it had grounded something: {r['shown_note']!r}")


def test_an_unmeasured_workspace_is_silence_not_emptiness():
    """ISOLATES `!== 0` FROM `> 0`.

    `connected_sources: null` means the workspace store could not be read - #392's rule is
    that an unmeasured corpus is silence, never emptiness. A guard written as `> 0` treats
    null exactly like a measured zero and prints the accusation anyway, and both the
    `connected` and `empty` scenarios pass under it."""
    r = _report("unknown")
    if r is None:
        return
    assert BOOT_LIE not in r["boot_banner"], (
        "an unreadable workspace was rendered as 'you have connected nothing': "
        f"{r['boot_banner']!r}")


def test_a_connected_caller_whose_question_matched_nothing_is_not_told_to_connect():
    """ROUND 2, and it was found on PROD after this card's first deploy - not by the suite.

    The first fix keyed entirely on `retrieved`, so it did nothing when a question matched
    NOTHING. On the live site that produced two contradicting sentences one line apart:

        "That query ran against your data and matched no records. The source is there and
         readable - it simply holds nothing that fits."
        "No documents are indexed yet, so there was nothing to search. Connect a source..."

    The answer knows the source exists. The note underneath told them to go and connect it.
    The lesson this file now carries: a fix that names an asymmetry (retrieved vs not) owes a
    probe on BOTH sides of it, and only prod had the case."""
    r = _report("dryrun")
    if r is None:
        return
    assert BOOT_LIE not in r["boot_banner"], r["boot_banner"]
    assert "Connect a source" not in r["shown_note"], (
        "a caller with a composed source was told to connect one: " f"{r['shown_note']!r}")
    assert PANEL_LIE not in r["shown_note"], r["shown_note"]
    assert r["shown_note"], "the note vanished entirely - silence is not the fix here"


if __name__ == "__main__":
    fails = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                fails.append(name)
                print(f"FAIL {name}\n     {exc}")
    print(f"\n{'FAILED' if fails else 'PASSED'}: {len(fails)} failure(s)")
    sys.exit(1 if fails else 0)
