"""#582 - a share now crosses an account boundary, and a foreign tenant still cannot.

THE SHAPE THAT WAS MISSING: every share test written before this one put both parties in
the SAME partition (the home tenant), which is exactly why a whole class of broken shares
went unseen - the grant returned 200, the grantee retrieved nothing, and no test looked.
So every test here puts the two people in DIFFERENT partitions.

Covers, per ADR 0019:
  D1  acct: <-> acct: and acct: <-> home now work; a foreign Entra tenant is refused in
      BOTH directions, and an account whose tid was never recorded fails closed
  D3  the doorway opens ONE document, never the partition holding it, and the ACL still
      decides what is returned
  D4  the grantee gets full READ parity - ask, listing, segments, download - and no write
  D5  the grantor names a person by email, not by an opaque account id

    PYTHONPATH=src python3 tests/selftest_582_share_across_partitions.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import ACCOUNTS, app, _edition  # noqa: E402

client = TestClient(app)

HOME_TID = "tid-home"
ALICE = "acct_alice"            # local account -> acct:acct_alice
BOB = "acct_bob"                # local account -> acct:acct_bob
CAROL = "acct_carol"            # local account -> acct:acct_carol, a bystander
HOME_USER = "33333333-3333-3333-3333-333333333333"    # home-tenant Entra
FOREIGNER = "44444444-4444-4444-4444-444444444444"    # foreign-tenant Entra
UNRECORDED = "55555555-5555-5555-5555-555555555555"   # Entra, tid never recorded

_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET")


def _secret(doc_id: str) -> str:
    """Body text unique to ONE document.

    The index lives for the WHOLE run, so a shared sentence would make "bob starts with no
    access" depend on whether an EARLIER test in this file had already shared some other
    document carrying the same words. That is the run-long shared-state trap that made an
    earlier #593 test vacuous; the first version of this file walked straight into it.
    """
    return f"The Hamburg actuator line in {doc_id} runs at 62 percent of rated throughput."


def _real_login():
    for k in _VARS:
        os.environ.pop(k, None)
    os.environ.update({"AUTH_TENANT_ID": HOME_TID, "AUTH_CLIENT_ID": "cid",
                       "AUTH_CLIENT_SECRET": "sec"})


def _cookie(oid: str, tid: str = "") -> dict:
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": tid, "exp": int(time.time()) + 3600})}


def _seed_accounts():
    """Recorded identities, as a real sign-in would leave them."""
    ACCOUNTS.resolve("local", "alice@x.com", preferred_account_id=ALICE, email="alice@x.com")
    ACCOUNTS.resolve("local", "bob@x.com", preferred_account_id=BOB, email="bob@x.com")
    ACCOUNTS.resolve("local", "carol@x.com", preferred_account_id=CAROL, email="carol@x.com")
    ACCOUNTS.resolve("entra", HOME_USER, preferred_account_id=HOME_USER,
                     tid=HOME_TID, email="home@corp.com")
    ACCOUNTS.resolve("entra", FOREIGNER, preferred_account_id=FOREIGNER,
                     tid="tid-somewhere-else", email="them@other.com")
    ACCOUNTS.resolve("entra", UNRECORDED, preferred_account_id=UNRECORDED,
                     email="quiet@corp.com")            # NO tid on purpose


def _ingest(doc_id: str, owner: str, tid: str = "") -> str:
    r = client.post("/ingest", cookies=_cookie(owner, tid), json={
        "external_id": doc_id, "title": f"title of {doc_id}", "text": _secret(doc_id),
        "acl": [owner], "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, f"seed ingest failed: {r.status_code} {r.text[:200]}"
    return doc_id


def _share(doc_id: str, owner: str, owner_tid: str = "", *, email=None, oid=None):
    body = {"grantee_email": email} if email else {"grantee_oid": oid}
    return client.post(f"/documents/{doc_id}/grants",
                       cookies=_cookie(owner, owner_tid), json=body)


def _can_read(oid: str, doc_id: str, tid: str = "") -> bool:
    """Can this identity retrieve THIS document's own unique sentence? Asking per document
    is what keeps one test's share from deciding another test's answer."""
    r = client.post("/search", cookies=_cookie(oid, tid),
                    json={"question": f"Hamburg actuator line in {doc_id} throughput"})
    assert r.status_code == 200, r.text[:200]
    return doc_id in r.text and "62 percent" in r.text


# ---- D1 + D3 + D5: the boundary a share may now cross ------------------------------------

def test_a_share_reaches_another_account_and_the_grantee_can_ask():
    """THE BUG: this returned 200 and shared nothing. Both parties are in their own
    private acct: partitions, which is the case that was silently dead."""
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-cross", ALICE)
    assert not _can_read(BOB, doc), "bob must start with no access"
    r = _share(doc, ALICE, email="bob@x.com")
    assert r.status_code == 200, f"share refused: {r.status_code} {r.text[:200]}"
    assert _can_read(BOB, doc), "the share returned 200 and shared nothing"


def test_a_bystander_in_a_third_partition_still_sees_nothing():
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-bystander", ALICE)
    assert _share(doc, ALICE, email="bob@x.com").status_code == 200
    assert not _can_read(CAROL, doc), "the doorway leaked to an unrelated account"


def test_the_doorway_opens_one_document_not_the_partition():
    """ADR 0019 D3: a pair, not a partition key. Alice's OTHER document must stay shut."""
    _real_login(); _seed_accounts()
    shared = _ingest("doc-582-shared", ALICE)
    other = _ingest("doc-582-private", ALICE)
    assert _share(shared, ALICE, email="bob@x.com").status_code == 200
    listed = {d["doc_external_id"] for d in
              client.get("/admin/documents", cookies=_cookie(BOB)).json()}
    assert shared in listed, listed
    assert other not in listed, "the doorway acted as a partition skeleton key"


