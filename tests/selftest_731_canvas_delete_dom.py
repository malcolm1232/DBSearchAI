"""#731 (client half) - a canvas delete survives the remount, driven in a real DOM.

The server test (selftest_731_store_delete.py) pins the endpoint; THIS file pins the only
part the owner ever saw: delete a node, navigate away and back, and the node must stay
gone. No existing probe ever unmounted and remounted the surface, which is exactly why the
resurrect shipped - the defect does not exist inside a single mount.

Five scenarios, each a claim the pre-#731 client fails:
 - panel_delete / menu_delete: BOTH affordances reach DELETE /router/stores/{id} (with
   keepalive - the delete-then-navigate race), and the node is absent after remount.
 - delete_all: the canvas stays EMPTY after remount - `stores: []` is authoritative, not
   an absence that falls back to localStorage (the latent half of #731).
 - refusal: a 500 puts the node BACK with a toast - never show a deletion the server
   refused.
 - undo: the toast's Undo re-inserts the held node and re-composes it.

    PYTHONPATH=src python3 tests/selftest_731_canvas_delete_dom.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402  the shared jsdom gate (#792)

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_delete_persists_dom_probe.mjs"

_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the delete-persists DOM check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the canvas delete surface ({scenario})")
    return _domgate.resolve(_dom[scenario])


def test_both_delete_paths_reach_the_server_and_survive_remount():
    for scenario in ("panel_delete", "menu_delete"):
        r = _report(scenario)
        if r is None:
            return
        assert r["deletes"] and r["deletes"][0]["id"] == "csv-1", (
            f"{scenario}: the delete never reached the server - the pre-#731 client-only "
            f"filter is back: {r['deletes']}")
        assert r["deletes"][0]["keepalive"] is True, (
            f"{scenario}: the DELETE was sent without keepalive - a delete-then-navigate "
            f"loses the server call")
        assert r["afterDelete"] == ["csv-2"]
        assert r["afterRemount"] == ["csv-2"], (
            f"{scenario}: THE DEFECT - the deleted node resurrected on remount: "
            f"{r['afterRemount']}")
    print("  PASS  both delete paths reach the server; the delete survives remount")


def test_delete_all_stays_empty_after_remount():
    r = _report("delete_all")
    if r is None:
        return
    assert len(r["deletes"]) == 2
    assert r["afterDelete"] == []
    assert r["afterRemount"] == [], (
        f"delete-ALL resurrected on remount ({r['afterRemount']}) - `stores: []` is being "
        f"read as 'no manifest' and something (localStorage, demo) refilled the canvas")
    print("  PASS  delete-all stays empty after remount - empty is a state")


def test_a_dev_rig_emptied_canvas_stays_empty():
    """The restoreCanvas clause ALONE: on the dev-rig fallback (no real login ->
    loadLiveDemo), a saved-but-EMPTY canvas renders EMPTY. The old `.length` guard read it
    as no-save and resurrected the demo manifest over a deliberately-emptied canvas."""
    r = _report("dev_empty_restore")
    if r is None:
        return
    assert r["afterDevRemount"] == [], (
        f"an emptied dev-rig canvas resurrected on reload: {r['afterDevRemount']}")
    print("  PASS  a deliberately-emptied dev-rig canvas stays empty")


def test_a_refused_delete_puts_the_node_back():
    r = _report("refusal")
    if r is None:
        return
    assert sorted(r["afterRefusedDelete"]) == ["csv-1", "csv-2"], (
        f"the server refused the delete and the canvas showed it anyway: "
        f"{r['afterRefusedDelete']}")
    assert r["toast"] and "csv-1" in r["toast"], (
        f"the refusal was silent - no toast named the store: {r['toast']!r}")
    print("  PASS  a refused delete puts the node back, with a toast")


def test_undo_restores_and_recomposes():
    r = _report("undo")
    if r is None:
        return
    assert sorted(r["afterUndo"]) == ["csv-1", "csv-2"], (
        f"Undo did not restore the node: {r['afterUndo']}")
    assert r["undoComposed"] is True, (
        "Undo re-inserted the node but never re-composed - the restore would not persist")
    print("  PASS  Undo restores the node and re-composes it")


if __name__ == "__main__":
    test_both_delete_paths_reach_the_server_and_survive_remount()
    test_delete_all_stays_empty_after_remount()
    test_a_dev_rig_emptied_canvas_stays_empty()
    test_a_refused_delete_puts_the_node_back()
    test_undo_restores_and_recomposes()
    print("\nCANVAS DELETE DOM SELF-TEST PASSED.")
