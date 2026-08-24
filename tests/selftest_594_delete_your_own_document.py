"""#594 - you can put a document in, and now you can take it out again.

"Your data" listed every document with Share, Check text and Download, and no way to remove
one. There was no DELETE route at all - only GET /admin/documents and the segments/download
readers. With the retention sweep deliberately disabled (#576), nothing in the product ever
removed an upload. A page titled "Your data" that cannot remove your data is the wrong
promise, and it is the first thing any customer asks for.

Delete is the one operation here with no undo, so the rules it has to obey are stricter than
the ones around it:

  - OWNERSHIP, not readability. `_may_share` lets anyone whose principals intersect the ACL
    share a document; that rule must NOT be reused here, or one colleague on an org-wide HR
    policy could destroy it for everyone. Ownership is ADR 0012's `owner_oid`, read back
    through `docs_owned_by`.
  - 404, never 403 (the #549 rule). A 403 tells someone who cannot see the document that it
    exists, which is the metadata leak #549 closed - reopened through a new door.
  - The UI must not offer what the API will refuse: the listing says whether each row is
    yours, so a Delete control is only drawn on documents you can actually delete. Offering
    a button that can only 404 is the "tile that always fails" trap (#551).
  - Everything goes: index rows, all four blob families, and the grants. A revoked share
    that outlives its document is #576's revoke-fails-open bug in a new place.

    PYTHONPATH=src python3 tests/selftest_594_delete_your_own_document.py
"""
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import app, _edition  # noqa: E402

STATIC = ROOT / "src/dbsearch/server/static"
client = TestClient(app)

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
OPERATOR = "99999999-9999-9999-9999-999999999999"

_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")


def _secret(doc_id: str) -> str:
    """Body text unique to ONE document.

    The first version of this file gave every seeded document the SAME sentence, so
    "the text is gone after delete" passed or failed on whether some OTHER document from an
    earlier test in the same process still carried it. Same shared-state trap as the vacuous
    test in #593 - the index, like the audit log, lives for the whole run.
    """
    return f"The Hamburg actuator line in {doc_id} runs at 62 percent of rated throughput."


def _real_login():
    for k in _VARS:
        os.environ.pop(k, None)
    os.environ.update({"AUTH_TENANT_ID": "tid-1", "AUTH_CLIENT_ID": "cid",
                       "AUTH_CLIENT_SECRET": "sec", "DBSEARCH_OPERATOR_OIDS": OPERATOR})


def _cookie(oid: str) -> dict:
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": "tid-1", "exp": int(time.time()) + 3600})}


