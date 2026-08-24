"""#575 (second half) - the upload audience picker: Only me / My organization.

#539 made an upload private to whoever ingested it. #538/ADR 0017 added sharing with a named
person after the fact. Missing until now: a one-step way to publish a document so EVERYONE in
the uploader's organization can read it, for the case an HR policy is meant for the whole
tenant rather than a private document plus a chain of individual shares.

`/admin/upload` grows one new form field, `audience`, with exactly two values:
  - "" / "private" (the existing #539 default) - acl = [uploader], unchanged.
  - "org" - acl = [uploader, "tenant:<the session's VERIFIED tid>"], 400 when the session
    carries no tid (Google / local-account sessions, ADR 0018, have no organization to
    publish into) or when the bound identity port cannot hold a tenant principal at all
    (review Finding 4 - the cloud EntraIdentity port, which has no `set_user_groups`).

"Specific people" stays out of this endpoint on purpose - it is not a third audience value
here, it is the existing per-document Share flow (ADR 0017 grants), reached after upload.

THE LAW 2 STAKE: the tenant principal that ends up on the ACL must come from the server-side,
already-verified session `tid` set at `/auth/callback` - never from anything the client
posts. `test_org_principal_ignores_every_plausible_client_supplied_spoof` is the test that
would fail if that were ever true.

Code review (260807) on the first version of this file found four Important issues, fixed
here:
  1. A synthetic `tenant:<tid>` principal must never reach the Graph names lookup (not a
     GUID, 400s the whole batch, silently zeroing the ACL-picker directory for everyone).
  2. The production registration line (`/auth/callback`) was untested - only hand-simulated.
  3. `/admin/identities`, ungated, must not leak `tenant:<tid>` principals.
  4. An identity port with no `set_user_groups` (the cloud shape) must refuse `audience=org`
     honestly instead of succeeding and lying about who can read the result.
`test_the_real_callback_registers_the_tenant_principal_and_keeps_it_out_of_the_names_lookup`
covers 1 and 2 together by driving the actual route. `test_admin_identities_never_leaks_a_
tenant_principal` covers 3. `test_org_upload_is_refused_when_the_identity_port_cannot_hold_a_
tenant_principal` covers 4.

    PYTHONPATH=src python3 tests/selftest_575b_audience_picker.py
"""
import io
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from _upload import settle  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import app as app_module  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)

ALICE = "aaaaaaaa-0000-0000-0000-000000000575"
BOB = "bbbbbbbb-0000-0000-0000-000000000575"
CAROL = "cccccccc-0000-0000-0000-000000000575"
DAVE = "dddddddd-0000-0000-0000-000000000575"
_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")

BODY = b"The whistleblower hotline is +1-555-0100, staffed 24/7 by an external ombuds firm."


def _real_login(on: bool):
    for k in _VARS:
        os.environ.pop(k, None)
    if on:
        os.environ.update({"AUTH_TENANT_ID": "tid-1", "AUTH_CLIENT_ID": "cid",
                           "AUTH_CLIENT_SECRET": "sec"})


def _cookie(oid: str, tid: str) -> dict:
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": tid, "exp": int(time.time()) + 3600})}


def _upload(oid, tid, name, audience=None, extra=None, headers=None):
    """Post the multipart form exactly as the browser does."""
    data = {"title": name}
    if audience is not None:
        data["audience"] = audience
    if extra:
        data.update(extra)
    files = {"file": (name, io.BytesIO(BODY), "text/plain")}
    return client.post("/admin/upload", cookies=_cookie(oid, tid), data=data, files=files,
                       headers=headers or {})