def test_a_home_tenant_document_can_be_shared_with_an_account():
    """acct: <-> home, the other direction ADR 0019 D1 allows."""
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-home-to-acct", HOME_USER, HOME_TID)
    assert _share(doc, HOME_USER, HOME_TID, email="bob@x.com").status_code == 200
    assert _can_read(BOB, doc)


# ---- D4: what the grantee gets --------------------------------------------------------

def test_the_grantee_gets_full_read_parity():
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-parity", ALICE)
    assert _share(doc, ALICE, email="bob@x.com").status_code == 200
    rows = {d["doc_external_id"]: d for d in
            client.get("/admin/documents", cookies=_cookie(BOB)).json()}
    assert doc in rows, "a shared document must appear in the grantee's own listing"
    assert rows[doc].get("owned_by_you") is not True, "a shared document is not yours"
    assert client.get(f"/admin/documents/{doc}/segments",
                      cookies=_cookie(BOB)).status_code == 200
    assert client.get(f"/admin/documents/{doc}/download?form=text",
                      cookies=_cookie(BOB)).status_code == 200, \
        "a citation the grantee cannot open reads as a bug"


def test_the_grantee_cannot_delete_or_reshare():
    """Read parity, not write parity. ADR 0017 s2 (no re-share) and #594 (ownership)."""
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-nowrite", ALICE)
    assert _share(doc, ALICE, email="bob@x.com").status_code == 200
    # Note the path: DELETE lives at /documents/{id}, not /admin/documents/{id}. An earlier
    # version of this test used the /admin path, got a 405 from the router, and would have
    # "passed" a delete guard it never actually reached.
    assert client.delete(f"/documents/{doc}", cookies=_cookie(BOB)).status_code == 404
    assert _share(doc, BOB, email="carol@x.com").status_code == 404, "a share was re-shared"


def test_revoking_closes_the_doorway_on_the_very_next_request():
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-revoke", ALICE)
    grant_id = _share(doc, ALICE, email="bob@x.com").json()["grant_id"]
    assert _can_read(BOB, doc)
    assert client.delete(f"/grants/{grant_id}", cookies=_cookie(ALICE)).status_code == 200
    assert not _can_read(BOB, doc), "a revoked grant still retrieved"
    listed = {d["doc_external_id"] for d in
              client.get("/admin/documents", cookies=_cookie(BOB)).json()}
    assert doc not in listed, "a revoked document still appeared in the listing"


# ---- D1: the boundary a share may NOT cross -------------------------------------------

def test_sharing_into_a_foreign_tenant_is_refused():
    """The case that was SILENT before #582 - allowed, and retrieved nothing forever."""
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-into-foreign", ALICE)
    r = _share(doc, ALICE, email="them@other.com")
    assert r.status_code == 400, r.status_code
    assert "organization" in r.json()["detail"], r.json()


def test_sharing_out_of_a_foreign_tenant_is_refused():
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-out-of-foreign", FOREIGNER, "tid-somewhere-else")
    r = _share(doc, FOREIGNER, "tid-somewhere-else", email="bob@x.com")
    assert r.status_code == 400, r.status_code


def test_an_account_whose_tenant_was_never_recorded_fails_closed():
    """Unknowable is refused, not guessed - guessing would hand a foreign user an acct:
    partition they do not have and rebuild the silent bug inside its own fix."""
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-unrecorded", ALICE)
    r = _share(doc, ALICE, email="quiet@corp.com")
    assert r.status_code == 400, r.status_code
    assert "sign in" in r.json()["detail"], r.json()


def test_sharing_with_an_unknown_address_is_refused_honestly():
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-unknown-email", ALICE)
    r = _share(doc, ALICE, email="nobody@nowhere.com")
    assert r.status_code == 400, r.status_code
    assert "signed in with that address" in r.json()["detail"], r.json()


