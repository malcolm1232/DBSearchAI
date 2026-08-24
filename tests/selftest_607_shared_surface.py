"""#607 / #608 - the owner's management surface over every share she has made.

Two routes and one Admin section: `GET /shares/mine` lists every live share this caller
granted, both audiences; `PATCH /shares/{share_id}/scope` takes documents back out of one that
is already live; and the Shared section on "Your data" renders a row per share with Edit, View
and Revoke.

WHAT EVERY ASSERTION HERE IS MADE AGAINST, because this branch has already shipped four tests
that were green while nobody had been shown anything (Task 6: they grepped a .js file the
visitor's page never loaded). Every assertion below is one of exactly two kinds, and the report
names which:

  RESPONSE   what a real HTTP call through the real app RETURNS - the JSON an owner receives,
             and, for the narrowing, the TEXT a cookie-carrying visitor receives on their very
             next request afterwards.
  DOM        the surface MOUNTED in a real DOM (jsdom) and driven the way a person drives it -
             the rows that exist, the dialog Edit opens, the request Save sends.

There is no third kind. Nothing here asserts that a string is present in a file on disk.

THE DEFECT SHAPE THIS FILE EXISTS TO CATCH is the one this feature has produced four times: an
authorization change that closes the GRANT channel and leaves the CONTENT channel open beside
it, so the person cut off still reads the answer TEXT synthesized out of the document that was
taken away. `test_narrowing_stops_the_content_a_cookied_visitor_already_had` is therefore
written against the visitor's transcript body and their next answer, never against grant rows
or counts.

    PYTHONPATH=src python3 tests/selftest_607_shared_surface.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import _domgate  # noqa: E402  the shared jsdom gate (#792)
os.environ.setdefault("SELFHOST_BACKEND", "memory")
# selftest_605/611's rig rule: the public demo's per-IP cap is installed at import time and
# every test here is several real HTTP calls.
os.environ["DBSEARCH_RATE_LIMIT"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.query.conversation import Turn  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import ACCOUNTS, app, _edition  # noqa: E402
from dbsearch.server.conversation_shares import AUDIENCE_LINK  # noqa: E402
from dbsearch.server.link_access import link_principal  # noqa: E402

client = TestClient(app)

ALICE = "acct_alice"
BOB = "acct_bob"
HOME_TID = "tid-607-shared-home"
CK = {}

# Two documents with markers that cannot be mistaken for one another, so an assertion about
# WHICH document's content reached somebody is unambiguous.
A_ID, A_MARKER = "doc-607-hamburg", "62 weeks"
B_ID, B_MARKER = "doc-607-lisbon", "26 days"
A_TEXT = f"The Hamburg severance clause in {A_ID} pays out over {A_MARKER} of salary."
B_TEXT = f"The Lisbon carryover allowance in {B_ID} carries over {B_MARKER} of unused leave."


def _seed_one_partition():
    """selftest_605/611's rig verbatim: two identities in the deployment's OWN partition, the
    single-org shape where a partition filter protects nobody from anybody - so nothing here
    can pass because of a tenant boundary that a real self-host box does not have."""
    global CK
    for k in ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET"):
        os.environ.pop(k, None)
    os.environ.update({"AUTH_TENANT_ID": HOME_TID, "AUTH_CLIENT_ID": "cid",
                       "AUTH_CLIENT_SECRET": "sec"})
    ACCOUNTS.resolve("local", "alice@x.com", preferred_account_id=ALICE, email="alice@x.com")
    ACCOUNTS.resolve("local", "bob@x.com", preferred_account_id=BOB, email="bob@x.com")
    one = _edition.tenant_id
    CK = {who: {user_auth.COOKIE: user_auth.sign_session(
        {"oid": who, "tid": one, "exp": int(time.time()) + 3600})}
        for who in (ALICE, BOB)}


def _ingest(doc_id: str, text: str, owner: str = BOB) -> str:
    r = client.post("/ingest", cookies=CK[owner], json={
        "external_id": doc_id, "title": f"policy {doc_id}", "acl": [owner],
        "text": text, "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, f"seed ingest failed: {r.status_code} {r.text[:200]}"
    return doc_id


def _turn(conv: str, question: str, answer: str, docs: list, who: str = BOB) -> None:
    _edition.conversation_service._store.append(conv, who, Turn(
        question=question, standalone=question, answer=answer, cited_docs=list(docs)))


def _two_turn_thread(conv: str, owner: str = BOB):
    """Turn 0 draws on A, turn 1 draws on B. Removing B must stop the transcript BEFORE turn 1
    while leaving turn 0 readable, which is the only shape that can tell a real narrowing apart
    from a share that simply died."""
    _ingest(A_ID, A_TEXT, owner=owner)
    _ingest(B_ID, B_TEXT, owner=owner)
    _turn(conv, "how many weeks of severance in Hamburg?", f"It pays out over {A_MARKER}.",
          [A_ID], who=owner)
    _turn(conv, "and how much leave carries over in Lisbon?",
          f"It carries over {B_MARKER}.", [B_ID], who=owner)


def _make_link(conv: str, owner: str = BOB) -> dict:
    r = client.post(f"/conversations/{conv}/shares", cookies=CK[owner],
                    json={"audience": AUDIENCE_LINK})
    assert r.status_code == 200, f"link share failed: {r.status_code} {r.text[:300]}"
    return r.json()


def _make_people(conv: str, owner: str = BOB, to: str = ALICE) -> dict:
    r = client.post(f"/conversations/{conv}/shares", cookies=CK[owner],
                    json={"grantee_oid": to})
    assert r.status_code == 200, f"people share failed: {r.status_code} {r.text[:300]}"
    return r.json()


def _mine(who: str = BOB):
    return client.get("/shares/mine", cookies=CK[who])


def _patch(share_id: str, body: dict, who: str = BOB):
    return client.patch(f"/shares/{share_id}/scope", cookies=CK[who], json=body)


def _row(body: dict, share_id: str) -> dict:
    for s in body["shares"]:
        if s["share_id"] == share_id:
            return s
    raise AssertionError(f"share {share_id} is not in /shares/mine: {body}")


def _cleanup(conv: str, *grantees: str):
    for g in grantees:
        try:
            _edition.grant_registry.drop_for_conversation(conv, g)
        except Exception:
            pass


def _share_record(share_id: str, owner: str = BOB):
    return _edition.conversation_shares.find(share_id, owner)


# ---- RESPONSE: the listing -----------------------------------------------------------------

def test_the_owner_sees_both_audiences_with_scope_names_and_counts():
    """RESPONSE. One thread, shared twice - once to a named person, once as a link - and both
    rows come back from ONE call, because the owner is not asked to remember which doorway she
    used. Every field on the row is checked, since a management surface with a field that is
    quietly always empty is a surface she stops reading."""
    _seed_one_partition()
    conv = "c-607-list"
    _two_turn_thread(conv)
    people = _make_people(conv)
    link = _make_link(conv)
    try:
        visitor = TestClient(app)
        # The PAGE first, then the question: `record_open` counts a page load, which is what a
        # person clicking a link actually does. Posting straight to /chat would leave `opens`
        # at zero and make the assertion below pass or fail for the wrong reason.
        assert visitor.get(link["url"]).status_code == 200
        assert visitor.post(link["url"] + "/chat",
                            json={"question": "STRANGER-Q how many weeks?"}
                            ).status_code == 200

        r = _mine()
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        ids = {s["share_id"] for s in body["shares"]}
        assert {people["share_id"], link["share_id"]} <= ids, (
            f"the owner's own shares are missing from /shares/mine: {body}")

        p, l = _row(body, people["share_id"]), _row(body, link["share_id"])
        for s in (p, l):
            assert s["first_question"] == "how many weeks of severance in Hamburg?", (
                f"the row has no human name - it is unreadable as a list: {s}")
            assert {d["id"] for d in s["scope"]} == {A_ID, B_ID}, (
                f"the row does not say which documents the share opens: {s}")
            assert all(d["title"] for d in s["scope"]), f"a scope row has no title: {s}"
            assert s["conv_id"] == conv and s["live"] is True, s
            assert "token_hash" not in s, (
                f"the link's credential digest reached a management listing: {s}")

        assert p["audience"] == "people" and p["grantee_oid"] == ALICE, p
        assert p["questions_asked"] == 0, (
            "a named grantee's own follow-up questions are hers, not the grantor's to count")
        assert l["audience"] == AUDIENCE_LINK, l
        assert l["opens"] >= 1 and l["last_open_at"], (
            f"the link has been used and the row shows no trace of it: {l}")
        assert l["questions_asked"] == 1, (
            f"a stranger asked one question through this link; the row says "
            f"{l['questions_asked']}: {l}")
        assert l["expires_at"], "a link with no expiry contradicts ADR 0021 invariant 3"
    finally:
        _cleanup(conv, ALICE, link_principal(_share_record(link["share_id"])))


def test_the_listing_is_scoped_to_the_caller_and_never_names_somebody_elses_share():
    """RESPONSE. `list_granted_by` is scoped inside the registry, so there is no parameter on
    this route for a caller to widen. Alice, a real signed-in account, sees nothing of Bob's."""
    _seed_one_partition()
    conv = "c-607-scoped"
    _two_turn_thread(conv)
    link = _make_link(conv)
    try:
        mine = _mine(BOB)
        assert link["share_id"] in mine.text, "control: the owner must see her own share"
        theirs = _mine(ALICE)
        assert theirs.status_code == 200, theirs.text[:200]
        assert link["share_id"] not in theirs.text, (
            f"somebody else's share was listed to a caller who did not make it: "
            f"{theirs.text[:400]}")
        assert "how many weeks of severance" not in theirs.text, (
            "the grantor's own question text reached a caller who is not the grantor")
        anon = TestClient(app).get("/shares/mine")
        assert anon.status_code == 401, (
            f"an anonymous caller reached the management listing: {anon.status_code}")
    finally:
        _cleanup(conv, link_principal(_share_record(link["share_id"])))


