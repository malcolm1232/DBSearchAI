"""#538 / ADR 0017 — sharing a document with a named person who signs in.

#539 made an upload private to its uploader, which is the safe half. This is the other half:
letting one named colleague read it, without a secret link and without anyone becoming an
operator.

The design under test (ADR 0017): a grant mints a `grant:<id>` principal, puts it on the
document's ACL once, and hands it to the grantee's expansion while the grant is live. The
properties that fall out of that shape are what these tests pin:

  - revocation is immediate, and needs no ACL rewrite
  - expiry is evaluated per request, so there is no sweeper and no staleness window
  - a share cannot be re-shared
  - a grant copies access; it never escalates, and never confers the metadata plane (#549)

    PYTHONPATH=src python3 tests/selftest_538_document_grants.py
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import ACCOUNTS, app  # noqa: E402
from dbsearch.server.app import _edition  # noqa: E402

client = TestClient(app)

ALICE = "aaaaaaaa-0000-0000-0000-000000000538"
BOB = "bbbbbbbb-0000-0000-0000-000000000538"
CAROL = "cccccccc-0000-0000-0000-000000000538"
_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")
SECRET = "The Hamburg plant redundancy list names 14 staff in the actuator line."


def _real_login(on=True):
    for k in _VARS:
        os.environ.pop(k, None)
    if on:
        os.environ.update({"AUTH_TENANT_ID": "tid-1", "AUTH_CLIENT_ID": "cid",
                           "AUTH_CLIENT_SECRET": "sec"})


def _cookie(oid):
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": "tid-1", "exp": int(time.time()) + 3600})}


def _alices_doc(doc_id):
    r = client.post("/ingest", cookies=_cookie(ALICE), json={
        "external_id": doc_id, "title": "Q3 Redundancy List", "text": SECRET,
        "acl": [ALICE], "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, r.text[:200]
    return doc_id


def _asks(oid, q="who is on the redundancy list"):
    return json.dumps(client.post("/search", cookies=_cookie(oid), json={"question": q}).json())


def _asks_as_account(oid, q="who is on the redundancy list"):
    """Ask as a NON-Entra account: no tid in the session, so `resolve_tenant` gives it the
    ADR 0018 `acct:<oid>` partition. `_cookie` hardcodes tid="tid-1" and would silently put
    the caller in the home tenant, which is the very co-tenanting that hid #582."""
    return json.dumps(client.post("/search", cookies=_acct_cookie(oid),
                                  json={"question": q}).json())


def test_before_a_grant_bob_sees_nothing():
    _real_login()
    _alices_doc("doc-538-a")
    assert SECRET not in _asks(BOB), "bob could already read it — the test proves nothing"


def test_a_grant_lets_the_named_person_read_it():
    _real_login()
    doc = _alices_doc("doc-538-b")
    r = client.post(f"/documents/{doc}/grants", cookies=_cookie(ALICE),
                    json={"grantee_oid": BOB})
    assert r.status_code == 200, f"grant refused: {r.status_code} {r.text[:200]}"
    assert SECRET in _asks(BOB), "bob still cannot read a document shared with him"


def test_revocation_is_immediate():
    _real_login()
    doc = _alices_doc("doc-538-c")
    gid = client.post(f"/documents/{doc}/grants", cookies=_cookie(ALICE),
                      json={"grantee_oid": BOB}).json()["grant_id"]
    assert SECRET in _asks(BOB)
    assert client.delete(f"/grants/{gid}", cookies=_cookie(ALICE)).status_code == 200
    assert SECRET not in _asks(BOB), "a revoked grantee can still read the document"


def test_the_sharer_can_revoke_their_own_share():
    """The gap #538's scoping found in the api-key design: a token bound to the SHARE
    principal left the sharer failing every ownership check on their own share."""
    _real_login()
    doc = _alices_doc("doc-538-d")
    gid = client.post(f"/documents/{doc}/grants", cookies=_cookie(ALICE),
                      json={"grantee_oid": BOB}).json()["grant_id"]
    assert client.delete(f"/grants/{gid}", cookies=_cookie(ALICE)).status_code == 200


