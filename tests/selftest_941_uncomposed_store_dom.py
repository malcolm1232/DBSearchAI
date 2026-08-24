"""#941 - a saved-but-uncomposed source must not present as connected, and Test connection
must compose.

FOUND ON PROD 260823, reported by the owner minutes after they re-added their Drive folder:
"still no data, but idk if its ingesting or not". It was not ingesting and never would have
been. `GET /router/manifest` held the store with the right link and ACL; `GET /router/catalog`
returned `{"business_units": []}`. Adding a source writes the row (#818); composing is a
separate button; nothing joined the two.

THE CONFLATION, which is the whole card: `testConn` and `composeUp` both write
`node.status = "connected"`. One means "the endpoint answered a probe", the other means "this
store is in the catalog and holds data". The probe is the one that runs first, so pressing
Test connection OVERWRITES the only honest signal on the page with a false one:

    before Test connection   dot=draft      "0 connected"   "Not connected yet"
    after  Test connection   dot=connected  "1 connected"   "Connection healthy - a record
                                                             round-tripped. probe ok,
                                                             content is retrievable"

...about a store holding nothing, with no compose on the wire.

SIX SCENARIOS, and the shape is deliberate - `composed` and `derived` are the controls that
fail if the fix simply stops trusting anything, and `tested` alone reproduces prod:

    uncomposed  compose skips it, no probe pressed               -> draft everywhere
    tested      compose skips it, THEN Test connection           -> must STAY draft
    composed    compose returns it                               -> connected, unchanged (CONTROL)
    autocompose Test connection on a healthy store               -> a compose must follow
    derived     an uploads node is present                       -> never accused (CONTROL)
    probefail   the probe refuses                                -> no compose may follow

Driven through tests/canvas_uncomposed_store_dom_probe.mjs, which mounts the real
`mountCanvas` in jsdom and clicks the real button. The health probe is stubbed HEALTHY in
every scenario on purpose: the Drive folder genuinely was reachable, and a fix that keyed on
the probe failing would pass a test and fix nothing.

  PYTHONPATH=src python3 tests/selftest_941_uncomposed_store_dom.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import _domgate  # noqa: E402  the shared jsdom gate (#792)

PROBE = ROOT / "tests/canvas_uncomposed_store_dom_probe.mjs"
CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
JSDOM = ROOT / "tests/node_modules/jsdom/lib/api.js"

_CACHE: dict = {}


def _report(scenario):
    if scenario in _CACHE:
        return _CACHE[scenario]
    if not _domgate.gate(f"the canvas uncomposed-store DOM probe ({scenario})"):
        return None
    out = subprocess.run(["node", str(PROBE), str(JSDOM), str(CANVAS), scenario],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"probe failed:\n{out.stderr[-2000:]}")
    _CACHE[scenario] = json.loads(out.stdout)
    return _CACHE[scenario]


def test_an_uncomposed_store_is_not_dressed_as_connected():
    """The resting state: in the row, absent from the catalog."""
    r = _report("uncomposed")
    if r is None:
        return
    assert r["node_present"], "no node rendered - the probe proves nothing"
    assert "connected" not in (r["dot_class"] or ""), (
        f"an uncomposed store shows the connected dot: {r['dot_class']!r}")
    assert "0 connected" in r["statusbar"], (
        f"the status bar counts an uncomposed store as connected: {r['statusbar']!r}")


def test_pressing_test_connection_does_not_turn_an_empty_store_green():
    """THE PROD SEQUENCE. This is the one that was live, and the one a fix must kill.

    The probe succeeds because the folder IS reachable. That fact says nothing about whether
    the store is composed, and the surface must stop treating it as if it did."""
    r = _report("tested")
    if r is None:
        return
    after = r["after"]
    assert after, "the probe did not record post-click state"
    assert "connected" not in (after["dot_class"] or ""), (
        "Test connection turned an uncomposed store green - the exact prod defect: "
        f"{after['dot_class']!r}")
    assert "1 connected" not in after["statusbar"], (
        f"the status bar counts it as connected after a mere probe: {after['statusbar']!r}")
    # ASSERT THE SENTENCE THE OLD CODE WOULD PRINT, not a string that happens to be absent.
    # The first version of this checked only for "content is retrievable" - and the mutation
    # that restores the old branch prints "Connected · live probe ok" instead, which contains
    # no such words and sailed through. Both are false above an empty store; both are pinned,
    # and the positive assertion below is what actually discriminates.
    assert "live probe ok" not in after["probe_line"], (
        "the panel calls an uncomposed store connected: " f"{after['probe_line']!r}")
    assert "content is retrievable" not in after["probe_line"], (
        "the panel asserts content is retrievable from a store that holds none: "
        f"{after['probe_line']!r}")
    assert "not composed" in after["probe_line"], (
        "the panel does not say the one thing that is true: " f"{after['probe_line']!r}")


def test_the_user_is_told_what_to_do_about_it():
    """Honesty that gives no next step is a different failure. The word the product uses for
    the action must appear where the user is looking."""
    r = _report("tested")
    if r is None:
        return
    after = r["after"]
    seen = " ".join([after["node_text"] or "", after["probe_line"] or "",
                     after["statusbar"] or "", after["dot_title"] or ""])
    assert "Compose" in seen, (
        f"nothing on screen names the action that would fix it: {seen!r}")


def test_test_connection_composes_so_the_button_need_not_be_found():
    """The other half. Honesty alone still leaves the user hunting for a button whose name
    means nothing to them; a healthy probe is the moment their intent is unambiguous."""
    r = _report("autocompose")
    if r is None:
        return
    assert r["test_button_present"], "no Test connection button - fixture drifted"
    assert r["composes_after_test"] >= 1, (
        "Test connection succeeded and sent no compose, so the store stays inert: "
        f"wire={r['wire_after_test']}")


def test_a_composed_store_still_reads_connected():
    """THE CONTROL, and the one that fails if the fix just stops believing anything.

    Compose RETURNS this store. Every signal must be exactly what it was before this card -
    green dot, counted, freshness on the card."""
    r = _report("composed")
    if r is None:
        return
    assert "connected" in (r["dot_class"] or ""), (
        f"a genuinely composed store lost its connected dot: {r['dot_class']!r}")
    assert "1 connected" in r["statusbar"], (
        f"a genuinely composed store is no longer counted: {r['statusbar']!r}")
    assert "ingested@" in r["node_text"], (
        f"the freshness the compose returned is gone from the card: {r['node_text']!r}")


def test_the_uploads_node_is_never_accused_of_being_a_draft():
    """ISOLATES THE `!node.derived` CLAUSE.

    "Your documents" (#917) is derived from /admin/documents and is never in a manifest, so it
    is never in a compose response either. Without that clause the honesty fix would libel the
    one node that is genuinely working, on every canvas that has an upload."""
    r = _report("derived")
    if r is None:
        return
    up = r["upload_node"]
    assert up, "no uploads node rendered - the fixture cannot isolate this clause"
    assert "connected" in (up["dot_class"] or ""), (
        f"the derived uploads node was demoted to draft: {up['dot_class']!r}")
    assert "Compose up" not in (up["text"] or ""), (
        f"the uploads node is told to compose something that has no manifest entry: {up['text']!r}")


def test_a_refused_probe_does_not_trigger_a_compose():
    """ISOLATES THE `v.status !== "failed"` CLAUSE.

    Auto-compose is keyed on the probe SUCCEEDING. Composing a store the probe just refused
    submits a crawl already known to fail, and buries the remediation the user needs under a
    compose error. Without this scenario, `composeUp()` called unconditionally passes every
    other test in this file."""
    r = _report("probefail")
    if r is None:
        return
    assert r["composes_after_test"] == 0, (
        "a refused probe still triggered a compose: " f"wire={r['wire_after_test']}")


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
