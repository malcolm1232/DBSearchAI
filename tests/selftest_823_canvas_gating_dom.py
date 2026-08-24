"""#823 - adding a source is gated on sign-in and on the provider being linked.

The owner's words: "If users never sign in, can't add data cos need to assign them ID for
their account data. If msft not signed in, can't add data so left side canvas adding of node
is greyed out. On hover = connect your msft account! Do same for AWS and others."

The UX ruling that goes with it is the opposite of greyed-out ("so ugly, bad UX"): the row
stays full colour and reads as live, and the AFFORDANCE appears when you interact with it.
So the assertions below are about what the flyout OFFERS, not about a disabled attribute -
and one of them checks the row was not quietly disabled instead.

Three layers, plus the two controls that stop the gate being over-broad:
  1. signed_out - real login configured, nobody signed in: every provider offers sign-in.
  2. unlinked   - signed in, nothing vaulted: the cloud providers offer Connect, while
                  Files & Links (which needs only an account) still offers its services.
  3. linked     - everything vaulted: every provider opens normally.
  4. dev_rig    - no real login configured: nothing is gated, because a dev rig has
                  signed_in permanently false and drives identity through X-DBSearch-User.
                  Gating on !signed_in instead of isDemoMode() would break every dev rig.

    PYTHONPATH=src python3 tests/selftest_823_canvas_gating_dom.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402  the shared jsdom gate (#792)

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_gating_dom_probe.mjs"

# #920: "Microsoft 365" left the rail - SharePoint was its only kind and now sits in the
# renamed "Files & Links" row, gated on its OWN requirement rather than on the brand row it
# happens to be filed under. What survives here is the set of rows whose every kind really
# does query as you, which is what this gate was always about.
CLOUD = ("azure", "google", "aws")
_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the #823 gating DOM check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the canvas gating rules ({scenario})")
    return _domgate.resolve(_dom[scenario])


def test_signed_out_every_provider_offers_sign_in():
    r = _report("signed_out")
    if r is None:
        return
    for key in CLOUD + ("files",):
        p = r["providers"][key]
        assert p["present"], f"{key} row is missing from the rail entirely"
        assert p["svcCount"] == 0, (
            f"{key} still lists {p['svcCount']} addable services to a visitor who is not "
            "signed in, so the canvas offers what the server will refuse")
        assert p["ctaText"], f"{key} offers no sign-in affordance at all: {p}"
        assert "sign in" in p["ctaText"].lower(), (
            f"{key}'s affordance does not invite a sign-in: {p['ctaText']!r}")


def test_signed_in_but_unlinked_offers_connect_for_that_provider():
    r = _report("unlinked")
    if r is None:
        return
    for key in CLOUD:
        p = r["providers"][key]
        assert p["svcCount"] == 0, (
            f"{key} offers {p['svcCount']} services to a user who has linked nothing, so "
            "every one of them composes to a credential error (#551's always-fails tile)")
        assert p["ctaText"] and "connect" in p["ctaText"].lower(), (
            f"{key} does not offer to connect the account it needs: {p['ctaText']!r}")


def test_files_and_local_needs_only_an_account():
    """Layer 3, and the control that stops the gate swallowing the one group a hosted user
    can always act on: upload/csv/local need no third-party credential at all."""
    r = _report("unlinked")
    if r is None:
        return
    p = r["providers"]["files"]
    assert p["svcCount"] > 0, (
        "Files & Links is gated for a signed-in user, so an ordinary hosted user who has "
        "linked no cloud provider cannot add ANY source - which is the whole product")
    assert p["ctaText"] is None, f"Files & Links offers a connect affordance it does not need: {p}"


def test_a_linked_user_sees_every_service():
    """The over-broad-gate control: with everything vaulted nothing may be withheld."""
    r = _report("linked")
    if r is None:
        return
    for key in CLOUD + ("files",):
        p = r["providers"][key]
        assert p["svcCount"] > 0, (
            f"{key} withholds its services from a user who HAS linked it: {p}")
        assert p["ctaText"] is None, f"{key} still nags a linked user to connect: {p}"


def test_a_dev_rig_is_never_gated():
    """No real login configured means identity arrives through X-DBSearch-User and
    signed_in is permanently false. Gating on that would break every dev rig."""
    r = _report("dev_rig")
    if r is None:
        return
    for key in CLOUD + ("files",):
        p = r["providers"][key]
        assert p["svcCount"] > 0, (
            f"{key} is gated on a rig with no real login configured, which breaks the dev "
            f"rigs every selftest and every local drive runs on: {p}")


def test_the_row_is_never_rendered_as_disabled():
    """The owner rejected greyed-out explicitly. The gate lives in the flyout."""
    for scenario in ("signed_out", "unlinked"):
        r = _report(scenario)
        if r is None:
            return
        for key, p in r["providers"].items():
            assert not p.get("rowGreyed"), (
                f"{key} is rendered as a disabled row in {scenario}, which is the UX the "
                "owner rejected; the affordance belongs in the flyout")


def test_the_rail_and_the_account_panel_agree_about_every_provider():
    """The canvas keeps its own copy of the four link facts, because this surface has no
    cross-surface imports. A copy that can drift is worth nothing, so this is the thing that
    stops it: for every idp the rail gates on, the account panel must name it the same way,
    read the same enabled flag, and send people to the same place to link it."""
    import re

    canvas = CANVAS.read_text()
    roster_src = (ROOT / "src/dbsearch/server/static/js/ui/account.js").read_text()

    def _fields(blob, pairs):
        out = {}
        for name, key in pairs.items():
            m = re.search(key + r'\s*:\s*(?:"([^"]*)"|(null))', blob)
            out[name] = None if (m and m.group(2)) else (m.group(1) if m else "MISSING")
        return out

    # A LIST, not a dict keyed by idp. Two rail rows used to share one idp (Azure and
    # Microsoft 365 both linked through entra), so keying by it let the second row overwrite
    # the first and a drift in Azure alone became invisible. The mutation matrix caught this
    # guard being vacuous, which is the whole reason the mutation exists. #920 removed the
    # Microsoft 365 row, so no two rows share an idp TODAY - the list stays because the next
    # row to share one must not silently re-open the hole.
    rail = []
    for blob in re.findall(r'\{key:"[a-z0-9_]+".*?connect:(?:"[^"]*"|null)\}',
                           canvas, re.S):
        f = _fields(blob, {"key": "key", "idp": "link", "who": "who", "flag": "flag",
                           "connect": "connect"})
        if f["idp"]:
            rail.append(f)
    assert rail, "no linked providers parsed out of the canvas PROVIDERS table"
    assert len(rail) >= 3, (
        f"only {len(rail)} linked provider rows parsed; the regex has stopped matching the "
        "table it is supposed to police, which would make every assertion below vacuous")

    panel = {}
    for blob in re.findall(r'\{\s*key:\s*"[a-z0-9_]+".*?\}', roster_src, re.S):
        f = _fields(blob, {"idp": "key", "who": "name", "flag": "enabledFlag",
                           "connect": "connect"})
        if f["idp"] in ("entra", "google", "aws"):
            panel[f["idp"]] = f

    # EVERY row is checked on its own, so a drift in one of two rows sharing an idp is caught.
    for got in rail:
        want = panel.get(got["idp"])
        assert want, (
            f"the {got['key']} row gates on idp {got['idp']!r}, which the account panel "
            "cannot link, so the palette asks for a credential nothing can supply")
        for field in ("who", "flag", "connect"):
            assert got[field] == want[field], (
                f"the {got['key']} row and the account panel disagree about {field}: rail "
                f"says {got[field]!r}, panel says {want[field]!r}")

    for idp in panel:
        assert any(r["idp"] == idp for r in rail), (
            f"the account panel can link {idp!r} but no rail row gates on it, so a provider "
            "the user can connect is not one the palette ever asks about")


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
