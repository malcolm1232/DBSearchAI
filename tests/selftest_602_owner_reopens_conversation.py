"""#602 - the owner's door back to her own conversation.

Conversations became durable in #596 and shareable in #600, and then the Ask surface minted a
FRESH conv_id on every page load. So after a reload the owner had NO way back into her own
thread: she could not reopen it, could not see its shares, could not press Remove. During the
260808 acceptance run the tester had to call the DELETE route by hand to finish the script.
The recipient of a share had a door (`/conversations/shared-with-me`); the person who OWNS the
data did not. The data was there and it was unreachable, which is the opposite of what the
owner asked for ("they login next time, their data is still there").

This file pins the door: `ConversationStore.conversations_for`, `GET /conversations/mine`, and
the "Your conversations" list on Ask whose rows reopen a thread through the SAME transcript
route a grantee already uses.

WHAT EVERY ASSERTION HERE IS MADE AGAINST. Two kinds, and the report names which:

  RESPONSE   what a real HTTP call through the real app RETURNS - the JSON this owner
             receives, and the JSON a DIFFERENT owner receives from the same route.
  DOM        the Ask surface MOUNTED in a real DOM (jsdom) and driven the way a person drives
             it: the rows that exist, the click, the thread that appears, the Share button,
             and the request the share modal then sends.

There is no third kind. Nothing here asserts that a string is present in a file on disk. Task 6
shipped four tests that grepped `static/js/surfaces/ask.js` and every one of them was green
while the page in question was never rendered to anybody; that is the failure shape this file
is written to be incapable of.

THE THREE THINGS THAT DECIDE WHETHER THIS IS RIGHT, and the tests that decide each:

  SCOPED TO THE CALLER.   History is keyed by (conv_id, user_oid). The list is an EQUALITY
                          match on user_oid, never a prefix match, and two tests hold that
                          from both directions: another account's threads are absent, and an
                          account whose oid is a strict PREFIX of another account's oid does
                          not inherit the longer one's threads. `users_for_conv` next door
                          matches by prefix on purpose, and copying that shape into this
                          method is the likeliest way this leaks.
  NO VISITOR FORKS.       A link share stores every stranger's turns under a synthetic
                          `link:<share_id>:<visitor_id>` oid IN THE OWNER'S OWN conv_id
                          (ADR 0021). A listing that grouped by conv_id alone, or matched
                          loosely, would put a stranger's typed question in the owner's list
                          under the owner's own thread. Driven with REAL visitor traffic
                          through the real `/c/{token}` doorway, not a hand-written oid.
  THE REOPENED THREAD     The acceptance criterion that failed in step 8 of the 260808 run:
  IS SHAREABLE.           after clicking a row the Share button must appear AND the modal must
                          operate on the reopened conv_id, not on the fresh one the page
                          minted at load. Asserted twice - once on the response
                          (`POST /conversations/{listed_id}/shares` succeeds and is backed by
                          the thread's documents) and once on the DOM (the URL the mounted
                          modal actually posts to).

    PYTHONPATH=src python3 tests/selftest_602_owner_reopens_conversation.py
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
# selftest_605/607/611's rig rule: the public demo's per-IP cap is installed at import time and
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
# DELIBERATELY a strict prefix-extension of BOB. `users_for_conv` in the same store matches by
# PREFIX (that is its contract), so the copy-paste hazard for this method is a `left()` or a
# `startswith` where an equality belongs - and this pair is the only rig shape that can tell
# those two apart. With a plain `acct_carol` here the prefix bug would pass every test.
BOB2 = "acct_bob_second"
HOME_TID = "tid-602-mine-home"
CK = {}

A_ID, A_MARKER = "doc-602-hamburg", "62 weeks"
B_ID, B_MARKER = "doc-602-lisbon", "26 days"
A_TEXT = f"The Hamburg severance clause in {A_ID} pays out over {A_MARKER} of salary."
B_TEXT = f"The Lisbon carryover allowance in {B_ID} carries over {B_MARKER} of unused leave."


def _seed_one_partition():
    """selftest_605/607/611's rig verbatim: every identity in the deployment's OWN partition,
    the single-org shape where a partition filter protects nobody from anybody - so nothing
    here can pass because of a tenant boundary a real self-host box does not have."""
    global CK
    for k in ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET"):
        os.environ.pop(k, None)
    os.environ.update({"AUTH_TENANT_ID": HOME_TID, "AUTH_CLIENT_ID": "cid",
                       "AUTH_CLIENT_SECRET": "sec"})
    for who, mail in ((ALICE, "alice@x.com"), (BOB, "bob@x.com"), (BOB2, "bob2@x.com")):
        ACCOUNTS.resolve("local", mail, preferred_account_id=who, email=mail)
    one = _edition.tenant_id
    CK = {who: {user_auth.COOKIE: user_auth.sign_session(
        {"oid": who, "tid": one, "exp": int(time.time()) + 3600})}
        for who in (ALICE, BOB, BOB2)}


def _ingest(doc_id: str, text: str, owner: str = BOB) -> str:
    r = client.post("/ingest", cookies=CK[owner], json={
        "external_id": doc_id, "title": f"policy {doc_id}", "acl": [owner],
        "text": text, "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, f"seed ingest failed: {r.status_code} {r.text[:200]}"
    return doc_id


def _turn(conv: str, question: str, answer: str, docs: list, who: str = BOB) -> None:
    _edition.conversation_service._store.append(conv, who, Turn(
        question=question, standalone=question, answer=answer, cited_docs=list(docs)))


def _mine(who: str = BOB):
    return client.get("/conversations/mine", cookies=CK[who])


def _row(body: dict, conv_id: str) -> dict:
    for c in body["conversations"]:
        if c["conv_id"] == conv_id:
            return c
    raise AssertionError(f"{conv_id} is not in /conversations/mine: {body}")


def _ids(body: dict) -> list:
    return [c["conv_id"] for c in body["conversations"]]


def _drop(*keys):
    """Take a (conv_id, oid) pair back out of the memory store, so one test's threads cannot
    show up in another's listing. The listing is per USER and the rig reuses BOB, so this is
    not optional bookkeeping - it is what keeps each assertion about the rows IT seeded."""
    store = _edition.conversation_service._store
    for conv, oid in keys:
        store._turns.pop((conv, oid), None)


def _cleanup(conv: str, *grantees: str):
    for g in grantees:
        try:
            _edition.grant_registry.drop_for_conversation(conv, g)
        except Exception:
            pass


# ---- RESPONSE: the listing ------------------------------------------------------------------

def test_two_threads_for_one_owner_list_newest_first_with_questions_and_counts():
    """RESPONSE. The whole card in one call: after two asks under one conv and one under
    another, the owner's own listing carries both, newest first, each named by ITS OWN opening
    question and counting ITS OWN turns.

    Newest-FIRST is the product decision, not a detail: the thread she wants back after a
    reload is almost always the one she was just in, and a list that put it last would leave the
    door open but out of reach on a long list."""
    _seed_one_partition()
    old, new = "c-602-old", "c-602-new"
    try:
        _turn(old, "OLD-Q1 how many weeks of severance in Hamburg?", "62 weeks.", [])
        _turn(old, "OLD-Q2 and for part timers?", "Pro rata.", [])
        time.sleep(0.01)                      # the stamps are the ordering; make them distinct
        _turn(new, "NEW-Q1 how much leave carries over in Lisbon?", "26 days.", [])

        r = _mine()
        assert r.status_code == 200, f"the owner cannot list her own threads: {r.text[:300]}"
        body = r.json()
        ids = _ids(body)
        assert old in ids and new in ids, f"a thread the owner owns is missing: {body}"
        assert ids.index(new) < ids.index(old), (
            f"the listing is not newest-first - the thread she was just in is buried: {ids}")

        a, b = _row(body, old), _row(body, new)
        assert a["first_question"].startswith("OLD-Q1"), (
            f"the row is named by the wrong turn of its own thread: {a}")
        assert b["first_question"].startswith("NEW-Q1"), (
            f"the row is named by another thread's question: {b}")
        assert "OLD-Q2" not in json.dumps(body), (
            f"the listing carries turns beyond the opening question: {body}")
        assert a["turns"] == 2 and b["turns"] == 1, (
            f"the turn counts are not this thread's own: {a} {b}")
        assert a["last_asked_at"] and b["last_asked_at"], (
            f"a row has no time on it, so 'newest first' is unverifiable to the owner: {body}")
    finally:
        _drop((old, BOB), (new, BOB))


def test_another_owner_sees_only_her_own_threads():
    """RESPONSE. The scoping, from the plain direction. Alice's list contains Alice's thread
    and NOT Bob's - and the assertion is on the serialized body, because the leak that matters
    is the QUESTION TEXT, which is Bob's own words about Bob's own documents."""
    _seed_one_partition()
    bobs, alices = "c-602-bobs", "c-602-alices"
    try:
        _turn(bobs, "BOB-SECRET what is the Hamburg severance?", "62 weeks.", [], who=BOB)
        _turn(alices, "ALICE-Q what is the Lisbon allowance?", "26 days.", [], who=ALICE)

        hers = _mine(ALICE)
        assert hers.status_code == 200, hers.text[:300]
        assert _ids(hers.json()) == [alices], (
            f"Alice's listing is not exactly her own threads: {hers.json()}")
        assert "BOB-SECRET" not in hers.text, (
            f"another owner's typed question reached this listing: {hers.text[:400]}")

        his = _mine(BOB)
        assert alices not in _ids(his.json()), (
            f"Bob's listing carries Alice's thread: {his.json()}")
        assert "ALICE-Q" not in his.text, f"a leak in the other direction: {his.text[:400]}"
    finally:
        _drop((bobs, BOB), (alices, ALICE))


def test_an_oid_that_is_a_prefix_of_another_account_inherits_nothing():
    """RESPONSE. The scoping, from the direction a copy-paste would break.

    `users_for_conv` sits directly above this method in the same store and matches by PREFIX -
    that is its contract, and it is how the visitor question log enumerates a share's forks. A
    `conversations_for` written by copying it would use `left(user_oid, n) = %s` or
    `startswith`, and every ordinary test would still pass, because ordinary account ids are not
    prefixes of one another. `acct_bob` IS a prefix of `acct_bob_second`, so this one would not.
    """
    _seed_one_partition()
    short, long_ = "c-602-short", "c-602-long"
    try:
        _turn(short, "SHORT-Q what does the handbook say?", "It says so.", [], who=BOB)
        _turn(long_, "LONG-Q what does the other handbook say?", "It says so too.", [],
              who=BOB2)

        his = _mine(BOB)
        assert long_ not in _ids(his.json()), (
            f"an account whose oid is a PREFIX of another's inherited their thread: "
            f"{his.json()}")
        assert "LONG-Q" not in his.text, (
            f"a longer-oid account's question text reached the shorter one: {his.text[:400]}")

        theirs = _mine(BOB2)
        assert _ids(theirs.json()) == [long_], theirs.json()
        assert "SHORT-Q" not in theirs.text, theirs.text[:400]
    finally:
        _drop((short, BOB), (long_, BOB2))


def test_a_links_visitor_forks_never_appear_in_the_owners_list():
    """RESPONSE, and the sharpest scoping test in the file, driven with REAL visitor traffic.

    ADR 0021: an anonymous visitor's turns land under a synthetic `link:<share_id>:<visitor_id>`
    oid INSIDE THE OWNER'S OWN conv_id - that is what keeps strangers out of each other's
    threads and out of hers. So the owner's own conversation is, in the store, a group of keys
    sharing one conv_id, only ONE of which is hers.

    Two distinct failures are in scope and both are asserted. A listing that grouped by conv_id
    alone would name the owner's row with whichever question came first ACROSS the group, which
    can be a stranger's; a listing that matched user_oid loosely would emit a separate row per
    fork. Either one puts a stranger's typed words on the owner's screen, and the second also
    tells her the fork key exists, which is a person-tracker (#611's LAW 1 finding).

    The count is checked too: the owner's row counts HER turns, and a visitor's questions must
    not inflate it into a thread she does not recognise."""
    _seed_one_partition()
    conv = "c-602-linked"
    _ingest(A_ID, A_TEXT)
    _turn(conv, "OWNER-Q how many weeks of severance in Hamburg?", f"It pays {A_MARKER}.",
          [A_ID])
    made = client.post(f"/conversations/{conv}/shares", cookies=CK[BOB],
                       json={"audience": AUDIENCE_LINK})
    assert made.status_code == 200, f"link share failed: {made.text[:300]}"
    made = made.json()
    url = made["url"]
    try:
        v1, v2 = TestClient(app), TestClient(app)
        assert v1.get(url).status_code == 200 and v2.get(url).status_code == 200
        assert v1.post(url + "/chat",
                       json={"question": "VISITOR-Q1 what is the severance?"}
                       ).status_code == 200
        assert v2.post(url + "/chat",
                       json={"question": "VISITOR-Q2 and the carryover?"}).status_code == 200

        # Control: the forks really do exist under the owner's conv_id, or the absence below
        # is an empty rig rather than a rule firing.
        forks = _edition.conversation_service._store.users_for_conv(conv, "link:")
        assert len(forks) == 2, f"control: the visitors' forks were not stored: {forks}"

        r = _mine(BOB)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        mine_rows = [c for c in body["conversations"] if c["conv_id"] == conv]
        assert len(mine_rows) == 1, (
            f"the owner's own conversation appears once per visitor fork: {body}")
        row = mine_rows[0]
        assert row["first_question"].startswith("OWNER-Q"), (
            f"a stranger's question is being shown to the owner as the name of HER thread: "
            f"{row}")
        assert "VISITOR-Q1" not in r.text and "VISITOR-Q2" not in r.text, (
            f"a stranger's typed question reached the owner's conversation list: {r.text[:500]}")
        assert "link:" not in r.text, (
            f"a visitor fork key reached an owner-facing response: {r.text[:500]}")
        assert row["turns"] == 1, (
            f"the owner's row counts strangers' questions as her own turns: {row}")
    finally:
        _cleanup(conv, link_principal(_edition.conversation_shares.find(made["share_id"], BOB)))
        for oid in _edition.conversation_service._store.users_for_conv(conv, "link:"):
            _drop((conv, oid))
        _drop((conv, BOB))


def test_a_received_thread_the_grantee_replied_in_lists_as_hers_marked_not_her_own():
    """RESPONSE, FIX ROUND 1. The docstring on this route used to claim a thread somebody shared
    WITH the caller could never appear in this list. That was FALSE, and a false claim in a
    docstring is how the next reader is told not to look.

    The moment a grantee asks a follow-up in a received thread her turn keys `(conv_id, HER
    oid)` - which is what makes the conv-scoped doorway work at all (ADR 0020) - so the store
    legitimately has a row for her. What is asserted here is that it is HER row and nothing
    more: her own question, her own count of one, not a word of the grantor's half; and that the
    row SAYS it is not hers (`own: false`) so the surface can reopen it as a share rather than
    as her own data."""
    _seed_one_partition()
    conv = "c-602-received"
    _ingest(A_ID, A_TEXT)
    _turn(conv, "GRANTOR-Q what is the Hamburg severance?", f"It pays {A_MARKER}.", [A_ID])
    made = client.post(f"/conversations/{conv}/shares", cookies=CK[BOB],
                       json={"grantee_oid": ALICE})
    assert made.status_code == 200, made.text[:300]
    try:
        # Her follow-up, through the REAL chat route, so the key really is the one the product
        # writes rather than one this test invented.
        reply = client.post("/chat", cookies=CK[ALICE], json={
            "conv_id": conv, "question": "GRANTEE-Q and for part timers?"})
        assert reply.status_code == 200, reply.text[:300]

        r = _mine(ALICE)
        assert r.status_code == 200, r.text[:300]
        row = _row(r.json(), conv)
        assert row["first_question"].startswith("GRANTEE-Q"), (
            f"the row is named by the GRANTOR's question, which is not hers to be shown here: "
            f"{row}")
        assert "GRANTOR-Q" not in r.text, (
            f"the grantor's half of the thread reached the grantee's listing: {r.text[:400]}")
        assert row["turns"] == 1, (
            f"the row counts the grantor's turns as hers: {row}")
        assert row["own"] is False, (
            f"a received thread is listed as one she owns, so reopening it disarms the "
            f"revoke detection on her next question: {row}")
        assert row["grantor_oid"] == BOB, (
            f"there is nobody to label the grantor's half of the transcript with: {row}")

        # ...and Bob's own row for the same conv_id is still his, named by HIS question.
        his = _row(_mine(BOB).json(), conv)
        assert his["own"] is True and his["first_question"].startswith("GRANTOR-Q"), his
        assert his["grantor_oid"] is None, (
            f"her own thread names a grantor - there is none: {his}")
    finally:
        _cleanup(conv, ALICE)
        _drop((conv, BOB), (conv, ALICE))


def test_a_thread_whose_share_has_been_revoked_goes_back_to_being_hers():
    """RESPONSE, FIX ROUND 1's other edge. `live_share_for` applies expiry and liveness per
    read, so `own` is not a stored fact - it is the answer right now. After a revoke the row
    stops being a share (it also stops being listed by /conversations/shared-with-me at the same
    moment), and what is left is her own turn, which really is hers."""
    _seed_one_partition()
    conv = "c-602-revoked-back"
    _ingest(A_ID, A_TEXT)
    _turn(conv, "GRANTOR-Q what is the Hamburg severance?", f"It pays {A_MARKER}.", [A_ID])
    made = client.post(f"/conversations/{conv}/shares", cookies=CK[BOB],
                       json={"grantee_oid": ALICE}).json()
    try:
        assert client.post("/chat", cookies=CK[ALICE], json={
            "conv_id": conv, "question": "GRANTEE-Q and for part timers?"}).status_code == 200
        assert _row(_mine(ALICE).json(), conv)["own"] is False, "control: it starts as a share"

        gone = client.delete(f"/conversations/shares/{made['share_id']}", cookies=CK[BOB])
        assert gone.status_code == 200, gone.text[:300]

        row = _row(_mine(ALICE).json(), conv)
        assert row["own"] is True and row["grantor_oid"] is None, (
            f"a revoked share still reads as live on this row, so the two lists disagree about "
            f"the same thread: {row}")
        assert conv not in [s["conv_id"] for s in
                            client.get("/conversations/shared-with-me",
                                       cookies=CK[ALICE]).json()["shares"]], \
            "control: the other list already stopped showing it"
    finally:
        _cleanup(conv, ALICE)
        _drop((conv, BOB), (conv, ALICE))


def test_the_opening_question_is_truncated_for_display():
    """RESPONSE. It IS content - the owner's own words handed back to the owner alone - and
    that is fine here. What is not fine is an unbounded paragraph as a row label, so it is cut
    at the same width `/shares/mine` cuts its own thread name at. One definition, because the
    two lists name the same thing."""
    _seed_one_partition()
    conv = "c-602-long-question"
    long_q = ("what does the handbook say about severance, notice periods, carryover leave, "
              "sabbaticals and the parental policy in every European office we run?")
    try:
        _turn(conv, long_q, "It says a lot.", [])
        row = _row(_mine().json(), conv)
        assert len(row["first_question"]) < len(long_q), (
            f"the row label is the whole untruncated question: {row['first_question']!r}")
        assert len(row["first_question"]) <= 80, (
            f"the row label is longer than the shared cut: {row['first_question']!r}")
        assert row["first_question"].startswith("what does the handbook say"), row
        assert row["first_question"].endswith("…"), (
            f"truncation is silent - the owner cannot tell the label was cut: {row}")
    finally:
        _drop((conv, BOB))


def test_the_listing_is_refused_to_a_caller_with_no_identity():
    """RESPONSE. There is no anonymous shape of this route: it is defined as "the caller's own
    questions", so a caller with no identity has no answer, not an empty one."""
    _seed_one_partition()
    r = client.get("/conversations/mine")
    assert r.status_code in (401, 403), (
        f"an unauthenticated caller got a conversation listing: {r.status_code} {r.text[:200]}")


def test_an_owner_with_no_threads_gets_an_empty_list_not_an_error():
    """RESPONSE. The first-run case, and the one a refactor breaks quietly."""
    _seed_one_partition()
    r = _mine(ALICE)
    assert r.status_code == 200, r.text[:300]
    assert r.json()["conversations"] == [], r.json()


# ---- RESPONSE: reopening, and sharing what was reopened ------------------------------------

def test_reopening_a_listed_conversation_returns_the_full_thread():
    """RESPONSE. The door itself: the conv_id off the listing, put straight into the EXISTING
    transcript route a grantee already uses, returns every turn with `own: true`. No new read
    path was invented for the owner - which is the point, because a second transcript route is
    a second place the authorization rules have to be right."""
    _seed_one_partition()
    conv = "c-602-reopen"
    try:
        _turn(conv, "REOPEN-Q1 how many weeks of severance?", f"It pays {A_MARKER}.", [])
        _turn(conv, "REOPEN-Q2 and how much leave carries over?", f"It carries {B_MARKER}.", [])

        listed = _row(_mine().json(), conv)
        r = client.get(f"/conversations/{listed['conv_id']}/transcript", cookies=CK[BOB])
        assert r.status_code == 200, (
            f"the id the listing just handed out does not open: {r.status_code} {r.text[:300]}")
        body = r.json()
        assert body["own"] is True, f"the owner is being told she is reading a share: {body}"
        qs = [t["question"] for t in body["turns"]]
        assert qs == ["REOPEN-Q1 how many weeks of severance?",
                      "REOPEN-Q2 and how much leave carries over?"], (
            f"the reopened thread is not the whole thread, in order: {qs}")
        assert all(t["own"] for t in body["turns"]), body
        assert A_MARKER in r.text and B_MARKER in r.text, (
            "the reopened thread lost its answers - the owner gets questions with no answers")
    finally:
        _drop((conv, BOB))


def test_the_reopened_thread_can_be_shared():
    """RESPONSE. THE ACCEPTANCE CRITERION THAT FAILED IN STEP 8 of the 260808 run.

    Before this card the Ask surface minted a fresh conv_id on load, so the Share button - when
    it appeared at all - operated on an EMPTY conversation, and the share was refused (400, zero
    turns) or minted against nothing. Here the id comes off the listing, and the share it mints
    is backed by the document the thread actually cited. Both halves are asserted, because a
    200 carrying `documents: 0` is the failure wearing a success code."""
    _seed_one_partition()
    conv = "c-602-shareable"
    _ingest(A_ID, A_TEXT)
    try:
        _turn(conv, "SHARE-Q what is the Hamburg severance?", f"It pays {A_MARKER}.", [A_ID])
        listed = _row(_mine().json(), conv)

        # Exactly what the modal asks for when it opens on the reopened id.
        scope = client.get(f"/conversations/{listed['conv_id']}/shareable", cookies=CK[BOB])
        assert scope.status_code == 200, scope.text[:300]
        assert [d["id"] for d in scope.json()["documents"]] == [A_ID], scope.json()

        r = client.post(f"/conversations/{listed['conv_id']}/shares", cookies=CK[BOB],
                        json={"grantee_oid": ALICE})
        assert r.status_code == 200, (
            f"the reopened thread cannot be shared - the card's whole point: {r.text[:300]}")
        assert r.json()["documents"] == 1, (
            f"the share was minted against nothing, which is the old bug with a 200 on it: "
            f"{r.json()}")

        # ...and the recipient really can read it, so "shareable" is not a claim about a row.
        got = client.get(f"/conversations/{conv}/transcript", cookies=CK[ALICE])
        assert got.status_code == 200 and "SHARE-Q" in got.text, (
            f"the share minted from the reopened thread opens nothing: {got.text[:300]}")
    finally:
        _cleanup(conv, ALICE)
        _drop((conv, BOB))


# ---- RESPONSE: the wiring the surface needs -------------------------------------------------

def test_the_shell_serves_the_ask_surface_and_its_client():
    """RESPONSE. Not a grep for the feature's copy - a check that the modules the DOM probe
    drives are the ones the app actually serves, so the DOM assertions below are about the
    shipped page."""
    _seed_one_partition()
    router = client.get("/static/js/router.js")
    assert router.status_code == 200 and "./surfaces/ask.js" in router.text \
        and "mountAsk" in router.text, "the router no longer mounts the Ask surface"
    api = client.get("/static/js/api.js")
    assert api.status_code == 200 and "/conversations/mine" in api.text, \
        "api.js has no client for the listing route, so the list has nothing to render"


def test_the_served_modules_parse():
    node = subprocess.run(["which", "node"], capture_output=True, text=True)
    if node.returncode != 0:
        print("      (node not installed - skipping the parse check)")
        return
    for path in ("/static/js/surfaces/ask.js", "/static/js/api.js"):
        src = client.get(path).text
        r = subprocess.run(["node", "--check", "--input-type=module", "-"],
                           input=src, capture_output=True, text=True)
        assert r.returncode == 0, f"the served {path} does not parse: {r.stderr[:300]}"


# ---- DOM: the list, mounted and clicked -----------------------------------------------------

ASK_PATH = ROOT / "src/dbsearch/server/static/js/surfaces/ask.js"
JSDOM = _domgate.JSDOM
PROBE = ROOT / "tests/own_conversations_dom_probe.mjs"
_dom = {}


def _report():
    """Mount Ask in a real DOM, read the "Your conversations" rows, click one, share it, and
    try to click one over the top of an uncopied one-time link.

    Returns None when node or jsdom is unavailable and the DOM assertions then skip - the
    stance selftest_606/607 take. A skipped DOM check is REPORTED, never silently counted.

    A PROBE THAT CRASHES IS NOT A SKIP, and the distinction is cached explicitly rather than
    collapsed into `None`. In selftest_607's shape the crash raises inside the FIRST caller and
    leaves the cache holding `None`, so every later DOM test then prints "jsdom unavailable" and
    PASSES - one real failure reported, five silently turned green. Here the error is cached and
    re-raised, so a broken surface fails every assertion that depends on it."""
    if "r" not in _dom:
        if not _domgate.gate("the own-conversations DOM check"):
            _dom["r"] = None                           # permitted skip, already counted
        else:
            _dom["r"] = _domgate.run_node(
                ["node", str(PROBE), str(JSDOM), str(ASK_PATH)], "the Ask surface")
    return _domgate.resolve(_dom["r"])


def _skip_dom():
    """The DOM half did not run. `_report` has already printed and counted why."""
    return True


def test_dom_the_served_module_is_the_one_the_probe_drives():
    served = client.get("/static/js/surfaces/ask.js").text
    assert served == ASK_PATH.read_text(), \
        "the served ask.js differs from the file on disk - the DOM probe proves nothing"


def test_dom_the_list_renders_a_row_per_conversation_with_its_question_and_count():
    """DOM. The list exists on the mounted page, under a heading that says whose it is, one row
    per thread, each showing the opening question and how many turns are in it."""
    r = _report()
    if r is None: return _skip_dom()
    assert r["list_present"], (
        "there is no conversation list on the Ask surface at all - the owner still has no "
        "door back to her own thread")
    # #631: the heading is the rail's own group label now. "Recents" is what a thread list is
    # called where it lives, beside "Workspace" and "Operate".
    assert "Recents" in r["list_heading"], r["list_heading"]
    rows = r["rows"]
    assert len(rows) == r["mine_count"], (
        f"one row per conversation: {r['mine_count']} in the fixture, "
        f"{len(rows)} rendered: {rows}")
    assert rows[0]["title"].startswith("how much leave carries over"), (
        f"the first row is not the newest thread: {rows}")
    assert rows[1]["title"].startswith("how many weeks of severance"), rows
    assert "2 questions" in rows[1]["meta"], (
        f"the row does not say how much is in the thread: {rows[1]}")
    assert "1 question" in rows[0]["meta"] and "1 questions" not in rows[0]["meta"], (
        f"the turn count is not written for a thread of one: {rows[0]}")


def test_dom_clicking_a_row_reopens_that_thread_through_the_transcript_route():
    """DOM. The click a person makes: the row is a control, it fetches the EXISTING transcript
    route for THAT id, and the thread on screen is the one that was clicked."""
    r = _report()
    if r is None: return _skip_dom()
    assert r["clicked_fetched"] == "/conversations/c-602-old/transcript", (
        f"clicking a row did not read the thread through the existing transcript path: "
        f"{r['clicked_fetched']}")
    assert "how many weeks of severance" in r["thread_after_click"], (
        f"the clicked thread did not render: {r['thread_after_click'][:200]!r}")
    assert "62 weeks" in r["thread_after_click"], (
        "the reopened thread shows questions with no answers")
    assert "Shared" not in r["thread_after_click"], (
        "the owner's own turns are labelled as somebody else's share")


def test_dom_the_reopened_thread_offers_share_and_the_modal_uses_its_id():
    """DOM, AND THE ACCEPTANCE CRITERION, on the client side.

    A Share button that appears is not enough: before this card the button operated on the
    conv_id the page minted at load, so it shared an empty conversation. What is asserted is
    the URL the mounted modal actually reads and posts to - it must carry the id off the row,
    never the fresh one."""
    r = _report()
    if r is None: return _skip_dom()
    assert r["share_visible_after_click"], (
        "the reopened thread offers no Share button - the 260808 acceptance run failed here")
    assert r["share_modal_present_after_click"], "the Share button opened no modal"
    assert r["share_modal_conv_id"] == "c-602-old", (
        f"the share modal is operating on a conversation id that is not the reopened one: "
        f"{r['share_modal_conv_id']}")
    assert r["shared_posted_to"] == "/conversations/c-602-old/shares", (
        f"the share was minted against the wrong conversation: {r['shared_posted_to']}")


def test_dom_clicking_a_row_does_not_silently_destroy_an_uncopied_one_time_link():
    """DOM, AND THE CRITICAL REGRESSION THIS BRANCH ALREADY FIXED ONCE.

    `dismissShareModal` is the ONE teardown and it returns false while the modal is holding a
    link the owner has not copied. Two navigations already respect that - "New conversation"
    and opening a thread somebody shared with you - because both used to destroy the token
    silently, and the API returns it exactly once. A row in this new list is a THIRD navigation
    and it is guarded the same way: the first click warns and changes nothing, the second goes
    through.

    Both halves are asserted, because the dangerous half-fix is a click that leaves the modal up
    and navigates anyway - the same data loss with the dialog still on screen."""
    r = _report()
    if r is None: return _skip_dom()
    assert not r["guard_modal_closed_on_first_click"], (
        "clicking a conversation row destroyed an uncopied one-time link with no warning - "
        "the owner is left holding a live share whose URL is gone from the server")
    assert not r["guard_navigated_on_first_click"], (
        "the row navigated away underneath the warning, which is the same loss with the "
        "dialog still on screen")
    assert "not copied the link yet" in r["guard_note"], (
        f"the owner was given no reason for the refusal: {r['guard_note']!r}")
    assert r["guard_modal_closed_on_second_click"], (
        "a second click does not go through - the guard is a trap, not a confirmation")
    assert r["guard_navigated_on_second_click"], (
        "the second click closed the modal but never opened the thread")


def test_dom_reopening_a_received_thread_keeps_it_a_share():
    """DOM, FIX ROUND 1, AND THE BEHAVIOURAL HALF OF THE FINDING.

    A row whose `own` is false is a thread somebody shared WITH her that she has replied in.
    Reopening it from THIS list must set `sharedConv`, because `sharedConv` is the whole of
    #600's revoke detection in `submit` - and reopening with it false meant a grantee whose
    share had since been revoked was answered with "This conversation is no longer here. Start a
    new one", which is owner-data language shown to somebody whose SHARE ended. Wrong words,
    wrong mental model, and it told her she had lost her own data.

    Both directions are asserted: the share-ended sentence must appear AND the owner-data one
    must not. Asserting only the first would pass on a page showing both."""
    r = _report()
    if r is None: return _skip_dom()
    assert r["received_row_present"], "a thread she was given and replied in has no row at all"
    assert "shared with you" in r["received_row_meta"], (
        f"the row does not say the thread is not hers: {r['received_row_meta']!r}")
    assert "GRANTORQ" in r["received_thread_text"], (
        "reopening a received thread lost the grantor's half of it")
    assert r["received_labels_the_grantor"], (
        f"the grantor's turns are labelled with the reader's own name: "
        f"{r['received_thread_text'][:200]!r}")
    assert r["received_says_share_ended"], (
        f"after a revoke the grantee was not told her SHARE ended: "
        f"{r['received_after_revoke_text'][-300:]!r}")
    assert not r["received_says_owner_data_gone"], (
        "the grantee was told her own conversation is gone - owner-data language for somebody "
        "whose share was revoked by somebody else")


def test_dom_the_list_refreshes_after_an_answer():
    """DOM. A thread the owner has only just started must appear in the list without a reload -
    otherwise the door exists for old conversations and not for the one she is in."""
    r = _report()
    if r is None: return _skip_dom()
    assert r["list_reread_after_answer"], (
        "the list was not re-read after an answer, so a brand-new thread has no door until "
        "the page is reloaded")


def test_dom_861_a_reopened_routed_turn_keeps_each_rows_own_number():
    """#861: the reopened rail renumbered its survivors, so a marker opened the wrong row.

    A routed turn stores citations INTERLEAVED - proof, document, document, proof - and the
    answer's [n] markers are written against that full list. The rail can only render the
    proof rows, so the documents between them vanish from it. The number each survivor
    carries must still be its position in the list the answer was numbered against.

    MEASURED ON PROD (260820) before the fix, on a turn asked minutes earlier:

        answer  "- Singapore 137[1]  - London 92[4]  - Berlin 78[7]  - Austin 65[10]"
        rail    [1] [2] [3] [4]

    So [4] opened the row that says AUSTIN under an answer that says London, and [7] and [10]
    resolved to nothing. A dangling marker looks broken; a moved one looks sourced, which is
    why this is the worse half. #855's sentence, at a fourth home: "removing row n silently
    renumbers every later marker... A row that has moved is a lie."

    The LIVE path never had this - router_api numbers footnotes over ALL evidence and #859
    filters that list while each row keeps its own `n`. Only the reopen path renumbered."""
    r = _report()
    assert r["gap_row_present"], "the #861 fixture row is missing from the list"
    assert r["gap_source_numbers"] == ["[1]", "[4]"], (
        f"survivors must keep their own numbers, got {r['gap_source_numbers']} "
        "- [1], [2] is the pre-fix renumbering")
    # The number and the content must agree, which is the actual claim. A list that merely
    # LOOKS right ([1], [4]) while [4] holds London's row would pass a count assertion and
    # still lie to the reader.
    rows = {row["num"]: row["text"] for row in r["gap_rows"]}
    assert "London" in rows["[1]"], rows
    assert "Berlin" in rows["[4]"], rows


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
