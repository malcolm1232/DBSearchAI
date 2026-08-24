"""#810 - a failure reason must not survive the compose that fixed it.

The defect (found in wave-2's #780 prod audit): composeUp's success branch set
status="connected" but never cleared n.reason, and the status dot's tooltip renders
status+reason - so a store that failed one compose and succeeded the next showed
"connected: build/probe failed..." until a full reload. n.reason is per-node client
state and the node object survives across composes; only the tooltip lied, because
every other consumer (.nreason line, the status bar) gates on status==="planned".

One clause (composeUp only): adoptApplied rebuilds nodes fresh via nodeFromEntry, so
no stale reason can exist there - clearing one would be an equivalent-mutant home
(the #799 lesson). testConn's connected-degraded reason is deliberate and untouched.
Control in the same scenario: a store that KEEPS failing keeps its reason - an
over-broad clear (e.g. wiping reasons before the response lands) goes red on it.

    PYTHONPATH=src python3 tests/selftest_810_stale_reason_dom.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402  the shared jsdom gate (#792)

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_stale_reason_dom_probe.mjs"

_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the stale-reason DOM check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the compose reason lifecycle ({scenario})")
    return _domgate.resolve(_dom[scenario])


def test_fixture_shows_the_failure_first():
    r = _report("recompose_clears_reason")
    if r is None:
        return
    assert r["composeCallsAfterBoot"] >= 1, "boot never composed - the failure leg never ran"
    d = r["afterFail"]["csv-1"]
    assert d and "Unable to locate credentials" in (d["title"] or ""), (
        "fixture drift: the failed compose left no reason in csv-1's dot tooltip, so the "
        f"claim below would be about a page that never showed the failure ({d})")


def test_a_fixed_store_drops_its_stale_reason():
    r = _report("recompose_clears_reason")
    if r is None:
        return
    d = r["afterFix"]["csv-1"]
    assert d and "connected" in d["cls"], (
        f"fixture drift: csv-1 did not reach connected on the second compose ({d})")
    assert d["title"] == "connected", (
        "the dot tooltip still carries the OLD failure after a compose that succeeded - "
        f"'{d['title']}' (#810: composeUp's success branch must clear n.reason)")


def test_a_still_failing_store_keeps_its_reason():
    r = _report("recompose_clears_reason")
    if r is None:
        return
    d = r["afterFix"]["csv-2"]
    assert d and "still broken" in (d["title"] or ""), (
        "a store that FAILED the second compose lost its reason - the #810 clear "
        f"over-reached past the success branch ({d})")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