# ---- RESPONSE: the narrowing, asserted on CONTENT ------------------------------------------

def test_narrowing_stops_the_content_a_cookied_visitor_already_had():
    """RESPONSE, AND THIS IS THE ONE THAT MATTERS.

    The defect shape this branch has produced four times is an authorization change that closes
    the grant channel and leaves the content channel open beside it. So this asserts on the
    TEXT: the visitor's transcript carries B's marker before the PATCH and must not after, and
    their next question about B must come back from a corpus of one document without B's marker
    in the answer.

    THE VISITOR IS ALREADY COOKIED AND ALREADY ASKING before the owner narrows, because "it
    takes effect at the next sign-in" and "it takes effect now" are indistinguishable to a test
    that opens a fresh client afterwards. The same client, the very next request.

    A POSITIVE CONTROL COMES FIRST on both channels. Without it a broken rig - a share that
    never opened B at all - would produce exactly the same green.

    WHAT IS DELIBERATELY NOT ASSERTED, and the honest reason - #614.

    The visitor's OWN half of the thread keeps the answer they were already given, and this
    test does not demand otherwise. AN EARLIER VERSION OF THIS PARAGRAPH JUSTIFIED THAT BY
    CITING `conversation_transcript`'s "the answers she was legitimately given do not get
    retracted", AND THAT JUSTIFICATION WAS WRONG. That docstring is about a SIGNED-IN GRANTEE
    reading turns stored under her own account key, which survive because they are hers. It
    says nothing about this doorway, and this doorway does the opposite: revoking a link share
    makes `/c/{token}/transcript` 404 outright - `_live_share(token)` refuses before any
    history is read - so revoke retracts the visitor's fork TOO. Verified directly, not
    reasoned about.

    So the true statement is an ASYMMETRY, not a policy: revoke is strictly stronger than
    narrowing on this channel, and narrowing leaves a fork residue that revoke does not. That
    asymmetry may well be wrong, but which way it should go is the owner's call rather than
    this task's, and it is carded as #614. The behaviour is deliberately left alone here.

    A wrong explanation is worse than no explanation, because it tells the next reader not to
    look - which is exactly what the old paragraph did.

    The assertions below are therefore made against the GRANTOR'S HALF specifically - the turns
    the share hands over, which is what a narrowing governs - and against every FRESH answer,
    which is where a still-open retrieval channel would show up."""
    _seed_one_partition()
    conv = "c-607-narrow"
    _two_turn_thread(conv)
    link = _make_link(conv)
    url = link["url"]

    def shared_half(resp):
        """The turns the SHARE hands over, as opposed to this visitor's own fork."""
        return [t for t in resp.json()["turns"] if not t.get("own")]

    try:
        v = TestClient(app)
        assert v.get(url).status_code == 200, "the visitor could not open the link"

        before = v.get(url + "/transcript")
        assert before.status_code == 200, before.text[:300]
        handed = shared_half(before)
        assert B_MARKER in json.dumps(handed), (
            "control: the visitor must be able to read the Lisbon turn BEFORE the narrowing, "
            f"or this test proves nothing: {handed}")
        assert A_MARKER in json.dumps(handed), handed

        asked = v.post(url + "/chat", json={"question": "how much leave carries over?"})
        assert asked.status_code == 200, asked.text[:300]
        assert asked.json()["corpus"]["authorized_docs"] == 2, (
            f"control: the share must open both documents first: {asked.json()['corpus']}")
        assert B_MARKER in asked.json()["answer"], (
            f"control: the removed document must be answerable first: {asked.json()['answer']}")

        cut = _patch(link["share_id"], {"remove_docs": [B_ID]})
        assert cut.status_code == 200, f"the owner could not narrow her own share: {cut.text}"
        assert cut.json()["documents"] == 1 and cut.json()["removed"] == 1, cut.json()

        after = v.get(url + "/transcript")
        assert after.status_code == 200, after.text[:300]
        left = json.dumps(shared_half(after))
        assert B_MARKER not in left, (
            "THE CONTENT CHANNEL IS STILL OPEN: a visitor holding the link still reads the "
            "shared answer text synthesized out of the document that was just removed - "
            f"{left[:500]}")
        assert "carries over" not in left, (
            f"the removed document's turn is still being handed over: {left[:500]}")
        assert A_MARKER in left, (
            "the narrowing killed the whole share instead of one document - the surviving turn "
            f"must still be readable: {left[:400]}")
        assert len(shared_half(after)) == 1, (
            f"the shared half must stop at the removed document's turn: {shared_half(after)}")

        again = v.post(url + "/chat", json={"question": "how much leave carries over?"})
        assert again.status_code == 200, again.text[:300]
        assert again.json()["corpus"]["authorized_docs"] == 1, (
            f"the removed document is still retrievable by a link-holder: "
            f"{again.json()['corpus']}")
        assert B_MARKER not in again.json()["answer"], (
            f"the removed document's content was synthesized into a fresh answer: "
            f"{again.json()['answer'][:300]}")
    finally:
        _cleanup(conv, link_principal(_share_record(link["share_id"])))