def test_org_upload_is_readable_by_a_same_tenant_colleague():
    """alice (tid-1) uploads with audience="org"; the response ACL carries the tenant
    principal minted from her own verified session tid. bob's session (same tid-1) - after
    simulating his sign-in group registration exactly as /auth/callback would have done it -
    CAN search the document, even though he never touched it."""
    _real_login(True)
    try:
        r = _upload(ALICE, "tid-1", "whistleblower-hotline-1.txt", audience="org")
        assert r.status_code == 202, f"org upload refused: {r.status_code} {r.text[:200]}"
        assert r.json()["acl"] == sorted([ALICE, "tenant:tid-1"]), \
            f"unexpected acl: {r.json()['acl']}"
        job = settle(client, r, cookies=_cookie(ALICE, "tid-1"))
        assert job["status"] == "succeeded", job

        # Same in-memory mechanism real Entra group registration rides (/auth/callback) - bob
        # signs in and his session picks up the tenant principal for tid-1.
        app_module._edition.identity.set_user_groups(BOB, ["tenant:tid-1"])
        answer = client.post("/search", cookies=_cookie(BOB, "tid-1"),
                             json={"question": "whistleblower hotline number"})
        assert answer.status_code == 200
        blob = json.dumps(answer.json())
        assert "555-0100" in blob, f"same-tenant colleague cannot read the org upload: {blob[:300]}"
    finally:
        app_module._edition.identity.set_user_groups(BOB, [])
        _real_login(False)


def test_org_upload_is_invisible_to_a_foreign_tenant():
    """carol (tid-2, groups registered as ["tenant:tid-2"]) does NOT see alice's tid-1 org
    upload - both layers disagree with her: she lands in a different document PARTITION
    (ADR 0012) and her expanded principals don't overlap the doc's ACL either."""
    _real_login(True)
    try:
        r = _upload(ALICE, "tid-1", "whistleblower-hotline-2.txt", audience="org")
        assert r.status_code == 202
        job = settle(client, r, cookies=_cookie(ALICE, "tid-1"))
        assert job["status"] == "succeeded", job   # indexed, so carol's blindness below is real

        app_module._edition.identity.set_user_groups(CAROL, ["tenant:tid-2"])
        blob = json.dumps(client.post("/search", cookies=_cookie(CAROL, "tid-2"),
                                      json={"question": "whistleblower hotline number"}).json())
        assert "555-0100" not in blob, "a foreign tenant read an org-shared document"
        listing = json.dumps(client.get("/admin/documents", cookies=_cookie(CAROL, "tid-2")).json())
        assert "whistleblower-hotline-2.txt" not in listing, \
            "a foreign tenant can see an org-shared document in their listing"
    finally:
        app_module._edition.identity.set_user_groups(CAROL, [])
        _real_login(False)


def test_private_stays_the_default():
    """audience omitted -> acl == [uploader] exactly - no tenant principal creep just
    because the caller happens to have a tid on their session."""
    _real_login(True)
    try:
        r = _upload(ALICE, "tid-1", "whistleblower-hotline-3.txt")
        assert r.status_code == 202
        assert r.json()["acl"] == [ALICE], f"expected private-to-uploader, got {r.json()['acl']}"
    finally:
        _real_login(False)


def test_org_audience_without_a_tid_is_refused():
    """A local-account session (tid "") posting audience="org" -> 400, naming the reason,
    and nothing was ingested."""
    _real_login(True)
    try:
        r = _upload(ALICE, "", "whistleblower-hotline-4.txt", audience="org")
        assert r.status_code == 400, f"expected 400 for a no-tid org upload, got {r.status_code}"
        detail = r.json().get("detail", "")
        assert "organization" in detail.lower(), f"400 detail does not name the reason: {detail}"

        listing = json.dumps(client.get("/admin/documents", cookies=_cookie(ALICE, "tid-1")).json())
        assert "whistleblower-hotline-4.txt" not in listing, \
            "the refused org upload was ingested anyway"
    finally:
        _real_login(False)