def _ingest(doc_id: str, owner: str, acl: list) -> str:
    r = client.post("/ingest", cookies=_cookie(owner), json={
        "external_id": doc_id, "title": f"title of {doc_id}", "text": _secret(doc_id),
        "acl": acl, "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, f"seed ingest failed: {r.status_code} {r.text[:200]}"
    return doc_id


def _titles(oid: str) -> str:
    return json.dumps(client.get("/admin/documents", cookies=_cookie(oid)).json())


def _delete(doc_id: str, oid: "str | None"):
    kw = {"cookies": _cookie(oid)} if oid else {}
    return client.request("DELETE", f"/documents/{doc_id}", **kw)


# ---- the thing that was missing ---------------------------------------------------------

def test_the_owner_can_delete_and_it_is_really_gone():
    _real_login()
    doc = _ingest("doc-594-mine", ALICE, [ALICE])
    assert doc in _titles(ALICE), "seed did not land"

    r = _delete(doc, ALICE)
    assert r.status_code == 200, f"the owner could not delete her own document: {r.status_code}"

    assert doc not in _titles(ALICE), "the document is still listed after a successful delete"
    answer = client.post("/search", cookies=_cookie(ALICE),
                         json={"question": f"what throughput does the line in {doc} run at"})
    assert _secret(doc) not in json.dumps(answer.json()), (
        "the document was delisted but its TEXT is still retrievable - the index rows survived")


def test_deleting_takes_the_grants_with_it():
    """A grant that outlives its document is #576's revoke-fails-open shape in a new place:
    the row stays in Postgres and every restart resurrects a share of something that is gone."""
    _real_login()
    doc = _ingest("doc-594-shared", ALICE, [ALICE])
    g = client.post(f"/documents/{doc}/grants", cookies=_cookie(ALICE),
                    json={"grantee_oid": BOB})
    assert g.status_code == 200, f"could not seed a grant: {g.status_code} {g.text[:200]}"

    assert _delete(doc, ALICE).status_code == 200
    left = _edition.grant_registry.list_for_document(doc)
    assert not left, f"{len(left)} grant(s) outlived the document they shared"


# ---- who may not -------------------------------------------------------------------------

def test_a_colleague_who_can_read_it_cannot_delete_it():
    """The rule that must NOT be copied from _may_share. Readability is not ownership: on an
    org-wide policy every colleague reads it, and any one of them could destroy it."""
    _real_login()
    doc = _ingest("doc-594-org", ALICE, [ALICE, BOB])
    assert doc in _titles(BOB), "seed is wrong - bob cannot read it, so this proves nothing"

    r = _delete(doc, BOB)
    assert r.status_code == 404, (
        f"a reader deleted a document he does not own (got {r.status_code})")
    assert doc in _titles(ALICE), "the document was destroyed by someone who only read it"


def test_a_stranger_is_told_nothing():
    """404, never 403 - the #549 rule. A refusal must not confirm the document exists."""
    _real_login()
    doc = _ingest("doc-594-private", ALICE, [ALICE])
    r = _delete(doc, BOB)
    assert r.status_code == 404, f"expected 404, got {r.status_code}"
    assert "title of" not in r.text, "the refusal disclosed the document's title"
    missing = _delete("doc-594-does-not-exist-at-all", BOB)
    assert missing.status_code == r.status_code, (
        "a document that exists and one that does not answer differently, so the refusal "
        "itself tells a stranger which is which")
    assert doc in _titles(ALICE), "the stranger's refused delete still destroyed it"


def test_signed_out_cannot_delete():
    _real_login()
    doc = _ingest("doc-594-anon", ALICE, [ALICE])
    assert _delete(doc, None).status_code == 401
    assert doc in _titles(ALICE)


def _partition() -> str:
    """The partition the routes resolve for these test cookies (ADR 0012)."""
    from dbsearch.api.auth import resolve_tenant

    from dbsearch.server.app import _edition as ed
    return resolve_tenant(lambda _k, d=None: d,
                          {user_auth.COOKIE: _cookie(ALICE)[user_auth.COOKIE]}.get,
                          ed.tenant_id)


def test_an_unsafe_id_is_refused_even_when_you_own_it():
    """Defence in depth behind ownership, not instead of it.

    `doc_id` is interpolated straight into `raw/{partition}/{doc_id}` by blob_prefixes() - the
    exact shape that made the #576 sweep delete the blob root - and external_id is still
    unvalidated at several entry points (#581). So the dangerous case is not a stranger
    guessing: it is a document that really IS yours and whose id should never have been
    accepted in the first place. Ownership passes; the id guard has to be what refuses.

    Seeded straight into the index rather than through /ingest, because /ingest already
    refuses these - simulating the unvalidated path is the whole point.

    Consequence, accepted deliberately: a document whose id got in unvalidated cannot be
    deleted through this route either. Safe direction, and #581 is the fix for the cause.
    """
    _real_login()
    part = _partition()
    _edition.ingest_document(".hidden-594", "sneaky", _secret(".hidden-594"),
                             [ALICE], "upload://x.txt", tenant_id=part, owner_oid=ALICE)
    assert ".hidden-594" in set(_edition.index.docs_owned_by(part, ALICE)), (
        "the seed did not land as an OWNED document, so this proves nothing")

    calls = []
    store = _edition.store
    real = store.delete_prefix
    store.delete_prefix = lambda prefix: (calls.append(prefix), real(prefix))[1]
    try:
        r = _delete(urllib.parse.quote(".hidden-594", safe=""), ALICE)
        assert r.status_code == 404, (
            f"an unsafe id passed ownership and was accepted anyway: {r.status_code}")
        assert not calls, f"a refused delete still reached the object store with {calls!r}"
    finally:
        store.delete_prefix = real


def test_a_refused_delete_touches_no_blob():
    """The other half: an id that is merely NOT YOURS must also never reach the store."""
    _real_login()
    doc = _ingest("doc-594-untouchable", ALICE, [ALICE, BOB])
    calls = []
    store = _edition.store
    real = store.delete_prefix
    store.delete_prefix = lambda prefix: (calls.append(prefix), real(prefix))[1]
    try:
        assert _delete(doc, BOB).status_code == 404
        assert not calls, f"a refused delete still reached the object store with {calls!r}"
    finally:
        store.delete_prefix = real


# ---- the listing has to be honest about it ----------------------------------------------

def test_the_listing_says_which_documents_are_yours():
    """The UI needs this to avoid drawing a button that can only fail (#551)."""
    _real_login()
    doc = _ingest("doc-594-owned-flag", ALICE, [ALICE, BOB])
    rows = {d["doc_external_id"]: d for d in
            client.get("/admin/documents", cookies=_cookie(ALICE)).json()}
    assert rows[doc].get("owned_by_you") is True, "alice's own document is not marked as hers"
    bobs = {d["doc_external_id"]: d for d in
            client.get("/admin/documents", cookies=_cookie(BOB)).json()}
    assert bobs[doc].get("owned_by_you") is False, (
        "a document bob can only READ is marked as his, so the UI will offer him a Delete "
        "button that can only 404")


def test_the_listing_never_names_another_users_oid():
    """#791: ownership is disclosed as a BOOLEAN about the caller, never as somebody
    else's identifier.

    `owner_oid` had to reach the supersede-by-uri loop server-side (a doc's owner is what
    stops one user's upload deleting another's, #791). The trap is that `/admin/documents`
    serializes the DocACL wholesale, so surfacing the field on the object alone would put
    every uploader's OID into a listing that every colleague who can READ the document
    receives - a directory disclosure nobody asked for, in the endpoint whose entire
    history is metadata-leak fixes (#549/#550/#582/#594). `owned_by_you` is the contract."""
    _real_login()
    # ACL names BOB only. Alice's OID is therefore NOT a legitimate principal on this row,
    # so any occurrence of it in bob's listing is the uploader identity leaking - the
    # discrimination the first draft of this test lacked (an ACL that named alice made the
    # substring assertion unsatisfiable whether the bug was present or not).
    doc = _ingest("doc-791-owner-not-on-the-wire", ALICE, [BOB])
    body = _titles(BOB)
    rows = {d["doc_external_id"]: d for d in json.loads(body)}
    assert doc in rows, f"bob cannot see the document at all, so this proves nothing: {body[:300]}"
    assert "owner_oid" not in rows[doc], (
        f"the listing exposes an owner_oid field instead of owned_by_you: {rows[doc]!r}")
    # THIS row only: the index lives for the whole run, and other tests in this file seed
    # documents whose ACLs name alice legitimately (the shared-state trap in the docstring).
    row_json = json.dumps(rows[doc])
    assert ALICE not in row_json, (
        "bob's row for alice's document carries alice's OID - the supersede fix leaked an "
        f"uploader identity onto the wire: {row_json}")
    assert rows[doc].get("owned_by_you") is False, rows[doc]


def _admin_code() -> str:
    """admin.js with comments stripped.

    Both UI assertions below were VACUOUS against the raw file: the code explains in prose why
    it consults `owned_by_you` and why it avoids confirm(), so a naive substring search matched
    the explanation and passed even with the behaviour deleted. A test a comment can satisfy is
    not a test.
    """
    admin = (STATIC / "js/surfaces/admin.js").read_text()
    code = re.sub(r"/\*.*?\*/", "", admin, flags=re.S)
    return re.sub(r"^\s*//.*$", "", code, flags=re.M)


def test_the_ui_offers_delete_only_on_documents_you_own():
    code = _admin_code()
    assert "deleteDocument" in code, "admin.js never calls the delete API"
    m = re.search(r'if \s*\(\s*doc\.owned_by_you[^)]*\)[^\n]*deleteControl', code)
    assert m, (
        "the Delete control is appended without a doc.owned_by_you condition in front of it, "
        "so it is drawn on documents the API will refuse to delete")


def test_delete_asks_before_it_destroys():
    """No undo, so it must not be a single click - and it must not use a native confirm()
    either: a browser modal blocks the whole page (and every automation session that has to
    verify this) until somebody dismisses it by hand."""
    code = _admin_code()
    assert not re.search(r'(^|[^.\w])confirm\s*\(', code), (
        "admin.js calls a native confirm() dialog")

    body = code[code.index("function deleteControl("):]
    body = body[:body.index("\nfunction ") if "\nfunction " in body else len(body)]

    first, _, second = body.partition('"Yes, delete"')
    assert second, 'the confirm step has no "Yes, delete" control'
    assert "deleteDocument" not in first, (
        "the FIRST Delete click already calls deleteDocument - there is no confirmation step, "
        "only a button that looks like one")
    assert "deleteDocument" in second, "nothing after the confirm control actually deletes"
    assert re.search(r'Delete permanently|Really delete', body), (
        "the confirm step does not say that the deletion is permanent")


def test_the_post_delete_rerender_keeps_the_caller_identity():
    """Caught by driving prod, not by any test here.

    The first version re-rendered the listing with `renderDocuments(grid, "", ...)`. The list
    came back correct, but audienceLabel() needs the caller's own oid to recognise a private
    document, so every SURVIVING row's badge flipped from "Only you" to "1 group(s)" the moment
    anything was deleted. Nothing had been shared - the page had just lost the ability to tell,
    and the one moment it lied was the moment the user was watching a destructive action.
    """
    code = _admin_code()
    bad = re.findall(r'renderDocuments\(\s*\w+\s*,\s*""', code)
    assert not bad, (
        "renderDocuments is called with an empty identity, so every row's audience badge "
        "will render as a group count instead of \"Only you\"")


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
