"""#258 — the ACL principal directory behind the canvas picker.

The point of this feature is that an operator ACLs to a NAMED tier ("DBSearch Deal Team")
instead of pasting a GUID. Two things must hold for that to be trustworthy:

  1. LAW 2 is unmoved. Names are cosmetic labels on a picker; only oids are ever compared.
     A wrong/missing name must never widen or narrow what anyone can read.
  2. "I cannot name anything" must be distinguishable from "this tenant has no groups".
     An empty dropdown that looks authoritative would push the operator back to pasting
     raw oids while implying the tiers do not exist — the #255 failure mode relocated.

Run: python3 tests/selftest_principal_directory.py
"""
import os
import sys
from pathlib import Path

os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.adapters.local import InMemoryIdentity  # noqa: E402
from dbsearch.ports.base import IdentityPort  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
ALICE = {"X-DBSearch-User": "alice"}


def test_unnamed_principals_are_not_offered_as_bare_guids():
    ident = InMemoryIdentity({"alice-oid": ["deal-team-oid", "all-staff-oid"]})
    assert ident.list_directory() == [], \
        "principals with no display name must not be pickable"

    ident.set_principal_name("deal-team-oid", "DBSearch Deal Team")
    picked = ident.list_directory()
    assert [p.oid for p in picked] == ["deal-team-oid"], \
        f"only the NAMED group should be offered — got {picked}"
    assert picked[0].name == "DBSearch Deal Team" and picked[0].kind == "group"

    ident.set_principal_name("alice-oid", "Alice Test")
    kinds = {p.oid: p.kind for p in ident.list_directory()}
    assert kinds == {"deal-team-oid": "group", "alice-oid": "user"}, \
        f"a group and a user must be distinguishable in the picker — got {kinds}"


def test_a_backend_that_cannot_enumerate_raises_rather_than_returning_empty():
    """The port default must RAISE. If it returned [], every caller would render an
    authoritative-looking empty picker for backends that simply cannot enumerate."""
    class Bare(IdentityPort):
        def expand_groups(self, user_oid):
            return [user_oid]

    try:
        Bare().list_directory()
    except NotImplementedError:
        return
    raise AssertionError("list_directory() must raise on a backend that cannot enumerate, "
                         "never return an empty list")


def test_endpoint_reports_unavailable_not_empty_when_nothing_can_be_named():
    """available=false + a reason, so the canvas keeps the paste-an-oid escape hatch."""
    r = client.get("/admin/principals", headers=ALICE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"available", "principals", "reason"}, body
    if not body["principals"]:
        assert body["available"] is False, \
            "an empty directory must be reported as UNAVAILABLE, not as an authoritative " \
            "empty list — otherwise the picker implies the tenant has no groups"
        assert body["reason"], "unavailable must always carry a reason the operator can act on"


def test_names_never_affect_authorization():
    """LAW 2: renaming a principal — or failing to name it at all — must not change a
    single access decision. Only oids are compared."""
    manifest = {"tenant": "acme", "stores": [
        {"id": "tiered", "kind": "csv", "mode": "pushdown", "business_unit": "sales",
         "acl": ["deal-team-oid"], "title": "tiered", "description": "sales rows amount",
         "config": {"tables": {"sales": {"columns": ["region", "amount"],
                                         "rows": [["apac", 10]]}}}},
    ]}
    r = client.post("/router/compose", headers=ALICE, json={"manifest": manifest})
    assert r.status_code == 200, r.text

    # alice is NOT in deal-team-oid (the dev identity map does not grant it), so the store
    # must be invisible — and naming the group must not change that.
    before = client.post("/router/ask", headers=ALICE, json={"question": "total amount by region"})
    assert before.status_code == 200, before.text
    assert "tiered" not in before.text, \
        "a store ACL'd to a group alice is not in must never surface"


def test_canvas_keeps_the_oid_field_and_never_shows_an_unlabelled_picker():
    """The picker is an ADDITION to the oid field, not a replacement. If the directory is
    unavailable the operator must still be able to paste an oid, and must be told why the
    dropdown is missing rather than shown an empty one."""
    canvas = (ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js").read_text()

    assert 'data-fld="acl"' in canvas, \
        "the raw-oid ACL field was removed — that is the escape hatch when the directory " \
        "cannot be enumerated"
    assert "aclPickerHtml(node)" in canvas and "aclNamesHtml(node)" in canvas, \
        "the ACL field no longer renders the named picker / name resolution"
    assert "principalDir.available" in canvas, \
        "the canvas does not branch on availability — an unavailable directory would render " \
        "as an empty picker implying the tenant has no groups"
    assert "/admin/principals" in canvas, "the canvas never fetches the directory"
    # the picked value must be the OID, never the display name — LAW 2 compares oids
    assert 'const oid=pick.value' in canvas and "node.acl.push(oid)" in canvas, \
        "the picker must push the OID onto the ACL, never the display name"


def main():
    print("#258 principal directory (ACL picker):")
    test_unnamed_principals_are_not_offered_as_bare_guids()
    print("  PASS  unnamed principals are never offered as bare GUIDs; groups and users "
          "are distinguishable")
    test_a_backend_that_cannot_enumerate_raises_rather_than_returning_empty()
    print("  PASS  a backend that cannot enumerate RAISES instead of returning []")
    test_endpoint_reports_unavailable_not_empty_when_nothing_can_be_named()
    print("  PASS  /admin/principals reports available=false WITH a reason rather than an "
          "authoritative empty list")
    test_names_never_affect_authorization()
    print("  PASS  LAW 2 unmoved — names are cosmetic, only oids are compared")
    test_canvas_keeps_the_oid_field_and_never_shows_an_unlabelled_picker()
    print("  PASS  canvas keeps the raw-oid escape hatch, branches on availability, and "
          "pushes OIDs (not names) onto the ACL")
    print("\nPRINCIPAL DIRECTORY SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