def test_org_principal_ignores_every_plausible_client_supplied_spoof():
    """LAW 2, the property that actually matters. A single spoofed `tid` form field is not
    exhaustive proof of it: `/admin/upload` declares no `tid` parameter, so FastAPI silently
    discards that one field, and the same test would keep passing even if the real code read
    the tenant from a header, or from a field named `tenant` instead. This throws every
    plausible client-side vector in ONE request against alice's real tid-1 session - a `tid`
    form field, a `tenant_id` form field, a `tenant` form field, an `X-Tenant-Id` header, an
    `X-DBSearch-Tenant` header, and an `acl` field naming the attacker's tenant directly (the
    one field a caller CAN legitimately set - #539 - so this also proves audience=org does
    not merge it in) - and asserts only the real session's tenant principal ever appears."""
    _real_login(True)
    try:
        data = {"title": "spoof-test.txt", "audience": "org",
               "tid": "tid-999-attacker", "tenant_id": "tid-999-attacker",
               "tenant": "tid-999-attacker", "acl": "tenant:tid-999-attacker"}
        files = {"file": ("spoof-test.txt", io.BytesIO(BODY), "text/plain")}
        r = client.post("/admin/upload", cookies=_cookie(ALICE, "tid-1"), data=data, files=files,
                        headers={"X-Tenant-Id": "tid-999-attacker",
                                 "X-DBSearch-Tenant": "tid-999-attacker"})
        assert r.status_code == 202, f"unexpected refusal: {r.status_code} {r.text[:200]}"
        assert r.json()["acl"] == sorted([ALICE, "tenant:tid-1"]), \
            f"a client-supplied spoof vector influenced the ACL: {r.json()['acl']}"
        assert "tenant:tid-999-attacker" not in r.json()["acl"], \
            "the spoofed tenant leaked into the document ACL"
    finally:
        _real_login(False)


def test_has_org_reflects_whether_the_session_carries_a_tid():
    """/auth/me's has_org is the ONLY thing that decides whether the UI offers "My
    organization" at all - untested before, per review Finding 2."""
    _real_login(True)
    try:
        with_tid = client.get("/auth/me", cookies=_cookie(ALICE, "tid-1")).json()
        assert with_tid["has_org"] is True, with_tid

        without_tid = client.get("/auth/me", cookies=_cookie(ALICE, "")).json()
        assert without_tid["has_org"] is False, without_tid
    finally:
        _real_login(False)


def test_the_real_callback_registers_the_tenant_principal_and_keeps_it_out_of_the_names_lookup():
    """Review Finding 2: every test above hand-injects `tenant:<tid>` via `set_user_groups`,
    which proves ENFORCEMENT but says nothing about the one production line meant to populate
    it. Drive the actual `/auth/callback` route, mocked the same way
    `tests/selftest_doc_plane_tenant_gate.py::test_the_real_callback_mints_a_session_that_
    can_ingest` already does it, and assert the tenant principal really lands in
    `expand_groups` for the signed-in oid.

    Review Finding 1, same test: Graph's `directoryObjects/getByIds` requires real GUIDs. A
    synthetic `tenant:<tid>` principal is not one, and Graph 400s the WHOLE batch on a single
    bad id - silently, since `fetch_principal_facts` swallows every exception into `{}`. Left
    unfiltered, the very first sign-in after this deploy would have degraded the #258 ACL
    picker to "paste a raw oid" for every user on the tenant. Assert the synthetic principal
    never reaches that lookup."""
    _real_login(True)
    saved = (app_module._state_ok, user_auth.exchange_code,
             user_auth.fetch_member_principals, user_auth.fetch_principal_facts)
    app_module._state_ok = lambda request, params: True
    user_auth.exchange_code = lambda code: {"oid": DAVE, "name": "Dave",
                                            "email": "dave@x.test", "tid": "tid-1"}
    user_auth.fetch_member_principals = lambda tid, oid: []
    seen_names_oids = []

    def fake_names(tid, oids):
        seen_names_oids.extend(oids)
        return {}
    user_auth.fetch_principal_facts = fake_names
    try:
        r = client.get("/auth/callback?code=c&state=s", follow_redirects=False)
        assert r.status_code == 302, (r.status_code, r.text)
        token = r.cookies.get(user_auth.COOKIE)
        assert token, "the callback set no session cookie"

        principals = app_module._edition.identity.expand_groups(DAVE)
        assert "tenant:tid-1" in principals, \
            f"the real callback did not register the tenant principal: {principals}"
        assert not any(str(o).startswith("tenant:") for o in seen_names_oids), \
            f"a synthetic tenant principal reached the Graph names lookup: {seen_names_oids}"
    finally:
        (app_module._state_ok, user_auth.exchange_code,
         user_auth.fetch_member_principals, user_auth.fetch_principal_facts) = saved
        app_module._edition.identity.set_user_groups(DAVE, [])
        client.cookies.clear()
        _real_login(False)


