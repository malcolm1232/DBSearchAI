"""#920 - the document kinds sit under "Files & Links" and are gated on their OWN requirement.

The owner's words (260822): "shall we do sharepoint and gdrive for ANYONE to sign in, do it
under below Files & Local... so put another name for now?" - and, on the defect that opened
the card: "i connected my google account for mex3woof, i still cant connect gdrive... why?"

The answer was that the gate asked the wrong question. It asked which BRAND ROW a kind was
filed under, and Google Drive was filed under "Google Cloud", so it demanded a Google account
- while slice 1 (#712) reads an "anyone with the link" folder with the deployment's own API
key and needs no Google account at all. The gate was lying about its own requirement.

So the fix moves the gate from the PROVIDER to the KIND, and these are the two halves that
have to hold together:
  1. gdrive is addable by any signed-in user, with nothing linked (the owner's requirement).
  2. sharepoint is NOT - its ingest runs on the caller's own Microsoft consent, and moving
     the tile into another row does not change that.
A fix that only did (1) would pass a "can I add gdrive" test and would ALSO pass if the gate
had simply been deleted, which is why (2) and databases_still_gated are in here: each is a
control that goes red on the over-broad version of this change.

    PYTHONPATH=src python3 tests/selftest_920_files_and_links.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402  the shared jsdom gate (#792)

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_files_and_links_dom_probe.mjs"

_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the #920 Files & Links DOM check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the Files & Links regroup ({scenario})")
    return _domgate.resolve(_dom[scenario])


def test_the_row_is_renamed_and_the_brand_row_is_gone():
    r = _report("unlinked_can_add_gdrive")
    if r is None:
        return
    rows = r["railRows"]
    assert "Files & Links" in rows, (
        f"the renamed row is not on the rail at all; rows are {rows}")
    assert "Files & Local" not in rows, (
        "the old name is still on the rail, so the rename did not happen where the user "
        f"looks: {rows}")
    assert "Microsoft 365" not in rows, (
        "the Microsoft 365 row is still there. SharePoint was its only kind and has moved, "
        f"so what is left is a row advertising nothing: {rows}")


def test_an_unlinked_user_can_add_google_drive():
    """The owner's requirement, verbatim: "i need to NOT be connected to google and still use
    the gdrive and connect link"."""
    r = _report("unlinked_can_add_gdrive")
    if r is None:
        return
    assert r["filesRowExists"], "no Files & Links row for a signed-in user who linked nothing"
    assert not r["filesGated"], (
        "the whole Files & Links row is gated for a caller with nothing linked, so upload - "
        f"the one door every hosted user has - is shut too: {r['filesMenu']}")
    assert r["gdriveOffered"], (
        f"Google Drive is not offered under Files & Links: {r['filesMenu']}")
    assert "gdrive" in r["filesMenu"]["addable"], (
        "Google Drive is listed but gated for a caller with no Google account, which is the "
        f"exact defect this card was opened for: {r['filesMenu']}")
    assert r["nodeAdded"], (
        "clicking Google Drive added no node, so the tile is decorative for this caller")


def test_the_added_node_is_filed_under_the_adder():
    """#920: a public-link document has no per-user ACLs to mirror, so it is filed private to
    whoever added it - like an upload. Without this the store composes with no ACL and LAW 2
    (default-deny) leaves it invisible even to its owner."""
    r = _report("unlinked_can_add_gdrive")
    if r is None:
        return
    assert r["nodeAdded"], "no node to check the audience of"
    assert r["aclCarriesAdder"], (
        "the new Google Drive node does not carry the adder's own oid, so it is either "
        "unreachable under LAW 2 or reachable by someone who never added it")


def test_sharepoint_still_asks_for_the_account_it_actually_needs():
    """The control against an over-broad fix. Moving the tile does not move the requirement:
    SharePoint ingests on the caller's OWN Microsoft consent, so an unlinked caller must be
    told that - and told it on the tile, not by the whole row shutting."""
    r = _report("unlinked_can_add_gdrive")
    if r is None:
        return
    m = r["filesMenu"]
    assert r["sharepointOffered"], f"SharePoint did not move into Files & Links: {m}"
    assert "sharepoint" in m["gatedKinds"], (
        "SharePoint is offered as addable to a caller with no Microsoft account. The regroup "
        f"was meant to move the gate to the kind, not to delete it: {m}")
    assert any("Microsoft" in c for c in m["gatedCtas"]), (
        f"the SharePoint tile does not say which account would unlock it: {m['gatedCtas']}")


