"""#923 - the uploads node is FIRST-CLASS, driven in a real DOM across remounts.

The owner's three rulings (260821), each a claim the pre-#923 canvas fails:

 - add: clicking Files & Links -> "Upload files" in the sidebar ADDS the node (selected,
   panel open, NO modal - the modal used to be the first hop and no node ever appeared),
   the row's layout carries the "your-documents" marker, and a remount restores the node
   at 0 docs (it used to exist only while documents did).
 - refresh_docs (#921): a user whose ONLY source is uploads refreshes - the row is
   authoritative-empty stores, and the old boot branch never synced documents, so the
   node vanished until the next upload resurrected it.
 - delete_full: deleting the node deletes THE DATA - the caller's own documents, via the
   real per-document DELETE - after an inline confirm that names the count; a document
   merely shared TO the caller is not theirs and survives.
 - delete_empty: an empty node deletes non-destructively and STAYS gone after remount.

    PYTHONPATH=src python3 tests/selftest_923_upload_node_lifecycle.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402  the shared jsdom gate (#792)

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_upload_node_lifecycle_dom_probe.mjs"

_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the upload-node lifecycle DOM check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the upload-node lifecycle ({scenario})")
    return _domgate.resolve(_dom[scenario])


def test_sidebar_add_creates_a_persistent_selected_node_not_a_modal():
    r = _report("add")
    if r is None:
        return
    assert r["nodeBefore"] is False
    assert r["nodeAfterAdd"] is True, "the sidebar's Upload files never added a node"
    assert r["modalOpen"] is False, (
        "THE DEFECT - the sidebar entry still jumps straight into the upload modal")
    assert r["panelIsOverview"] is True, (
        "the new node was not auto-selected into the overview panel")
    assert r["freshness"] == "0 docs", f"an empty node must say so: {r['freshness']!r}"
    assert r["lastPutLayoutHasMarker"] is True, (
        "the row's layout never carried 'your-documents' - nothing durable marks the node")
    assert r["nodeAfterRemount"] is True, (
        "THE DEFECT - the empty node did not survive the remount")
    assert r["freshnessAfterRemount"] == "0 docs"
    print("  PASS  sidebar add -> persistent selected node, panel open, no modal")


def test_the_nodes_upload_button_opens_the_file_picker():
    """#950, owner-reported 260824: "i click upload files, nth happens then im like huh?".

    Reproduced on prod before the fix: node already selected (#923 auto-selects on add), panel
    already open, spPicker display:none - and the click left every one of those unchanged. The
    handler's entire body was `selected=node.uid; renderAll()`, so in the common case the
    button did nothing at all.

    #917 ruled that clicking the NODE lands on the overview panel rather than jumping to the
    modal, and that still holds - it is the node CARD's click that selects. But a control whose
    label is an imperative has to honour it: "Upload files" that cannot upload a file is the
    #654 hollow-offer shape, and the owner hit it within minutes of using the surface.
    """
    r = _report("node_upload_button")
    if r is None:
        return
    assert r["nodeAdded"] and r["buttonFound"], f"no upload node/button to drive: {r}"
    # the precondition that made this invisible - without it the test could pass on a node
    # whose panel simply opened, proving nothing about the defect
    assert r["selectedBeforeClick"], (
        "the node was NOT already selected, so this run cannot show the defect: the click had "
        f"a panel to open and the no-op would be masked: {r}")
    assert r["modalBeforeClick"] is False, f"the modal was already open before the click: {r}"
    assert r["modalAfterClick"] is True, (
        "clicking the node's 'Upload files' button did not open the file picker - it is still "
        f"the select-the-node no-op the owner reported: {r}")
    assert r["filePickerPresent"], f"the modal opened without a file input in it: {r}"
    print("  PASS  the node's Upload files button opens the file picker (#950)")


def test_there_is_exactly_one_upload_affordance_and_it_is_on_the_node():
    """The owner's ruling, 260824: "i dont want the 'upload files' in the RIGHT hand side ...
    if not there are two 'upload files' ... so ONE single 'upload files' under canvas node is
    good enough. the right side just shows what files are present."

    Once #950 gave the node's button the picker, the panel's button became a SECOND control
    for one action, on screen at the same moment - the duplication that caused the original
    confusion, arriving from the other side. The panel keeps per-DOCUMENT actions (Share,
    Delete), which belong to a listing; it carries no upload control."""
    r = _report("node_upload_button")
    if r is None:
        return
    assert r["buttonLabel"] == "Upload files", (
        f"the node's one upload control is not labelled 'Upload files': {r['buttonLabel']!r}")
    assert r["panelUploadButtons"] == 0, (
        f"the panel still carries {r['panelUploadButtons']} upload button(s) - there are two "
        "'Upload files' on screen again, which is exactly what the owner asked to remove")
    print("  PASS  exactly one upload affordance, and it is the node's button (#950)")


def test_a_refresh_with_only_uploads_keeps_the_node():
    r = _report("refresh_docs")
    if r is None:
        return
    assert r["nodePresent"] is True, (
        "#921 - authoritative-empty stores booted without a documents sync, so the node "
        "vanished on refresh until the next upload resurrected it")
    assert r["freshness"] == "5 docs", f"the count lies: {r['freshness']!r}"
    print("  PASS  a refresh with only uploads keeps the node (#921)")


def test_node_delete_deletes_the_callers_documents_and_spares_shared():
    r = _report("delete_full")
    if r is None:
        return
    assert r["nodePresent"] is True and r["freshness"] == "3 docs"
    assert r["confirmInModal"] is True, (
        "no confirm MODAL named the blast radius before a destructive delete")
    assert r["confirmInPanel"] is False, (
        "the confirm rendered in the sidebar panel - the owner ruled it a modal (260821)")
    assert r["deletesWhileArmed"] == [], (
        f"arming the confirm already sent deletes: {r['deletesWhileArmed']}")
    assert r["keepClosesModal"] is True, "Keep did not close the confirm modal"
    assert r["keepDeletedNothing"] is True and r["nodeAfterKeep"] is True, (
        "Keep was not a no-op - it deleted something or removed the node")
    assert r["deletesSent"] == ["own-1", "own-2"], (
        f"the node delete must delete exactly the caller's OWN documents: {r['deletesSent']}")
    assert r["sharedSurvives"] is True, (
        "a document merely shared TO the caller was deleted - not theirs to destroy")
    assert r["modalClosedAfter"] is True, "the confirm modal stayed open after the delete"
    assert r["nodeAfterDelete"] is False
    assert r["nodeAfterRemount"] is True and r["freshnessAfterRemount"] == "1 doc", (
        "the shared doc still exists - the overview must auto-adopt and show it honestly: "
        f"{r['nodeAfterRemount']}, {r['freshnessAfterRemount']!r}")
    print("  PASS  node delete deletes the caller's documents; shared-to-me survives")


def test_an_empty_node_deletes_quietly_and_stays_gone():
    r = _report("delete_empty")
    if r is None:
        return
    assert r["nodePresent"] is True
    assert r["nodeAfterDelete"] is False, "an empty node should delete without a confirm"
    assert r["docDeletes"] == [], (
        f"deleting an EMPTY node sent document deletes: {r['docDeletes']}")
    assert r["lastPutLayoutHasMarker"] is False, (
        "the marker never left the row - the node would resurrect on every reload")
    assert r["nodeAfterRemount"] is False, "the deleted empty node resurrected on remount"
    print("  PASS  an empty node deletes quietly and stays gone")


if __name__ == "__main__":
    test_sidebar_add_creates_a_persistent_selected_node_not_a_modal()
    test_the_nodes_upload_button_opens_the_file_picker()          # #950
    test_there_is_exactly_one_upload_affordance_and_it_is_on_the_node()   # #950
    test_a_refresh_with_only_uploads_keeps_the_node()
    test_node_delete_deletes_the_callers_documents_and_spares_shared()
    test_an_empty_node_deletes_quietly_and_stays_gone()
    print("\nUPLOAD NODE LIFECYCLE SELF-TEST PASSED.")