def test_admin_identities_never_leaks_a_tenant_principal():
    """Review Finding 3 (round 2 - the round-1 version of this test was vacuous). The ACL on
    alice's org upload alone puts nothing in the DIRECTORY: `list_principals()` reads the
    identity port's own `_user_groups` registrations (the same shape `/auth/callback` writes
    at sign-in), not document ACLs - so without a real group registration there is no
    `tenant:` string anywhere for the filter to have ever needed to remove, and this test
    would have passed with EITHER filter line deleted, which is exactly what the round-1
    review caught by mutation.

    Fixed by registering bob's own session with the tenant principal (the same in-memory
    mechanism `/auth/callback` uses), the same way the OTHER tests in this file already prove
    same-tenant visibility - so the directory genuinely contains `tenant:tid-1` before the
    assertion runs, and the GET has something real to fail to filter.

    #881 ROUND 3: the route is now operator-gated, and that nearly made this test vacuous a
    SECOND time in the same way. `_real_login(True)` sets no DBSEARCH_OPERATOR_OIDS, so under
    a real login nobody is an operator and bob's GET began answering 403 - whose body contains
    no "tenant:" either, so the assertion below would have passed with BOTH filter lines
    deleted. Bob is made an operator here for exactly that reason: the filter is only under
    test when the request actually reaches the handler. The 200 assertion is the precondition
    that says so out loud, rather than leaving it to be inferred from a blob that is empty for
    the wrong reason. The GATE is a different property with its own guard, in
    selftest_549_admin_metadata_gate.py."""
    _real_login(True)
    os.environ["DBSEARCH_OPERATOR_OIDS"] = BOB
    try:
        r = _upload(ALICE, "tid-1", "whistleblower-hotline-6.txt", audience="org")
        assert r.status_code == 202
        job = settle(client, r, cookies=_cookie(ALICE, "tid-1"))
        assert job["status"] == "succeeded", job

        app_module._edition.identity.set_user_groups(BOB, ["tenant:tid-1"])
        got = client.get("/admin/identities", cookies=_cookie(BOB, "tid-1"))
        assert got.status_code == 200, (
            "the filter cannot be under test unless the request REACHES the handler; "
            f"got {got.status_code} - if this is 403, bob lost his operator grant")
        blob = json.dumps(got.json())
        assert "tenant:" in json.dumps(
            app_module._edition.identity.list_principals().users), \
            "precondition: the directory must actually CONTAIN a tenant principal to filter"
        assert "tenant:" not in blob, f"a tenant principal leaked through /admin/identities: {blob[:400]}"
    finally:
        app_module._edition.identity.set_user_groups(BOB, [])
        _real_login(False)


class _NoGroupCapabilityIdentity:
    """A double shaped like the cloud EntraIdentity port: expand_groups only, no
    set_user_groups - the one capability _identity_can_hold_tenant_principal checks for."""
    def expand_groups(self, oid):
        return [oid]


def _wrapped_no_group_capability_identity():
    """#575 review round 2: the production identity port is never this double bare - it is
    always wrapped in GrantAwareIdentity (ADR 0017, #538), which forwards unknown attributes
    (including set_user_groups) via __getattr__. A test that swaps in the bare double proves
    nothing about whether the capability check sees THROUGH that wrapper; wrapping it here is
    what actually exercises the production shape."""
    from dbsearch.api.grants import GrantAwareIdentity, GrantRegistry
    return GrantAwareIdentity(_NoGroupCapabilityIdentity(), GrantRegistry())


def test_org_upload_is_refused_when_the_identity_port_cannot_hold_a_tenant_principal():
    """Review Finding 4: an identity port with no `set_user_groups` (the shape the cloud
    `EntraIdentity` port has - `expand_groups` is Graph-only) can NEVER be made to expand
    `tenant:<tid>` for anyone. Succeeding here would return 200 and tell the uploader their
    document is org-readable while it structurally cannot be - under-visible is the safe
    direction, but a dishonest success message is worse than an honest refusal."""
    _real_login(True)
    saved_identity = app_module._edition.identity
    app_module._edition.identity = _wrapped_no_group_capability_identity()
    try:
        r = _upload(ALICE, "tid-1", "no-group-support.txt", audience="org")
        assert r.status_code == 400, (
            f"expected 400 when the identity port cannot hold a tenant principal, "
            f"got {r.status_code} {r.text[:200]}")
    finally:
        app_module._edition.identity = saved_identity
        _real_login(False)


