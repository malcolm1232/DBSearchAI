"""#951 - a canvas mount torn down BEFORE its manifest arrives must not write the row.

THE DEFECT, from prod logs 260824 (account acct_e438...): the owner's gdrive and
sharepoint_link nodes vanished from Connectors while Admin still listed their documents.
Not a display bug - the workspace ROW had been overwritten with `stores: []`. The warm
in-process catalog still held the built stores, which is exactly why the documents outlived
the nodes, and why #948's Admin listing kept showing them.

The window is entirely client-side and only about a second wide:

    wire-up        state=[] is set SYNCHRONOUSLY
    bootCanvas     booting=false the moment AUTH resolves
    loadLiveUser   THEN asks the server what the row actually holds (async)
    unmountCanvas  flushes a row save FIRST, before alive=false - #818 on purpose, so a
                   just-added node survives the user navigating away
    pushRowSave    no alive gate, no booting gate (saveCanvas has one; flushRowSave does not
                   go through saveCanvas), and lastRowSave is still null because markRowClean
                   only runs after hydration - so the dirty-check cannot suppress it either

An unmount inside that window therefore PUTs an authoritative empty over a good row, with
keepalive so it lands even as the surface dies. On prod the trigger was a wedged /chat/stream
(#952) that had the user navigating and retrying: three /config+/auth/me pairs in two seconds,
a GET /router/manifest at 05:25:48.712, a PUT 1.1s later, and every later hydrate reading an
empty row - no POST /router/compose ever again, which is the empty-branch signature.

#731 ("stores:[] is AUTHORITATIVE empty - the owner deleted their last store") and #818 ("the
save must survive an unmount") are each right on their own. Together they let a mount that has
loaded nothing at all claim to be authoritative. The fix must keep BOTH: a real delete-all
still has to persist, which is what the second test here controls for.

    PYTHONPATH=src python3 tests/selftest_951_unmount_before_hydrate.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_unmount_before_hydrate_dom_probe.mjs"

_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the #951 unmount-before-hydrate check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"unmount before hydrate ({scenario})")
    return _domgate.resolve(_dom[scenario])


def test_an_unmount_before_hydration_never_writes_an_empty_row():
    """THE DEFECT. The manifest GET is held open, the surface is unmounted, and only then does
    the response land. A mount that never learned what the row held must write NOTHING - an
    empty PUT here is the owner's whole workspace destroyed."""
    r = _report("unmount_before_hydrate")
    if r is None:
        return
    assert r["emptyPuts"] == 0, (
        "a canvas mount that was torn down BEFORE its manifest arrived wrote "
        f"stores:[] over the stored row - the #951 data loss. PUTs seen: {r['putBodies']}")
    print("  PASS  an unmount before hydration writes no empty row (#951)")


def test_a_real_delete_all_still_persists():
    """THE CONTROL, and the reason the fix cannot just be 'refuse empty writes'. #731's whole
    contract is that deleting your last store STICKS - the row must be able to hold an
    authoritative empty, or delete-all silently resurrects on the next load."""
    r = _report("normal_delete_all")
    if r is None:
        return
    assert r["hydratedNodes"] >= 2, (
        f"the fixture never hydrated its two stores, so this control proves nothing: {r}")
    assert r["nodesAfter"] == 0, f"the deletes did not remove the nodes: {r}"
    assert r["emptyPuts"] >= 1, (
        "a HYDRATED canvas whose user deleted every node did not persist the empty row - the "
        f"fix broke #731's delete-all contract: {r}")
    print("  PASS  a hydrated delete-all still persists an empty row (#731 control)")


if __name__ == "__main__":
    failures = []
    for name in ["test_an_unmount_before_hydration_never_writes_an_empty_row",
                 "test_a_real_delete_all_still_persists"]:
        try:
            globals()[name]()
        except AssertionError as e:
            failures.append(name); print(f"FAIL  {name}\n      {e}")
        except Exception as e:
            failures.append(name); print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