def test_a_stranger_cannot_revoke_someone_elses_grant():
    _real_login()
    doc = _alices_doc("doc-538-e")
    gid = client.post(f"/documents/{doc}/grants", cookies=_cookie(ALICE),
                      json={"grantee_oid": BOB}).json()["grant_id"]
    assert client.delete(f"/grants/{gid}", cookies=_cookie(CAROL)).status_code == 404
    assert SECRET in _asks(BOB), "carol's failed revoke still cut bob off"


def test_an_expired_grant_stops_working_with_no_sweeper():
    """Expiry is evaluated during principal expansion, so it applies on the next request —
    nothing has to run in the background for a lapsed share to actually lapse."""
    _real_login()
    doc = _alices_doc("doc-538-f")
    client.post(f"/documents/{doc}/grants", cookies=_cookie(ALICE),
                json={"grantee_oid": BOB, "expires_in_days": 1})
    assert SECRET in _asks(BOB)
    reg = _edition.grant_registry
    with reg._lock:                      # simulate the clock passing, not the sweeper running
        for gid, g in list(reg._by_id.items()):
            reg._by_id[gid] = type(g)(**{**g.__dict__,
                                         "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    assert SECRET not in _asks(BOB), "an expired grant still reads"


def test_a_share_cannot_be_re_shared():
    _real_login()
    doc = _alices_doc("doc-538-g")
    client.post(f"/documents/{doc}/grants", cookies=_cookie(ALICE), json={"grantee_oid": BOB})
    r = client.post(f"/documents/{doc}/grants", cookies=_cookie(BOB), json={"grantee_oid": CAROL})
    assert r.status_code == 404, f"bob re-shared a document he was merely given ({r.status_code})"
    assert SECRET not in _asks(CAROL), "carol got access through a re-share"


def test_a_stranger_cannot_learn_the_document_exists():
    """The refusal must be 404, not 403 — a 403 confirms existence to someone who may not
    see it, which is the #549 metadata leak through a new door."""
    _real_login()
    doc = _alices_doc("doc-538-h")
    assert client.post(f"/documents/{doc}/grants", cookies=_cookie(CAROL),
                       json={"grantee_oid": CAROL}).status_code == 404
    assert client.get(f"/documents/{doc}/grants", cookies=_cookie(CAROL)).status_code == 404


def test_granting_a_document_that_does_not_exist_says_so():
    _real_login()
    assert client.post("/documents/no-such-doc/grants", cookies=_cookie(ALICE),
                       json={"grantee_oid": BOB}).status_code == 404


def test_a_grant_does_not_confer_the_metadata_plane():
    """A grantee reads the document. They do not become an operator (#549)."""
    _real_login()
    doc = _alices_doc("doc-538-i")
    client.post(f"/documents/{doc}/grants", cookies=_cookie(ALICE), json={"grantee_oid": BOB})
    assert client.get("/admin/audit", cookies=_cookie(BOB)).status_code == 403


# ---- FINAL WHOLE-BRANCH REVIEW, Fix 2: a cross-partition share must refuse, not 200 ------
#
# THE SEAM every per-task review missed: a grant puts `grant:<id>` on the document's ACL
# inside the GRANTOR's partition, but the grantee retrieves through THEIR OWN partition
# (resolve_tenant), and the partition filter runs BEFORE the ACL overlap. Every test above
# puts both parties in the home tenant (`_cookie` hardcodes tid="tid-1"), which is exactly
# why the whole class went unseen: those are the only shares that have ever worked.
#
# Owner's ruling: refuse honestly now; real cross-partition retrieval is CARD #582.
LOCAL = "acct_538aaaaaaaaaaaaaaaaaaaaaaaaaaaaa"      # a local email/password account id
GOOGLE = "colleague@example.com"                     # a Google account id IS the email


def _acct_cookie(oid):
    """A verified session with NO Entra tid - Google or local email/password. ADR 0018 puts
    it in its own private `acct:<oid>` partition."""
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": "", "exp": int(time.time()) + 3600})}


def test_a_same_partition_share_still_succeeds():
    """The guard must not cost the case that works. Two Entra colleagues in the home tenant:
    200, and the grantee really reads the document."""
    _real_login()
    doc = _alices_doc("doc-538-j")
    r = client.post(f"/documents/{doc}/grants", cookies=_cookie(ALICE),
                    json={"grantee_oid": BOB})
    assert r.status_code == 200, f"a same-tenant share was refused: {r.status_code} {r.text[:200]}"
    assert SECRET in _asks(BOB), "a same-tenant share stopped actually sharing"


def test_a_share_out_of_a_private_account_workspace_now_reaches_another_account():
    """FLIPPED BY #582 / ADR 0019 D1, and the flip is stated rather than left implicit.

    This test used to pin "a share out of an `acct:` workspace is REFUSED", and that was
    correct at the time: the grantee retrieved through their own partition, the partition
    filter ran before the ACL overlap, so the grant was inert by construction and a 400 was
    the honest answer. ADR 0019 gave the grantee a per-document doorway into the grantor's
    partition, so the case the refusal existed for now WORKS - and continuing to refuse it
    would be the bug.

    Deliberately asserts RETRIEVAL, not a 200. A status code was exactly what the old
    behaviour got right and the product got wrong.
    """
    _real_login()
    ACCOUNTS.resolve("local", "local538@x.com", preferred_account_id=LOCAL,
                     email="local538@x.com")
    ACCOUNTS.resolve("entra", BOB, preferred_account_id=BOB, tid="tid-1", email="bob538@x.com")
    # A marker unique to THIS document. `SECRET` is shared by every document in this file
    # and bob already holds grants from earlier tests, so asserting on it would make
    # "bob starts with no access" depend on test order rather than on the share.
    marker = "The Bremen calibration jig is logged under asset tag 7741."
    r = client.post("/ingest", cookies=_acct_cookie(LOCAL), json={
        "external_id": "doc-538-k", "title": "Private note", "text": marker,
        "acl": [LOCAL], "uri": "upload://doc-538-k.txt"})
    assert r.status_code == 200, f"the local account could not ingest at all: {r.text[:200]}"
    q = "Bremen calibration jig asset tag"
    assert marker not in _asks(BOB, q), "bob must start with no access"

    r = client.post("/documents/doc-538-k/grants", cookies=_acct_cookie(LOCAL),
                    json={"grantee_oid": BOB})
    assert r.status_code == 200, f"a share that now works was refused: {r.text[:200]}"
    assert marker in _asks(BOB, q), "the share returned 200 and shared nothing"


def test_a_share_from_the_org_to_a_non_microsoft_account_now_works():
    """The other direction, flipped by the same ADR 0019 D1 decision.

    An Entra user in the home tenant shares with a Google or local account. Those
    identities have no verified `tid` and so hold an `acct:` partition - which used to mean
    they could never read the home tenant's corpus, and the share was refused on the SHAPE
    of the identifier. The doorway removes that limit, and the shape heuristic with it.

    What is still refused is a FOREIGN Entra tenant, in either direction; that is covered
    by tests/selftest_582_share_across_partitions.py.
    """
    _real_login()
    ACCOUNTS.resolve("google", "google538@x.com", preferred_account_id=GOOGLE,
                     email="google538@x.com")
    ACCOUNTS.resolve("local", "local538@x.com", preferred_account_id=LOCAL,
                     email="local538@x.com")
    doc = _alices_doc("doc-538-l")
    for grantee, who in ((GOOGLE, "a Google account"), (LOCAL, "a local account")):
        r = client.post(f"/documents/{doc}/grants", cookies=_cookie(ALICE),
                        json={"grantee_oid": grantee})
        assert r.status_code == 200, (
            f"sharing with {who} was refused: {r.status_code} {r.text[:200]}")
        assert SECRET in _asks_as_account(grantee), (
            f"{who} got a grant but still retrieves nothing")

    # The document is untouched: the share must not have damaged the owner's own access.
    assert SECRET in _asks(ALICE), "the share damaged the owner's own access"


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