def test_the_record_stops_claiming_more_turns_than_it_actually_hands_over():
    """RESPONSE. `turn_cutoff` is on the share record and reaches BOTH the owner (here) and the
    grantee (/conversations/shared-with-me). After a narrowing it must equal the number of the
    grantor's turns the share still hands over - a record that still says "the first two turns"
    over a share serving one is a number two people read and act on.

    This is the assertion that dies if the PATCH drops the grants and skips the boundary
    recompute; the transcript assertion above does not, because `_readable_prefix` re-derives
    the prefix from live grants on every read. Both are kept, and the report says so."""
    _seed_one_partition()
    conv = "c-607-cutoff"
    _two_turn_thread(conv)
    link = _make_link(conv)
    try:
        assert _row(_mine().json(), link["share_id"])["turn_cutoff"] == 2, "control"
        assert _patch(link["share_id"], {"remove_docs": [B_ID]}).status_code == 200

        v = TestClient(app)
        served = v.get(link["url"] + "/transcript").json()["turns"]
        grantor_turns = [t for t in served if not t.get("own")]
        row = _row(_mine().json(), link["share_id"])
        assert row["turn_cutoff"] == len(grantor_turns), (
            f"the share record claims to hand over {row['turn_cutoff']} turns while serving "
            f"{len(grantor_turns)}: {row}")
        assert {d["id"] for d in row["scope"]} == {A_ID}, (
            f"the listed scope still names the removed document: {row}")
    finally:
        _cleanup(conv, link_principal(_share_record(link["share_id"])))


