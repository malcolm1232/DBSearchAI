"""#949 - a gated brand row REVEALS its services, with one connect banner above them.

The owner, 260824: "instead of azure -> connect your msft, its azure -> all other svc ->
Connect your msft to enable all services... i want to show users what azure can do. if we
gate the UI via 'connect your msft'... they cant see there is different connectors. same for
gcp and aws."

This supersedes #823, which hid a fully-gated row's services behind a single Connect CTA so
nobody clicked a tile that would 403. The owner's point is the opposite: a user cannot decide
to connect Microsoft / Google / AWS if the canvas never shows what those unlock. So:
  - a gated brand row now LISTS its services (as gated tiles), and
  - the connect action moves to a BANNER above them ("Connect your <who> account to enable
    all N services"), and
  - clicking any gated tile still routes to that same connect, never a 403.

The #823 property that must NOT regress: none of those revealed services is ADDABLE by an
unlinked caller, and a mixed row (Files & Links) is unchanged. selftest_920 and selftest_823
carry those controls; this file pins the reveal itself.

    PYTHONPATH=src python3 tests/selftest_949_brand_rows_reveal_services.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_gating_dom_probe.mjs"

_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the #949 brand-row reveal ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the brand-row reveal ({scenario})")
    return _domgate.resolve(_dom[scenario])


AZURE_SVCS = {"azure_sql", "postgres", "mysql", "synapse", "cosmos_db"}


def test_a_gated_azure_row_reveals_all_five_services():
    """The owner's exact example: Azure, unlinked, must show its five database services rather
    than a bare 'Connect Microsoft' - so the user can see what Azure offers."""
    r = _report("unlinked")
    if r is None:
        return
    az = r["providers"]["azure"]
    assert set(az["kinds"]) == AZURE_SVCS, (
        f"the gated Azure row does not reveal its five services: {az}")
    assert set(az["gatedKinds"]) == AZURE_SVCS, (
        f"a revealed service is addable to an unlinked caller (the #823 property regressed): {az}")
    assert not az["addable"], f"something is addable on a gated Azure row: {az}"
    print("  PASS  a gated Azure row reveals all five services, all gated, none addable")


def test_the_connect_banner_names_the_provider_and_the_service_count():
    """The connect action is a banner above the services, not a substitute for them, and it
    says what it unlocks - 'Connect your Microsoft account to enable all 5 services'."""
    r = _report("unlinked")
    if r is None:
        return
    az = r["providers"]["azure"]
    assert az["bannerCta"], f"the Azure row has no connect banner action: {az}"
    assert az["gateText"] and "Microsoft" in az["gateText"], (
        f"the banner does not name the provider: {az.get('gateText')}")
    assert "all 5 services" in (az["gateText"] or ""), (
        f"the banner does not say how many services connecting unlocks: {az.get('gateText')}")
    print("  PASS  the banner names the provider and the service count, with a connect action")


def test_google_cloud_with_one_service_reads_naturally():
    """The singular case: Google Cloud gates one service (bigquery), so the banner must say
    'this service', not 'all 1 services'."""
    r = _report("unlinked")
    if r is None:
        return
    g = r["providers"]["google"]
    assert g["gatedKinds"] == ["bigquery"], f"Google Cloud did not reveal bigquery gated: {g}"
    assert "this service" in (g["gateText"] or ""), (
        f"the one-service banner reads unnaturally: {g.get('gateText')}")
    print("  PASS  a one-service row's banner reads 'this service', not 'all 1 services'")


def test_clicking_a_gated_tile_routes_to_connect_not_a_dead_add():
    """#823's real fear - a tile that 403s - is answered by ROUTING the click to connect, not
    by hiding the tile. A gated tile click must reach the connect flow and add no node."""
    r = _report("click_gated")
    if r is None:
        return
    c = r["click"]
    assert c["tileFound"], "no gated Azure tile was rendered to click"
    assert c["nodesAdded"] == 0, f"clicking a gated Azure tile added a node that could only 403: {c}"
    assert c["connectReached"], (
        f"clicking a gated tile was a dead click - it neither linked nor opened connect: {c}")
    print("  PASS  clicking a gated tile routes to connect and adds no node")


def test_the_flyout_width_is_capped_by_the_MENU_not_by_its_content():
    """THE PIXEL DEFECT, owner-caught on prod the same day this shipped.

    The first cut gave `.gate-banner` max-width:none while `.provmenu` itself had only a
    min-width. The banner's prose then ran as one unwrapped line and dragged the whole flyout
    out to ~800px, stretching every service tile with its arrow stranded at the far edge.

    Nothing in this file could catch it: jsdom does no layout, so offsetWidth is 0 and a DOM
    probe reports a perfect-looking tree for a flyout that is visibly broken. So the guard is
    STATIC and it is on the rule that actually decides the width - the parent's cap. A child
    may declare max-width:none only because the menu bounds it; take the cap off the menu and
    any long string can stretch it again.
    """
    css = (ROOT / "src/dbsearch/server/static/css/canvas.css").read_text()
    rule = css.split(".canvas-surface .provmenu {")[1].split("}")[0]
    assert "max-width" in rule, (
        "`.canvas-surface .provmenu` declares no max-width, so its width is decided by whatever "
        "the longest child happens to be - which is exactly how the #949 banner stretched the "
        f"flyout to ~800px on prod. Rule was: {rule!r}")
    print("  PASS  the flyout width is capped on .provmenu, not left to its content")


def test_a_linked_user_sees_the_same_services_now_addable():
    """The over-broad control: once Microsoft is linked, the same revealed services become
    addable and the banner is gone - the reveal is not a permanent 'gated' cosmetic."""
    r = _report("linked")
    if r is None:
        return
    az = r["providers"]["azure"]
    assert set(az["addable"]) == AZURE_SVCS, f"a linked caller cannot add the Azure services: {az}"
    assert not az["gatedKinds"], f"a service stays gated for a linked caller: {az}"
    assert not az["bannerCta"], f"the connect banner still shows for a linked caller: {az}"
    print("  PASS  a linked user sees the same services, now addable, with no banner")


if __name__ == "__main__":
    failures = []
    for name in ["test_a_gated_azure_row_reveals_all_five_services",
                 "test_the_connect_banner_names_the_provider_and_the_service_count",
                 "test_google_cloud_with_one_service_reads_naturally",
                 "test_clicking_a_gated_tile_routes_to_connect_not_a_dead_add",
                 "test_the_flyout_width_is_capped_by_the_MENU_not_by_its_content",
                 "test_a_linked_user_sees_the_same_services_now_addable"]:
        try:
            globals()[name]()
        except AssertionError as e:
            failures.append(name); print(f"FAIL  {name}\n      {e}")
        except Exception as e:
            failures.append(name); print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
