"""#803 - the live-demo reset button must never reach a signed-in user's workspace.

The defect: #reset ("Live demo") -> loadLiveDemo({fresh:true}) removes the localStorage
save, loads /router/demo and composeUp()s it - and _persisting_compose OVERWRITES the
signed-in user's stored user_manifests row with the DEMO manifest, whose demo-group ACLs
their real identity cannot even see (#293). applyModeChrome hid the button in DEMO mode
only, so a signed-in owner saw it in the toolbar. One click, durable workspace loss.

Two clauses, one scenario each - either clause alone going missing turns exactly its own
scenario red (never a fixture both halves rescue):
 - live_reset_hidden: the CHROME clause (applyModeChrome hides #reset whenever a real
   login is configured). Asserted on the inline style the code sets; the guard clause
   cannot rescue this - visibility is read directly, not through effects.
 - live_reset_click: the GUARD clause (loadLiveDemo refuses for isLiveUser()). jsdom
   dispatches clicks on display:none elements, so the chrome clause cannot rescue this.
Controls (green before AND after #803 - they fail an over-broad fix):
 - dev_reset_works: no real login (dev/self-host) keeps the #199 escape hatch.
 - demo_reset_hidden: demo mode keeps hiding the button (#279 B).

    PYTHONPATH=src python3 tests/selftest_803_canvas_reset_guard_dom.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402  the shared jsdom gate (#792)

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_reset_guard_dom_probe.mjs"

_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the reset-guard DOM check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the canvas reset guard ({scenario})")
    return _domgate.resolve(_dom[scenario])


def test_live_user_never_sees_the_reset_button():
    r = _report("live_reset_hidden")
    if r is None:
        return
    assert r["nodesOnMount"] == ["csv-1"], (
        "fixture drift: the signed-in mount no longer shows the stored workspace, so the "
        f"visibility claim below is about the wrong page state ({r['nodesOnMount']})")
    assert r["resetDisplay"] == "none", (
        "a signed-in user can SEE the 'Live demo' button - one click composes the demo "
        "manifest over their stored workspace row (the #803 chrome clause)")


def test_a_reset_click_cannot_reach_a_live_users_workspace():
    r = _report("live_reset_click")
    if r is None:
        return
    assert r["nodesBefore"] == ["csv-1"], (
        f"fixture drift: the signed-in mount did not show the stored workspace ({r['nodesBefore']})")
    assert r["demoFetched"] == 0, (
        "clicking reset as a signed-in user fetched /router/demo - loadLiveDemo ran, the "
        "#803 guard clause is gone")
    assert all(ids == ["csv-1"] for ids in r["composesAfter"]), (
        "a compose carried something other than the user's own stores after the reset "
        f"click - the demo manifest is headed for their stored row ({r['composesAfter']})")
    assert r["nodesAfter"] == ["csv-1"], (
        f"the canvas changed under a reset click for a signed-in user ({r['nodesAfter']})")
    assert r["localSaveIds"] == ["csv-1"], (
        "the localStorage save no longer holds the user's own nodes after the reset click "
        f"({r['localSaveIds']})")


def test_dev_rig_keeps_the_escape_hatch():
    r = _report("dev_reset_works")
    if r is None:
        return
    assert r["resetDisplay"] == "", (
        "the reset button is hidden on a no-login dev rig - the fix over-hid the #199 "
        "escape hatch")
    assert r["nodesAfter"] == ["demo-1"] and r["demoFetched"] >= 1, (
        "clicking reset on a dev rig no longer loads the demo manifest "
        f"(nodes {r['nodesAfter']}, demo fetches {r['demoFetched']})")


def test_demo_mode_still_hides_the_button():
    r = _report("demo_reset_hidden")
    if r is None:
        return
    assert r["resetDisplay"] == "none", (
        "demo mode shows the reset button again - the #279 (B) chrome regressed")


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