def test_narrowing_a_people_share_cuts_the_named_grantee_off_too():
    """RESPONSE. The same PATCH, the other audience: Alice signs in and reads Bob's thread
    through her own session, and the removal must reach her transcript as well. One route, one
    mechanism - a narrowing that only worked on links would be a second implementation waiting
    to drift."""
    _seed_one_partition()
    conv = "c-607-people-narrow"
    _two_turn_thread(conv)
    people = _make_people(conv)
    try:
        before = client.get(f"/conversations/{conv}/transcript", cookies=CK[ALICE])
        assert before.status_code == 200 and B_MARKER in before.text, (
            f"control: the grantee must read the Lisbon turn first: {before.text[:400]}")

        assert _patch(people["share_id"], {"remove_docs": [B_ID]}).status_code == 200

        after = client.get(f"/conversations/{conv}/transcript", cookies=CK[ALICE])
        assert after.status_code == 200, after.text[:300]
        assert B_MARKER not in after.text, (
            f"a named grantee still reads the removed document's turn: {after.text[:500]}")
        assert A_MARKER in after.text, f"the whole share died instead: {after.text[:400]}"
    finally:
        _cleanup(conv, ALICE)


def test_narrowing_to_nothing_is_refused_whole_and_changes_nothing():
    """RESPONSE. A share that grants nothing but still lists as live is "looks revoked, isn't"
    inverted: the owner reads a live row and believes the recipient still has what it says,
    while the recipient gets a 404. So it is refused, with copy that tells her to revoke - and
    the refusal is WHOLE, which is checked by reading the share afterwards: a route that
    revoked as it went and discovered the emptiness later would have destroyed the share while
    answering 400."""
    _seed_one_partition()
    conv = "c-607-empty"
    _two_turn_thread(conv)
    link = _make_link(conv)
    try:
        r = _patch(link["share_id"], {"remove_docs": [A_ID, B_ID]})
        assert r.status_code == 400, (
            f"narrowing a share down to nothing was performed rather than refused: "
            f"{r.status_code} {r.text[:300]}")
        assert "revoke" in r.json()["detail"].lower(), (
            f"the refusal does not tell the owner what to do instead: {r.json()}")

        # Removing only the FIRST turn's document empties it just as surely - the shared half is
        # a contiguous prefix, so losing turn 0 loses everything after it.
        first = _patch(link["share_id"], {"remove_docs": [A_ID]})
        assert first.status_code == 400, (
            f"removing the leading turn's document left a share opening nothing: {first.text}")

        v = TestClient(app)
        still = v.get(link["url"] + "/transcript")
        assert still.status_code == 200, (
            f"the refused narrowing damaged the share anyway: {still.status_code}")
        assert A_MARKER in still.text and B_MARKER in still.text, (
            f"the refused narrowing removed documents on its way to the 400: {still.text[:400]}")
        row = _row(_mine().json(), link["share_id"])
        assert {d["id"] for d in row["scope"]} == {A_ID, B_ID}, row
        assert row["turn_cutoff"] == 2, row
    finally:
        _cleanup(conv, link_principal(_share_record(link["share_id"])))


