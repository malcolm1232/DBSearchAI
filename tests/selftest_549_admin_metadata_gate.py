"""#549 — LAW 2 holds on the METADATA plane, not just the content plane.

The suite has always had one signed-in identity at a time, so nothing ever asked the
question this file asks: when TWO real people share a deployment, can the second one
enumerate the first one's documents and read the questions she asked?

Measured before the fix: yes to both. `/admin/documents` returned every document in the
deployment with its TITLE, uri and allowed_principals, and `/admin/audit` returned other
users' QUESTION TEXT attributed to their oid — to any caller who was merely signed in.
A title is very often the entire secret ("Q3 redundancy list"), and allowed_principals
hands over the exact target list.

Same class as #87 (segments leaked content without an ACL trim), one route over: #87
fixed the body and the sibling routes were never swept.

Three distinct rules are asserted here, because the routes differ in kind:
  - deployment-wide OBSERVABILITY (audit, telemetry, index, permission-test) is
    operator-only — 403 for an ordinary user. No ordinary-user flow calls these.
  - the DOCUMENT LISTING is not operator-only: an ordinary user legitimately sees THEIR
    OWN documents (that is the "talk to your own data" journey, #548/#539). It is ACL-
    trimmed to the caller's own expanded principals instead, by the same identity port
    retrieval uses — so the listing can never show more than a query would.
  - principals, sources and identities STAY OPEN to any signed-in user, because real
    flows need them (the ACL picker, a user's own ingested SharePoint state, and the
    upload form's group selector). The first cut of this fix gated all three and silently
    broke both the picker and upload itself; only the browser drive caught it. Their real
    defect is that neither is scoped to the caller's tenant/ownership, which predates this
    work and needs scoping in the adapter, not a role check — #550.

A dev rig (no real login configured) must be COMPLETELY unaffected: is_operator() is
True for everyone there by design (ADR 0011 s3), because that box belongs to whoever
runs it. A fix that broke local rigs would just relocate the pain.

    PYTHONPATH=src python3 tests/selftest_549_admin_metadata_gate.py
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import app  # noqa: E402
from dbsearch.server import app as app_module  # noqa: E402

client = TestClient(app)

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
OPERATOR = "99999999-9999-9999-9999-999999999999"

SECRET_TITLE = "Q3 Redundancy List CONFIDENTIAL"
SECRET_QUESTION = "who is on the redundancy list for the Hamburg plant"

_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")

# Deployment-wide observability with no ordinary-user consumer: operator-only.
# /admin/identities JOINED THIS LIST in #881, and the move is the point of that card.
# It sat below as an ordinary-user route on one stated ground: it "fills the UPLOAD form's
# group selector, which refuses to submit empty". #539 DELETED that selector - an upload is
# private to its uploader now, ACL resolved server-side - and the only caller left is the
# "Users and groups" panel, which surfaces/admin.js renders inside `if (operator)`. The
# exemption outlived the journey it protected; #872 then made the response name who holds
# which DIRECTORY ROLE, which turned a stale exemption into the tenant's admin list.
OBSERVABILITY = ("/admin/index", "/admin/telemetry", "/admin/audit", "/admin/identities")

# The routes an ORDINARY user genuinely needs, found by driving the real canvas (#550):
#   /admin/principals backs the ACL picker — gating it left users pasting raw GUIDs.
#   /admin/sources restores a user's own ingested SharePoint state across a reload.
# Their residual over-exposure is a scoping defect that predates this gate, not a role
# problem, so it is carded rather than "fixed" by locking out the people who need them.
# Do not add a route here without finding its non-operator CALLER first: that is the exact
# check whose staleness #881 paid for.
ORDINARY_USER_ROUTES = ("/admin/principals", "/admin/sources")


def _real_login(on: bool, operators: str = ""):
    for k in _VARS:
        os.environ.pop(k, None)
    if on:
        os.environ.update({"AUTH_TENANT_ID": "tid-1", "AUTH_CLIENT_ID": "cid",
                           "AUTH_CLIENT_SECRET": "sec"})
    if operators:
        os.environ["DBSEARCH_OPERATOR_OIDS"] = operators


def _cookie(oid: str) -> dict:
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": "tid-1", "exp": int(time.time()) + 3600})}


def _seed_alices_private_document():
    """Alice ingests a document ACL'd to HER OID ALONE, and asks it something. Bob is on no
    ACL and in no group — an ordinary colleague, not an attacker."""
    r = client.post("/ingest", cookies=_cookie(ALICE), json={
        "external_id": "doc-redundancy-549", "title": SECRET_TITLE,
        "text": "The Hamburg plant redundancy list names 14 staff in the actuator line.",
        "acl": [ALICE], "uri": "upload://q3-redundancy.txt"})
    assert r.status_code == 200, f"seed ingest failed: {r.status_code} {r.text[:200]}"
    client.post("/search", cookies=_cookie(ALICE), json={"question": SECRET_QUESTION})


def test_bob_cannot_enumerate_alices_documents():
    _real_login(True, operators=OPERATOR)
    _seed_alices_private_document()
    body = client.get("/admin/documents", cookies=_cookie(BOB))
    assert body.status_code == 200, f"expected a trimmed listing, got {body.status_code}"
    blob = json.dumps(body.json())
    assert SECRET_TITLE not in blob, "bob can read the TITLE of a document he may not open"
    assert "doc-redundancy-549" not in blob, "bob learns the document exists"
    assert ALICE not in blob, "bob learns exactly who is allowed to read it"


def test_bob_cannot_read_alices_questions():
    _real_login(True, operators=OPERATOR)
    _seed_alices_private_document()
    r = client.get("/admin/audit", cookies=_cookie(BOB))
    blob = json.dumps(r.json()) if r.status_code == 200 else ""
    assert r.status_code == 403, f"audit is deployment-wide observability, got {r.status_code}"
    assert SECRET_QUESTION not in blob, "bob can read what alice ASKED"


def test_observability_is_operator_only():
    _real_login(True, operators=OPERATOR)
    for path in OBSERVABILITY:
        r = client.get(path, cookies=_cookie(BOB))
        assert r.status_code == 403, f"{path} answered an ordinary user with {r.status_code}"
        assert OPERATOR not in r.text, f"{path} disclosed the operator list in its refusal"


def test_ordinary_user_keeps_the_routes_the_canvas_needs():
    """The regression this file exists to prevent a SECOND time. The first cut of #549 gated
    these too, and the canvas ACL picker fell back to "directory not loaded yet" — caught only
    by driving the real browser, never by the API drive."""
    _real_login(True, operators=OPERATOR)
    for path in ORDINARY_USER_ROUTES:
        r = client.get(path, cookies=_cookie(BOB))
        assert r.status_code == 200, f"{path} is needed by ordinary users, got {r.status_code}"


GLOBAL_ADMIN_ROLE = "d99e09e2-415c-4a39-acf7-66d7e0a5bc0b"


def test_bob_cannot_learn_who_holds_a_directory_role():
    """#881, the disclosure itself rather than the status code.

    #872 moved group expansion to getMemberObjects so a document ACL'd to a DIRECTORY ROLE
    becomes readable by the people holding it - correct, and the reason the owner could read
    his own SharePoint library at all. The cost landed on a route #872 never touched:
    /admin/identities reports every registered principal with a member_count and lists each
    user's principals, so once roles became principals the response named the tenant's
    Global Administrators and said how few of them there are. That is a target list.

    The positive control is the point of this test. Asserting only that bob cannot see the
    role would pass just as well against an EMPTY directory - the #575b round-1 mistake, in
    this file's own neighbourhood - so the operator's own GET has to find the role in the same
    breath. One half proves the seed is real; the other proves the gate refuses it. Deleting
    the `dependencies=[Depends(_require_operator)]` on /admin/identities turns the first
    assertion red; a seeding mistake turns the second one red. Neither can rescue the other."""
    _real_login(True, operators=OPERATOR)
    ident = app_module._edition.identity
    try:
        ident.set_user_groups(ALICE, [GLOBAL_ADMIN_ROLE])

        seen_by_operator = json.dumps(
            client.get("/admin/identities", cookies=_cookie(OPERATOR)).json())
        assert GLOBAL_ADMIN_ROLE in seen_by_operator, (
            "positive control failed: the role is not in the directory, so a bob who cannot "
            "see it proves nothing about the gate")

        r = client.get("/admin/identities", cookies=_cookie(BOB))
        assert r.status_code == 403, \
            f"an ordinary user read the directory-role registry ({r.status_code})"
        assert GLOBAL_ADMIN_ROLE not in r.text, "the refusal itself named the role"
        assert ALICE not in r.text, "bob learned WHO holds it"
    finally:
        ident.set_user_groups(ALICE, [])
        _real_login(False)


def test_alice_still_sees_her_own_document():
    """The fix must not cost the owner sight of her own corpus — that IS the product."""
    _real_login(True, operators=OPERATOR)
    _seed_alices_private_document()
    blob = json.dumps(client.get("/admin/documents", cookies=_cookie(ALICE)).json())
    assert SECRET_TITLE in blob, "alice lost sight of the document she just uploaded"


def test_operator_still_sees_everything():
    _real_login(True, operators=OPERATOR)
    _seed_alices_private_document()
    assert SECRET_TITLE in json.dumps(
        client.get("/admin/documents", cookies=_cookie(OPERATOR)).json())
    for path in OBSERVABILITY:
        r = client.get(path, cookies=_cookie(OPERATOR))
        assert r.status_code == 200, f"{path} refused the operator ({r.status_code})"


def test_dev_rig_is_unchanged():
    """No real login = somebody's own machine (ADR 0011 s3). Everything stays open, or we
    have broken every local rig and every hermetic test in the suite."""
    _real_login(False)
    _seed_alices_private_document()
    for path in OBSERVABILITY + ORDINARY_USER_ROUTES + ("/admin/documents",):
        r = client.get(path, headers={"X-DBSearch-User": ALICE})   # the dev rig's own identity seam
        assert r.status_code == 200, f"{path} broke on a dev rig ({r.status_code})"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
    _real_login(False)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