def test_a_foreign_tenant_reader_gets_no_doorway_even_holding_a_live_grant():
    """D1 is ENFORCED AT READ TIME, which is the only place both partitions are verified.

    The hole this closes: a grant may name an account with no identity rows yet - a
    colleague who has not signed in - and share-time refusal has nothing to check. If that
    person then signs in from a FOREIGN tenant, the doorway would hand them a home-tenant
    document by a route the share-time check never saw. So the doorway is withheld from a
    foreign-partition session outright.
    """
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-read-time", HOME_USER, HOME_TID)
    stranger = "66666666-6666-6666-6666-666666666666"      # no identity rows at all
    assert _share(doc, HOME_USER, HOME_TID, oid=stranger).status_code == 200, \
        "sharing with someone who has not signed in yet must still be allowed"
    # ...and now they turn out to be a foreign-tenant user.
    assert not _can_read(stranger, doc, "tid-somewhere-else"), \
        "a foreign-tenant session used a live grant to read a home-tenant document"


def test_a_legacy_grant_naming_a_foreign_partition_is_inert():
    """The doorway filters foreign partitions even when a GRANT names one.

    Nothing in the API can create such a grant any more - `_refuse_cross_partition_share`
    stops it - so this builds the row directly on the registry, which is exactly how one
    would arrive in reality: a grant created BEFORE #582 shipped, sitting in Postgres and
    rehydrated by the next restart (grants.py `load_all`).

    Written because mutation testing showed the filter was unprotected: deleting it broke
    no test, since every route into that state had just been closed. A guard nothing can
    fail is a guard nobody can trust.
    """
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-legacy-foreign", FOREIGNER, "tid-somewhere-else")
    # A grant that today's API would refuse: out of a foreign tenant, into a local account.
    legacy = _edition.grant_registry.create(
        doc_external_id=doc, tenant_id="tid-somewhere-else",
        grantee_oid=BOB, granted_by=FOREIGNER)
    _edition.index.add_doc_principals("tid-somewhere-else", doc, [legacy.principal])
    try:
        assert not _can_read(BOB, doc), \
            "a legacy grant re-opened a foreign tenant's partition"
        listed = {d["doc_external_id"] for d in
                  client.get("/admin/documents", cookies=_cookie(BOB)).json()}
        assert doc not in listed, "a legacy foreign grant surfaced in the listing"
    finally:
        _edition.grant_registry.discard_unconfirmed(legacy.grant_id)


def test_a_refused_share_leaves_no_grant_row():
    """#575 Fix 2 ordering: refuse BEFORE the record is created."""
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-no-phantom", ALICE)
    assert _share(doc, ALICE, email="them@other.com").status_code == 400
    grants = client.get(f"/documents/{doc}/grants", cookies=_cookie(ALICE)).json()
    assert grants == [], f"a refused share left a phantom row: {grants}"


# ---- the page must not offer what the API refuses ---------------------------------------

def test_the_listing_marks_a_document_that_was_shared_with_you():
    """The page cannot tell the truth about a shared row without this flag - it called
    somebody else's handbook "everything you have added" and offered Share on it."""
    _real_login(); _seed_accounts()
    doc = _ingest("doc-582-marked", ALICE)
    assert _share(doc, ALICE, email="bob@x.com").status_code == 200
    rows = {d["doc_external_id"]: d for d in
            client.get("/admin/documents", cookies=_cookie(BOB)).json()}
    assert rows[doc]["shared_with_you"] is True, rows[doc]
    assert rows[doc]["owned_by_you"] is False, rows[doc]
    mine = {d["doc_external_id"]: d for d in
            client.get("/admin/documents", cookies=_cookie(ALICE)).json()}
    assert mine[doc]["shared_with_you"] is False, "the owner's own row must not say shared"
    assert mine[doc]["owned_by_you"] is True, mine[doc]


def test_the_ui_offers_share_only_on_documents_you_own():
    """Same rule and same reason as #594's Delete check (#551, the tile that always fails).
    A share cannot be re-shared (ADR 0017 s2), so the API 404s - and before this the button
    was drawn on every shared row, because a grantee had never been able to see one."""
    import re as _re
    code = (ROOT / "src/dbsearch/server/static/js/surfaces/admin.js").read_text()
    assert _re.search(r'if\s*\(\s*doc\.owned_by_you[^)]*\)[^\n]*shareBtn', code), (
        "the Share control is added without a doc.owned_by_you condition, so it is drawn "
        "on documents the API will refuse to share")
    assert "Everything you have added, and who can read each one." not in code, (
        "the panel still claims every document listed was added by the caller")


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
    for k in _VARS:
        os.environ.pop(k, None)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