def test_a_non_owner_gets_the_same_404_as_a_share_that_never_existed():
    """RESPONSE. `find(share_id, requester_oid)` raises ONE KeyError for both "no such share"
    and "not yours", so the two must be indistinguishable from outside - otherwise a caller can
    enumerate other people's share ids by reading which flavour of refusal comes back."""
    _seed_one_partition()
    conv = "c-607-notyours"
    _two_turn_thread(conv)
    link = _make_link(conv)
    try:
        theirs = _patch(link["share_id"], {"remove_docs": [B_ID]}, who=ALICE)
        assert theirs.status_code == 404, (
            f"somebody else narrowed a share they did not make: {theirs.status_code} "
            f"{theirs.text[:300]}")
        nonexistent = _patch("no-such-share-607", {"remove_docs": [B_ID]}, who=ALICE)
        assert nonexistent.status_code == theirs.status_code, (
            "'not yours' and 'never existed' answer differently - the 404 is an oracle")
        assert nonexistent.json() == theirs.json(), (nonexistent.text, theirs.text)

        anon = TestClient(app).patch(f"/shares/{link['share_id']}/scope",
                                     json={"remove_docs": [B_ID]})
        assert anon.status_code == 401, (
            f"an anonymous caller reached the narrowing route: {anon.status_code}")

        # ...and nothing happened to the share, which is what makes the 404 a refusal rather
        # than a refusal-shaped response over a completed mutation.
        row = _row(_mine().json(), link["share_id"])
        assert {d["id"] for d in row["scope"]} == {A_ID, B_ID}, (
            f"a non-owner's PATCH narrowed the share anyway: {row}")
    finally:
        _cleanup(conv, link_principal(_share_record(link["share_id"])))


