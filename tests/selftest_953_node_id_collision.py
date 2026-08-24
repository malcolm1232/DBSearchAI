"""#953 - a new node's id is allocated against what EXISTS, never by counting.

addNode named a new store `kind + "-" + (count-of-kind + 1)`. That is only collision-free
while nothing is ever deleted: with gdrive-1 and gdrive-2 on the canvas, deleting gdrive-1
drops the count to 1, so the NEXT add is named "gdrive-2" - a duplicate of a LIVE node. Two
nodes then share one server store id: compose builds a single store for both, and deleting
either node purges (#947) the data the other still claims to show.

This is the id-recycling half of the owner's 260824 incident: after #951's row wipe emptied
their canvas, a fresh "add a gdrive node" was handed the id gdrive-1 - the same id as the
still-warm server store holding their real data - so the empty new node and the old data
were welded together, and the node's delete took the data with it ("when i add a gdrive
node, an empty node appears. but when i delete, in my admin, gdrive disappears"). #951
closed the entry door; this closes the welding.

The fix is first-free-suffix: scan the CURRENT state for the smallest unused number. A
freed id may be reused - after a #947 delete the server purged that store, so the name is
genuinely free - but a live one may never be taken.

    PYTHONPATH=src python3 tests/selftest_953_node_id_collision.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_node_id_collision_probe.mjs"

_dom = {}


def _report():
    if "run" not in _dom:
        if not _domgate.gate("the #953 node-id collision check"):
            _dom["run"] = None
        else:
            _dom["run"] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS)],
                "node id allocation")
    return _domgate.resolve(_dom["run"])


def test_two_adds_number_sequentially():
    r = _report()
    if r is None:
        return
    assert r["afterTwoAdds"] == ["gdrive-1", "gdrive-2"], (
        f"two adds did not produce gdrive-1, gdrive-2: {r['afterTwoAdds']}")
    print("  PASS  two adds allocate gdrive-1 then gdrive-2")


def test_an_add_after_a_delete_never_takes_a_live_id():
    """THE DEFECT: count-based naming hands the new node 'gdrive-2' - a duplicate of the
    survivor. Two nodes, one store id, and either delete purges the other's data."""
    r = _report()
    if r is None:
        return
    assert r["afterDelete"] == ["gdrive-2"], f"the delete fixture broke: {r['afterDelete']}"
    assert not r["hasDuplicate"], (
        f"an add after a delete produced a DUPLICATE node id: {r['afterReAdd']} - two nodes "
        "now share one server store, and deleting either purges the other's data (#947)")
    assert sorted(r["afterReAdd"]) == ["gdrive-1", "gdrive-2"], (
        f"the new node did not take the freed id: {r['afterReAdd']}")
    print("  PASS  an add after a delete fills the hole instead of duplicating a live id")


if __name__ == "__main__":
    failures = []
    for name in ["test_two_adds_number_sequentially",
                 "test_an_add_after_a_delete_never_takes_a_live_id"]:
        try:
            globals()[name]()
        except AssertionError as e:
            failures.append(name); print(f"FAIL  {name}\n      {e}")
        except Exception as e:
            failures.append(name); print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