def test_linking_microsoft_opens_sharepoint():
    """The other half of the same clause. If SharePoint were gated by something that never
    lifts, the assertion above would pass on a permanently dead tile."""
    r = _report("entra_linked_opens_sharepoint")
    if r is None:
        return
    m = r["filesMenu"]
    assert "sharepoint" in m["addable"], (
        f"SharePoint stays gated for a caller who HAS linked Microsoft: {m}")
    assert not m["gatedKinds"], f"something is still gated for this caller: {m}"


def test_the_database_rows_keep_their_brand_gate():
    """ADR 0022/0024: a database query really does run as you, so the brand gate there is
    load-bearing. This is the control that fails if the gate was loosened globally.

    #949 (supersedes #823): the gate is now shown as a BANNER above the services, not as a
    substitute for them - so a gated row REVEALS its services (the owner's point: a user must
    see what connecting unlocks) but none of them is ADDABLE. The load-bearing property is
    unchanged and is what this control checks: an unlinked caller can add nothing here."""
    r = _report("databases_still_gated")
    if r is None:
        return
    assert r["googleRowExists"], "the Google Cloud row disappeared with its document kind"
    g = r["googleMenu"]
    assert g["gate"], (
        f"Google Cloud no longer gates a caller who has not linked Google: {g}")
    assert not g["addable"], (
        f"a gated Google Cloud row has an ADDABLE service for an unlinked caller: {g}")
    # #949: the service is now revealed, as a GATED tile - the row no longer hides what it offers.
    assert "bigquery" in g["gatedKinds"], (
        f"the gated Google Cloud row hides its service instead of revealing it gated (#949): {g}")
    az = r["azureMenu"]
    assert az and az["gate"] and not az["addable"], (
        f"Azure no longer gates an unlinked caller: {az}")


def test_bigquery_did_not_travel_with_gdrive():
    """The regroup moves DOCUMENT kinds only. BigQuery in Files & Links would be a database
    with no credential gate at all."""
    r = _report("unlinked_can_add_gdrive")
    if r is None:
        return
    m = r["filesMenu"]
    assert "bigquery" not in m["kinds"], (
        f"BigQuery followed the document kinds into the ungated row: {m}")
    for db in ("azure_sql", "postgres", "redshift", "s3"):
        assert db not in m["kinds"], f"{db} is listed under Files & Links: {m}"


def test_the_csv_card_is_gone_but_the_kind_survives():
    """#918 (owner ruling): CSV left the rail because it was a dead end - the panel collected
    `path` and router/structured.py's _make reads only `tables` or `files`, so a CSV node
    configured through the UI could never compose. Any signed-in stranger could reach it.

    Both halves matter. Offering it again re-opens the hollow offer; DELETING the KINDS entry
    would be worse than the bug, because an existing csv node in someone's manifest would
    lose its own identity on render (#368's defect exactly), and a hand-written manifest can
    still declare one with the keys the store actually reads."""
    r = _report("unlinked_can_add_gdrive")
    if r is None:
        return
    m = r["filesMenu"]
    assert "csv" not in m["kinds"], (
        f"the CSV card is back on the rail, and it still cannot compose: {m['kinds']}")
    canvas = CANVAS.read_text()
    assert "csv:" in canvas, (
        "the KINDS entry for csv was deleted along with its card - an existing csv node "
        "would no longer render as itself")
    # the control: removing csv must not have taken the row's real kinds with it
    for kind in ("upload", "gdrive", "sharepoint", "local"):
        assert kind in m["kinds"], f"{kind} disappeared from Files & Links: {m['kinds']}"


def test_a_linked_user_sees_the_same_row():
    """The regroup is not conditional on linkage - it is where the kinds LIVE now."""
    r = _report("linked_user_unchanged")
    if r is None:
        return
    assert r["filesRowExists"] and r["gdriveOffered"], (
        f"a caller with Google linked sees a different Files & Links row: {r}")
    assert "gdrive" in r["filesMenu"]["addable"], (
        f"Google Drive is gated for a caller who linked Google: {r['filesMenu']}")


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {e}")
            except Exception as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