def test_an_unknown_document_id_subtracts_nothing_and_is_not_an_error():
    """RESPONSE. Identical to `exclude_docs`'s rule and for the identical reason: an id that
    errored would make this parameter a PROBE - a caller could pass a document id and learn,
    from which status came back, whether it exists inside a share. The TYPE is still refused,
    because a bare string iterates as characters and would remove nothing while reporting
    success, which is a narrowing the owner believes happened and did not."""
    _seed_one_partition()
    conv = "c-607-unknown"
    _two_turn_thread(conv)
    link = _make_link(conv)
    try:
        r = _patch(link["share_id"], {"remove_docs": ["doc-that-is-not-here"]})
        assert r.status_code == 200, (
            f"an unknown document id errored, which makes remove_docs a probe: {r.text[:300]}")
        assert r.json()["removed"] == 0 and r.json()["documents"] == 2, r.json()
        row = _row(_mine().json(), link["share_id"])
        assert {d["id"] for d in row["scope"]} == {A_ID, B_ID}, row

        empty = _patch(link["share_id"], {"remove_docs": []})
        assert empty.status_code == 200 and empty.json()["removed"] == 0, empty.text[:300]

        bad = _patch(link["share_id"], {"remove_docs": B_ID})
        assert bad.status_code == 400, (
            f"a bare string was accepted and silently removed nothing: {bad.status_code}")
    finally:
        _cleanup(conv, link_principal(_share_record(link["share_id"])))


def test_there_is_no_add_key_and_a_body_that_tries_to_widen_is_refused():
    """RESPONSE. REMOVE ONLY is a property of the API, not a rule somebody enforces: what a
    caller sends is subtracted from a set the route computed out of grant records, so nothing
    here can put a document INTO a share. A body that names an add key is refused rather than
    ignored - it names no document, so it is no oracle, and a caller sending it believes a
    widening happened."""
    _seed_one_partition()
    conv = "c-607-noadd"
    _two_turn_thread(conv)
    link = _make_link(conv)
    third = _ingest("doc-607-krakow", "The Krakow office relocation budget is 4 million.")
    try:
        for key in ("add_docs", "add", "include_docs"):
            r = _patch(link["share_id"], {key: [third], "remove_docs": []})
            assert r.status_code == 400, (
                f"the PATCH accepted a widening key {key!r}: {r.status_code} {r.text[:300]}")
        row = _row(_mine().json(), link["share_id"])
        assert {d["id"] for d in row["scope"]} == {A_ID, B_ID}, (
            f"a document was added to a live share through the narrowing route: {row}")

        v = TestClient(app)
        answered = v.post(link["url"] + "/chat",
                          json={"question": "what is the Krakow relocation budget?"})
        assert answered.status_code == 200, answered.text[:300]
        assert "4 million" not in answered.json()["answer"], (
            f"a document outside the share reached a visitor: {answered.json()['answer'][:300]}")
    finally:
        _cleanup(conv, link_principal(_share_record(link["share_id"])))


def test_a_revoked_share_leaves_the_listing():
    """RESPONSE. The section's Revoke is the existing DELETE, and the listing must agree with
    it immediately - a row that survives a revoke is the "looks revoked, isn't" confusion on the
    surface built to prevent it."""
    _seed_one_partition()
    conv = "c-607-revoked"
    _two_turn_thread(conv)
    link = _make_link(conv)
    assert link["share_id"] in _mine().text, "control"
    gone = client.delete(f"/conversations/shares/{link['share_id']}", cookies=CK[BOB])
    assert gone.status_code == 200, gone.text[:300]
    body = _mine()
    assert link["share_id"] not in body.text, (
        f"a revoked share is still listed as live: {body.text[:400]}")


# ---- RESPONSE: the module that carries the section actually reaches a user ------------------

def test_the_admin_shell_loads_the_module_that_carries_the_shared_section():
    """RESPONSE. The chain, through responses. A section built in a module nothing imports is
    exactly the Task 6 failure, and no amount of grepping the file would show it."""
    shell = client.get("/admin")
    assert shell.status_code == 200, shell.status_code
    assert 'src="/static/js/main.js?v=' in shell.text, \
        "the admin shell no longer loads a versioned main.js"
    main = client.get("/static/js/main.js")
    assert main.status_code == 200 and "./router.js" in main.text
    router = client.get("/static/js/router.js")
    assert router.status_code == 200, router.status_code
    assert "./surfaces/admin.js" in router.text and "mountAdmin" in router.text, \
        "the router no longer mounts the admin surface, so the Shared section is unreachable"
    admin = client.get("/static/js/surfaces/admin.js")
    assert admin.status_code == 200, admin.status_code
    assert "/shares/mine" in client.get("/static/js/api.js").text, \
        "api.js has no client for the listing route, so the section has nothing to render"
    css = client.get("/static/css/app.css")
    assert css.status_code == 200 and ".shared-row" in css.text, \
        "the Shared rows have no styles - they would render as unstyled markup"