def test_has_org_is_false_when_the_identity_port_cannot_hold_a_tenant_principal():
    """New finding (round 2): has_org used to be `bool(tid)` alone, so on a cloud/Entra
    deployment (an identity port with no set_user_groups) the canvas would still render "My
    organization" for every signed-in user while every selection 400s underneath it - the
    #551 "tile that always fails" trap, made worse because the file is now rejected outright
    rather than ingested privately. has_org must agree with the same capability predicate the
    400 in /admin/upload uses, on the SAME (GrantAwareIdentity-wrapped) production shape."""
    _real_login(True)
    saved_identity = app_module._edition.identity
    app_module._edition.identity = _wrapped_no_group_capability_identity()
    try:
        me = client.get("/auth/me", cookies=_cookie(ALICE, "tid-1")).json()
        assert me["has_org"] is False, (
            f"has_org offered 'My organization' on an identity backend that cannot hold "
            f"the tenant principal - the option would always 400: {me}")

        r = _upload(ALICE, "tid-1", "has-org-disagreement.txt", audience="org")
        assert r.status_code == 400, (
            "has_org said no but the upload still succeeded - the UI signal and the "
            f"server enforcement disagree: {r.status_code} {r.text[:200]}")
    finally:
        app_module._edition.identity = saved_identity
        _real_login(False)


ERIN = "eeeeeeee-0000-0000-0000-000000000575"


def test_a_restart_re_registers_the_tenant_principal_from_the_session():
    """FINAL WHOLE-BRANCH REVIEW, Fix 3. `_resolve_groups_if_unknown` (app.py) is the THIRD
    place groups get registered - the restart/scale-out repair path from #266 - and it was
    the only one `_with_tenant_principal` was never applied to.

    Why that is not a corner case: a restart does NOT sign anyone out (the session cookie
    lives 8 hours), so after ANY deploy every already-signed-in user hits this path instead
    of `/auth/callback`. It registered them WITHOUT `tenant:<tid>` and `knows_groups` then
    cached the omission for the life of the process - so every "My organization" document
    went invisible to every existing user until their cookie expired. Fail-closed, and
    therefore silent: it looks exactly like "you have no access", which is the failure #266
    exists to prevent one layer up.

    This test IS the restart: ERIN has a valid signed session (with her verified tid) and the
    identity port has never heard of her, which is precisely the state a fresh worker is in."""
    _real_login(True)
    ident = app_module._edition.identity
    assert not ident.knows_groups(ERIN), "the fixture is stale - erin must be unknown"

    saved = (user_auth.fetch_member_principals, user_auth.fetch_principal_facts)
    user_auth.fetch_member_principals = lambda tid, oid: ["group-erin"]
    seen_names_oids = []

    def fake_names(tid, oids):
        seen_names_oids.extend(oids)
        return {}
    user_auth.fetch_principal_facts = fake_names
    try:
        # Any endpoint behind `current_user` - the #266 repair hook is at that ONE chokepoint
        # on purpose, so this is the first request of the day for every returning user.
        r = client.post("/search", cookies=_cookie(ERIN, "tid-1"),
                        json={"question": "what is the whistleblower hotline"})
        assert r.status_code == 200, (r.status_code, r.text[:200])

        principals = ident.expand_groups(ERIN)
        assert "tenant:tid-1" in principals, (
            "the restart repair path registered the user WITHOUT their tenant principal - "
            f"every org-audience document is invisible to them until the cookie expires: "
            f"{principals}")
        assert "group-erin" in principals, (
            f"the real group registration regressed while adding the principal: {principals}")
        # The synthetic principal must stay out of the Graph names lookup here too - the same
        # #575 Finding 1 that 400s the whole getByIds batch and zeroes the ACL picker.
        assert not any(str(o).startswith("tenant:") for o in seen_names_oids), (
            f"a synthetic tenant principal reached the Graph names lookup: {seen_names_oids}")
    finally:
        (user_auth.fetch_member_principals, user_auth.fetch_principal_facts) = saved
        ident.set_user_groups(ERIN, [])
        client.cookies.clear()
        _real_login(False)


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
