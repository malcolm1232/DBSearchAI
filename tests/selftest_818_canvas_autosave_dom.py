"""#818 (client half) - an added node survives a reload, driven in a real DOM.

The server test (selftest_818_draft_save.py) pins PUT /router/manifest; THIS file pins
what the owner actually hit: add a node from the rail, reload, and the node must still be
there. The canvas mirrors every mutation into the server row - a debounced, dirty-checked,
keepalive PUT - and Cmd/Ctrl+S flushes it immediately with feedback.

Four scenarios, each a clause only IT can catch:
 - draft_autosave_survives: the autosave clause. The added draft rides a PUT (after the
   debounce, not synchronously) and the REMOUNT - hydrating purely from the server row,
   which is what destroyed drafts before #818 - still shows it.
 - cmd_s_flush: the flush clause. Cmd+S PUTs NOW (not after the debounce), suppresses the
   browser save dialog, and confirms with a toast.
 - clean_no_put: the dirty-check clause. Mounting and touching nothing must produce ZERO
   PUTs - hydration never re-saves the row it just read.
 - dev_no_put: the gate clause. A no-login dev rig never PUTs - localStorage remains its
   only store; the server row is a signed-in concept (#368).

    PYTHONPATH=src python3 tests/selftest_818_canvas_autosave_dom.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402  the shared jsdom gate (#792)

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_draft_autosave_dom_probe.mjs"

_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the draft-autosave DOM check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the canvas draft autosave ({scenario})")
    return _dom[scenario] if _dom[scenario] is None else _domgate.resolve(_dom[scenario])


def test_an_added_node_survives_the_remount():
    r = _report("draft_autosave_survives")
    if r is None:
        return
    assert r["putsRightAfterAdd"] == 0, (
        "the PUT fired synchronously on add - the debounce is gone, every keystroke-level "
        "mutation would hit the server")
    assert r["putIds"] and r["putIds"][-1] == ["csv-1", "azure_sql-1"], (
        f"no PUT carried the added draft - the #818 autosave clause is gone: {r['putIds']}")
    assert r["nodesAfterRemount"] == ["csv-1", "azure_sql-1"], (
        "the added node did not survive the remount - the pre-#818 loss, the exact thing "
        f"the owner hit on prod: {r['nodesAfterRemount']}")


def test_cmd_s_flushes_now_with_feedback():
    r = _report("cmd_s_flush")
    if r is None:
        return
    assert r["defaultPrevented"], (
        "Cmd+S fell through to the browser's save-page dialog")
    assert r["putsAfterCmdS"] == 1 and r["putIds"][-1] == ["csv-1", "azure_sql-1"], (
        f"Cmd+S did not flush the pending save immediately: {r['putIds']}")
    assert r["toast"] == "Workspace saved", (
        f"no confirmation toast - the flush must be visible: {r['toast']!r}")


def test_hydration_never_resaves_the_row_it_read():
    r = _report("clean_no_put")
    if r is None:
        return
    assert r["nodes"] == ["csv-1"], f"fixture drift: {r['nodes']}"
    assert r["puts"] == 0, (
        f"an untouched mount PUT the row back - the dirty-check clause is gone ({r['puts']} "
        "PUTs); every page load would rewrite the row it just read")


def test_server_layout_outranks_the_local_cache():
    r = _report("layout_hydrates_from_server")
    if r is None:
        return
    assert (r["left"], r["top"]) == ("1234px", "777px"), (
        "a row layout written on another device did not position the node - localStorage "
        f"was empty, so only the server could have said where it goes: {r}")


def test_a_move_is_a_mutation_and_saves():
    r = _report("move_saves_layout")
    if r is None:
        return
    assert r["putsBeforeDrag"] == 0, (
        f"the fixture is broken: a PUT fired before the drag ({r['putsBeforeDrag']}), so "
        "the assertion below could be rescued by a stale pending debounce")
    assert r["after"] != r["before"], f"the drag never moved the node: {r}"
    want = [int(r["after"][0][:-2]), int(r["after"][1][:-2])]
    assert r["putLayouts"] and r["putLayouts"][-1] == want, (
        "a pure move never reached the row - the drop is the mutation, nothing renders "
        f"after it, so without the drag-end save the position dies with the tab: {r}")


def test_dev_rig_never_puts():
    r = _report("dev_no_put")
    if r is None:
        return
    assert r["puts"] == 0, (
        f"a no-login dev rig PUT the row ({r['puts']}) - the isLiveUser gate is gone; "
        "localStorage is the dev rig's only store (#368)")


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