def test_the_served_modules_parse():
    node = subprocess.run(["which", "node"], capture_output=True, text=True)
    if node.returncode != 0:
        print("      (node not installed - skipping the parse check)")
        return
    for path in ("/static/js/surfaces/admin.js", "/static/js/surfaces/ask.js",
                 "/static/js/api.js"):
        src = client.get(path).text
        r = subprocess.run(["node", "--check", "--input-type=module", "-"],
                           input=src, capture_output=True, text=True)
        assert r.returncode == 0, f"the served {path} does not parse: {r.stderr[:300]}"


# ---- DOM: the section, mounted and driven ---------------------------------------------------

ADMIN_PATH = ROOT / "src/dbsearch/server/static/js/surfaces/admin.js"
JSDOM = _domgate.JSDOM
PROBE = ROOT / "tests/shared_surface_dom_probe.mjs"
_dom = {}


def _report():
    """Mount "Your data" in a real DOM, read the Shared rows, click Edit, uncheck, Save.

    Returns None only when node or jsdom is unavailable AND `DBSEARCH_ALLOW_DOM_SKIP=1` was set;
    without that opt-out missing tooling FAILS here rather than passing silently (#792).

    #792 also fixed the older shape this carried: the cache was seeded with None BEFORE the
    assert, so a probe crash raised once and every later caller then read that None, printed
    "skipping" and reported ok. The crash is now cached as an exception and re-raised per
    caller, which is the rule selftest_602 earned."""
    if "r" not in _dom:
        if not _domgate.gate("the Shared-section DOM check"):
            _dom["r"] = None                           # permitted skip, already counted
        else:
            _dom["r"] = _domgate.run_node(
                ["node", str(PROBE), str(JSDOM), str(ADMIN_PATH)], "the Shared section")
    return _domgate.resolve(_dom["r"])


def _skip_dom():
    """The DOM half did not run. `_report` has already printed and counted why."""
    return True


def test_dom_the_served_module_is_the_one_the_probe_drives():
    served = client.get("/static/js/surfaces/admin.js").text
    assert served == ADMIN_PATH.read_text(), \
        "the served admin.js differs from the file on disk - the DOM probe proves nothing"


def test_dom_the_shared_section_renders_one_row_per_share_with_every_field():
    """DOM. The spec's row, read out of the mounted page: a name, who it went to, when and for
    how long, how many documents, how many questions, and the three actions."""
    r = _report()
    if r is None: return _skip_dom()
    assert "Shared" in r["section_title"], \
        f"there is no Shared section on the page at all: {r['section_title']}"
    rows = r["rows"]
    assert len(rows) == 2, f"one row per share, both audiences: {rows}"
    people, link = rows
    for row in rows:
        assert "severance" in row["name"], f"the row has no human name: {row}"
        assert row["scope"] == "2 documents", row
        assert row["buttons"] == ["Edit", "View", "Revoke"], (
            f"the row does not offer exactly edit, view and revoke: {row}")
    assert people["audience"] == "acct_beef", (
        f"a people row must NAME its recipient (#603 prints the raw id, carded): {people}")
    assert link["audience"] == "Anyone with the link", (
        f"a link row is named after what it is - there is nobody to name: {link}")
    assert "opened 4 times" in link["when"], (
        f"a link row does not report how much it has been used: {link['when']}")
    assert "last " in link["when"], f"a link row does not say when it was last opened: {link}"
    assert "expires" in link["when"], link["when"]
    assert "opened" not in people["when"], (
        f"a people row reports open counts it has no visitor to count: {people['when']}")
    assert people["asked"] == "asked 0 questions" and link["asked"] == "asked 2 questions", rows


def test_dom_view_opens_the_question_log_under_the_row():
    """DOM. Task 6's route, rendered where the owner is - questions and visitor ordinals, and
    no answer text, because the route does not return any."""
    r = _report()
    if r is None: return _skip_dom()
    assert "VISITOR-Q1" in r["log_text"] and "VISITOR-Q2" in r["log_text"], \
        f"the question log did not render: {r['log_text']!r}"
    assert "visitor 1" in r["log_text"] and "visitor 2" in r["log_text"], \
        f"the log does not tell two strangers apart: {r['log_text']!r}"


def test_dom_edit_reopens_the_share_modal_in_edit_mode_with_a_checklist_and_nothing_else():
    """DOM, AND THE STRUCTURAL ASSERTION OF THIS TASK on the client side.

    The dialog is the Task 7 modal, reopened in edit mode. Its controls are COUNTED: one
    checkbox per document the share currently opens, and nothing else. No audience picker, no
    email field, no expiry, and above all nothing that could put a document back - which is what
    makes "a share can only ever be narrowed" a fact about the DOM rather than a rule."""
    r = _report()
    if r is None: return _skip_dom()
    assert r["edit_modal_present"], "clicking Edit built no dialog at all"
    assert r["edit_title"] == "Edit what this share opens", r["edit_title"]
    assert sorted(r["edit_inputs"]) == ["checkbox", "checkbox"], (
        f"the edit dialog offers controls beyond the checklist: {r['edit_inputs']}")
    assert all(d["has_checkbox"] for d in r["edit_doc_rows"]), (
        f"a document this share opens cannot be unchecked: {r['edit_doc_rows']}")
    assert len(r["edit_doc_rows"]) == 2, r["edit_doc_rows"]
    for b in r["edit_buttons"]:
        assert not any(w in b.lower() for w in ("add", "+", "share with", "invite")), \
            f"a control in the edit dialog offers to widen the share: {b!r}"
    assert sorted(r["edit_buttons"]) == ["Close", "Save changes"], r["edit_buttons"]
    assert "Anyone with the link" in r["edit_text"], \
        "the edit dialog does not say who this share is for"
    assert "cannot put one back" in r["edit_text"], \
        "the owner is not told the dialog can only narrow"
    assert r["edit_count_line"] == "2 documents stay shared.", r["edit_count_line"]


def test_dom_unchecking_and_saving_sends_a_remove_only_patch():
    """DOM. The request that actually goes out, read off the stub: `remove_docs` carrying the
    unchecked id, and no key that could widen anything."""
    r = _report()
    if r is None: return _skip_dom()
    assert r["edit_count_after_uncheck"] == "1 document stays shared.", \
        r["edit_count_after_uncheck"]
    assert len(r["patched"]) == 1, f"Save sent {len(r['patched'])} requests: {r['patched']}"
    sent = r["patched"][0]
    assert sent["url"].endswith("/shares/s-link/scope"), sent
    assert sent["body"] == {"remove_docs": ["d-lis"]}, (
        f"the request Save sent is not a remove-only narrowing: {sent['body']}")
    assert r["reread_after_save"], (
        "the section did not re-read from the server after the narrowing - it would be showing "
        "the browser's own opinion of an authorization decision")


def test_dom_the_edit_dialog_keeps_the_promise_its_aria_modal_makes():
    """DOM. Review round 1, Finding 3: this dialog declared `aria-modal="true"` and implemented
    NEITHER Escape NOR a focus trap, because both live in `mountAsk`'s closure and opening the
    same panel from "Your data" inherited the markup and none of the behaviour. `aria-modal`
    tells a screen reader the rest of the page is inert; nothing enforces that, so the claim was
    false and a keyboard user could Shift+Tab straight out onto the row behind.

    Both are now wired from ui/modal.js, ONE definition shared with the Ask surface, and this is
    what pins it. WHAT IT DOES NOT PROVE, same honest limit selftest_606 records: jsdom has no
    native tab traversal, so it cannot show where focus would have gone without the trap. It
    proves the trap's own contract, which is the code that prevents the escape in a browser."""
    r = _report()
    if r is None: return _skip_dom()
    assert r["edit_aria_modal"] == "true", (
        "the dialog no longer declares aria-modal, so this test is pinning nothing")
    assert r["edit_focus_on_open"]["inside"], (
        f"opening the edit dialog left focus on the page behind it: {r['edit_focus_on_open']}")
    assert r["edit_shift_tab_landed_on_last"], (
        "Shift+Tab at the first control escaped the dialog instead of wrapping to the last")
    assert r["edit_tab_landed_on_first"], "Tab at the last control escaped the dialog"
    assert r["edit_focus_after_tab_from_outside"]["inside"], (
        f"focus parked behind the dialog was allowed to walk the page: "
        f"{r['edit_focus_after_tab_from_outside']}")
    assert r["edit_closed_on_escape"], (
        "Escape does not close the edit dialog - it can only be dismissed by finding its "
        "Close button, which is not what a modal promises")
    assert r["edit_survives_click_inside_panel"], (
        "a click inside the dialog closed it - the backdrop handler is not checking its target")
    assert r["edit_closed_on_backdrop_click"], "clicking the backdrop does not dismiss the dialog"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
