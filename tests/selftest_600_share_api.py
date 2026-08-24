"""#600 Task 5: the share / list / revoke / transcript / shared-with-me API.

This is the task that turns four separate mechanisms (durable conversations, per-turn
citations, conv-scoped grants, the share record) into one product feature, so every test
here drives the REAL FastAPI routes with two real account partitions and real cookies -
the same client/bootstrap fixture tests/selftest_600_conv_scoped_grants.py uses.

The story under test is GOAL_ACCEPTANCE steps 4-8: bob shares an HR thread with alice,
she reads it and keeps asking her OWN questions answered from exactly the documents that
thread cited, she gains NO general access to bob's files, and bob can take it back.

    PYTHONPATH=src python3 tests/selftest_600_share_api.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")
# The public demo's per-IP cap (30 requests/minute) is installed at import time and every
# test here is several real HTTP calls, so without this the file starts returning 429s
# partway through and every later test fails on its own seed ingest - a rig failure that
# reads exactly like a regression. tests/e2e_router_api.py does the same, same reason.
os.environ["DBSEARCH_RATE_LIMIT"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.api.auth import ACCT_TENANT_PREFIX  # noqa: E402
from dbsearch.query.conversation import Turn  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.conversation_store import ConversationStoreUnavailable  # noqa: E402
from dbsearch.server.app import ACCOUNTS, app, _edition  # noqa: E402

client = TestClient(app)

ALICE = "acct_alice"
BOB = "acct_bob"
CAROL = "acct_carol"                          # a stranger, in a third partition
BOB_PARTITION = ACCT_TENANT_PREFIX + BOB      # ADR 0018: the local account's own partition
HOME_TID = "tid-600-share-home"
_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET")


def _real_login():
    """Real-login mode, which is what makes each local account resolve to its own private
    `acct:<oid>` partition (ADR 0018) instead of the dev-rig deployment constant. Without
    it every identity shares one partition and the cross-partition half of this feature
    cannot be exercised at all."""
    for k in _VARS:
        os.environ.pop(k, None)
    os.environ.update({"AUTH_TENANT_ID": HOME_TID, "AUTH_CLIENT_ID": "cid",
                       "AUTH_CLIENT_SECRET": "sec"})


def _cookie(oid: str, tid: str = "") -> dict:
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": tid, "exp": int(time.time()) + 3600})}


def _seed_accounts():
    ACCOUNTS.resolve("local", "alice@x.com", preferred_account_id=ALICE, email="alice@x.com")
    ACCOUNTS.resolve("local", "bob@x.com", preferred_account_id=BOB, email="bob@x.com")
    ACCOUNTS.resolve("local", "carol@x.com", preferred_account_id=CAROL, email="carol@x.com")


def _secret(doc_id: str) -> str:
    """Body text naming its own document, so an assertion can always ask about a SPECIFIC
    one (selftest_582_share_across_partitions.py's `_secret`, same shape and same reason).

    Note what this does NOT buy, because assuming otherwise is the run-long shared-state
    trap that file documents: the index lives for the whole run and these sentences are near
    identical, so a question about one document routinely retrieves its siblings too. Every
    assertion here is therefore written against the ACTUAL cited set a response reports
    ("the grants are exactly what this thread cited"), never against a set this file
    predicted retrieval would return."""
    return f"The Hamburg severance clause in {doc_id} pays out over 62 weeks of salary."


def _ingest(doc_id: str, owner: str = BOB) -> str:
    r = client.post("/ingest", cookies=_cookie(owner), json={
        "external_id": doc_id, "title": f"hr policy {doc_id}", "text": _secret(doc_id),
        "acl": [owner], "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, f"seed ingest failed: {r.status_code} {r.text[:200]}"
    return doc_id


def _chat(oid: str, conv_id: str, question: str) -> dict:
    r = client.post("/chat", cookies=_cookie(oid),
                    json={"conv_id": conv_id, "question": question})
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _ask_about(oid: str, conv_id: str, doc_id: str) -> dict:
    """One turn whose question names ONE document, so that document is certainly among what
    the turn cites - see `_secret` for why "certainly among" is the honest claim and "only"
    is not."""
    return _chat(oid, conv_id, f"how many weeks of severance in {doc_id} Hamburg clause?")


def _conv_grants(grantee: str, conv_id: str) -> set:
    return {g.doc_external_id for g in _edition.grant_registry.live_grants_for(grantee)
            if g.conv_id == conv_id}


def _cleanup(conv_id: str, *grantees: str):
    for g in grantees:
        try:
            _edition.grant_registry.drop_for_conversation(conv_id, g)
        except Exception:
            pass


# ---- the happy path: share exactly what the thread cited, and nothing wider -------------

def test_share_grants_exactly_the_cited_documents_conv_scoped():
    """GOAL_ACCEPTANCE steps 4-6. Bob's thread cites two documents; the share mints one
    conv-scoped grant PER CITED DOCUMENT and alice can then ask her own question inside that
    conversation and be answered from them - while the same documents stay invisible to her
    everywhere else (step 7: a conversation share is structurally incapable of widening into
    general document access, because /documents endpoints never declare a conversation)."""
    _real_login(); _seed_accounts()
    conv = "c-600-happy"
    d1, d2 = _ingest("doc-600s-hr-1"), _ingest("doc-600s-hr-2")

    cited = set(_ask_about(BOB, conv, d1)["retrieved_docs"])
    cited |= set(_ask_about(BOB, conv, d2)["retrieved_docs"])
    assert {d1, d2} <= cited, f"the seed thread did not cite both documents: {cited}"

    try:
        r = client.post(f"/conversations/{conv}/shares", cookies=_cookie(BOB),
                        json={"grantee_email": "alice@x.com"})
        assert r.status_code == 200, r.text[:300]
        share = r.json()
        assert share["conv_id"] == conv and share["grantee_oid"] == ALICE, share
        assert share["documents"] == len(cited), (
            f"{share['documents']} grants for {len(cited)} cited documents: {share}")
        # LAW 1: ids, counts, timestamps - never a document title or a word of the thread.
        assert not any(d in str(share) for d in ("severance", "Hamburg", "hr policy")), share

        assert _conv_grants(ALICE, conv) == cited, (
            f"grants {_conv_grants(ALICE, conv)} != cited {cited}")

        body = _chat(ALICE, conv, f"how many weeks of severance in {d1} Hamburg clause?")
        assert d1 in body["retrieved_docs"], (
            f"the recipient cannot ask from the shared thread's documents: {body}")

        # ...and the share is NOT general access to bob's files.
        assert client.get(f"/admin/documents/{d1}/download?form=text",
                          cookies=_cookie(ALICE)).status_code == 404, (
            "a conversation share widened into a document download")
        listed = {d["doc_external_id"] for d in
                  client.get("/admin/documents", cookies=_cookie(ALICE)).json()}
        assert d1 not in listed, "a conversation share widened into the 'Your data' listing"
    finally:
        _cleanup(conv, ALICE)


def test_a_padded_conv_id_from_the_path_still_matches_the_share_it_made():
    """Carried-forward review warning: `create` strips a conv_id while `_request_scope` and
    `drop_for_conversation` compare it RAW, so a route that forwarded an unnormalized path
    segment would mint a share that can never match anything and never raise. Normalization
    lives at the route boundary (`_conv_id`), and this drives it through the URL."""
    _real_login(); _seed_accounts()
    conv = "c-600-padded"
    d1 = _ingest("doc-600s-pad")
    _ask_about(BOB, conv, d1)
    try:
        r = client.post(f"/conversations/%20{conv}%20/shares", cookies=_cookie(BOB),
                        json={"grantee_email": "alice@x.com"})
        assert r.status_code == 200, r.text[:300]
        assert r.json()["conv_id"] == conv, r.json()
        body = _chat(ALICE, conv, f"how many weeks of severance in {d1} Hamburg clause?")
        assert d1 in body["retrieved_docs"], (
            f"a padded conv_id minted a share that opens nothing: {body}")
    finally:
        _cleanup(conv, ALICE)


# ---- the refusals -----------------------------------------------------------------------

def test_share_of_a_conversation_you_do_not_own_is_404():
    """History is keyed by (conv_id, user_oid), so somebody else's thread simply is not
    there to read - and the refusal is 404, never 403, or it confirms the thread exists to
    somebody who may not see it (the #549 rule)."""
    _real_login(); _seed_accounts()
    conv = "c-600-not-yours"
    _ask_about(BOB, conv, _ingest("doc-600s-notyours"))
    r = client.post(f"/conversations/{conv}/shares", cookies=_cookie(CAROL),
                    json={"grantee_email": "alice@x.com"})
    assert r.status_code == 404, f"{r.status_code} {r.text[:200]}"
    assert _conv_grants(ALICE, conv) == set(), "a refused share still minted grants"


def test_share_with_yourself_is_400():
    _real_login(); _seed_accounts()
    conv = "c-600-self"
    _ask_about(BOB, conv, _ingest("doc-600s-self"))
    r = client.post(f"/conversations/{conv}/shares", cookies=_cookie(BOB),
                    json={"grantee_email": "bob@x.com"})
    assert r.status_code == 400, f"{r.status_code} {r.text[:200]}"
    assert "already have access" in r.json()["detail"], r.json()


def test_share_of_a_conversation_citing_nothing_is_400():
    """A thread that cited nothing has nothing to share, and saying 200 would report a
    share that granted nobody anything. The turn is appended through the store directly:
    THAT row - a real turn with an empty `cited_docs` - is exactly what a question nothing
    matched leaves behind, and driving retrieval to reliably return nothing is not something
    a run-long shared in-memory index can promise."""
    _real_login(); _seed_accounts()
    conv = "c-600-nocites"
    _edition.conversation_service._store.append(conv, BOB, Turn(
        question="anything on the Reykjavik office?", standalone="Reykjavik office",
        answer="I could not find anything about that.", cited_docs=[]))
    r = client.post(f"/conversations/{conv}/shares", cookies=_cookie(BOB),
                    json={"grantee_email": "alice@x.com"})
    assert r.status_code == 400, f"{r.status_code} {r.text[:200]}"
    assert "cites no documents" in r.json()["detail"], r.json()
    assert _edition.conversation_shares.list_for_conversation(conv, BOB) == [], (
        "a refused share left a phantom share row behind")


def test_unknown_email_is_the_same_honest_400_as_grant_document():
    """The two share surfaces must never drift apart in what they tell a user, so this
    asserts the copy is IDENTICAL to `grant_document`'s rather than merely similar."""
    _real_login(); _seed_accounts()
    conv = "c-600-unknown"
    doc = _ingest("doc-600s-unknown")
    _ask_about(BOB, conv, doc)
    unknown = "nobody-600@x.com"
    conv_r = client.post(f"/conversations/{conv}/shares", cookies=_cookie(BOB),
                         json={"grantee_email": unknown})
    doc_r = client.post(f"/documents/{doc}/grants", cookies=_cookie(BOB),
                        json={"grantee_email": unknown})
    assert conv_r.status_code == 400 == doc_r.status_code, (conv_r.text[:200],
                                                            doc_r.text[:200])
    assert conv_r.json()["detail"] == doc_r.json()["detail"], (
        f"the two share surfaces answer the same question differently:\n"
        f"  conversation: {conv_r.json()['detail']}\n  document:     {doc_r.json()['detail']}")


def test_store_down_on_history_makes_share_503_never_an_empty_share():
    """#596: `history()` is deliberately unguarded. A caller deciding what a conversation
    CONTAINS must see a store outage as an error - reading it as an empty conversation would
    either mint a share of nothing and report success, or (as here) answer 404 "no such
    conversation" about a thread that exists and is theirs."""
    _real_login(); _seed_accounts()
    conv = "c-600-storedown"
    _ask_about(BOB, conv, _ingest("doc-600s-storedown"))

    real_history = _edition.conversation_service.history

    def _down(user_oid, conv_id):
        raise ConversationStoreUnavailable("PgConversationStore")

    _edition.conversation_service.history = _down
    try:
        r = client.post(f"/conversations/{conv}/shares", cookies=_cookie(BOB),
                        json={"grantee_email": "alice@x.com"})
        assert r.status_code == 503, (
            f"a store outage was reported as {r.status_code}: {r.text[:200]}")
        t = client.get(f"/conversations/{conv}/transcript", cookies=_cookie(BOB))
        assert t.status_code == 503, (
            f"a store outage read as an empty transcript: {t.status_code} {t.text[:200]}")
    finally:
        _edition.conversation_service.history = real_history
    assert _conv_grants(ALICE, conv) == set(), "a 503 share still minted grants"


# ---- reading the transcript --------------------------------------------------------------

def test_transcript_readable_by_owner_and_live_grantee_404_for_stranger():
    _real_login(); _seed_accounts()
    conv = "c-600-transcript"
    d1 = _ingest("doc-600s-transcript")
    q = f"how many weeks of severance in {d1} Hamburg clause?"
    _ask_about(BOB, conv, d1)
    try:
        assert client.post(f"/conversations/{conv}/shares", cookies=_cookie(BOB),
                           json={"grantee_email": "alice@x.com"}).status_code == 200

        own = client.get(f"/conversations/{conv}/transcript", cookies=_cookie(BOB))
        assert own.status_code == 200, own.text[:200]
        assert own.json()["own"] is True and len(own.json()["turns"]) == 1, own.json()

        theirs = client.get(f"/conversations/{conv}/transcript", cookies=_cookie(ALICE))
        assert theirs.status_code == 200, theirs.text[:200]
        body = theirs.json()
        assert body["own"] is False, body
        assert [t["question"] for t in body["turns"]] == [q], body
        assert body["turns"][0]["own"] is False, (
            "the grantor's half must be flagged so the surface can render it read-only")

        stranger = client.get(f"/conversations/{conv}/transcript", cookies=_cookie(CAROL))
        assert stranger.status_code == 404, (
            f"existence is the secret: {stranger.status_code} {stranger.text[:200]}")

        # She keeps asking her OWN questions in the same thread - and both halves survive.
        _chat(ALICE, conv, q)
        both = client.get(f"/conversations/{conv}/transcript", cookies=_cookie(ALICE)).json()
        assert [t["own"] for t in both["turns"]] == [False, True], (
            f"the recipient's own continuation replaced or hid the shared thread: {both}")
    finally:
        _cleanup(conv, ALICE)


def test_shared_with_me_lists_the_live_share():
    _real_login(); _seed_accounts()
    conv = "c-600-swm"
    _ask_about(BOB, conv, _ingest("doc-600s-swm"))
    try:
        made = client.post(f"/conversations/{conv}/shares", cookies=_cookie(BOB),
                           json={"grantee_email": "alice@x.com"})
        assert made.status_code == 200, made.text[:300]
        share_id = made.json()["share_id"]

        listed = client.get("/conversations/shared-with-me", cookies=_cookie(ALICE)).json()
        assert share_id in {s["share_id"] for s in listed["shares"]}, listed
        assert all(s["grantee_oid"] == ALICE for s in listed["shares"]), listed

        mine = client.get(f"/conversations/{conv}/shares", cookies=_cookie(BOB)).json()
        assert [s["share_id"] for s in mine["shares"]] == [share_id], mine
        # A share somebody else made is never listed as this caller's to manage.
        assert client.get(f"/conversations/{conv}/shares",
                          cookies=_cookie(CAROL)).json()["shares"] == []
        assert client.get("/conversations/shared-with-me",
                          cookies=_cookie(CAROL)).json()["shares"] == []
    finally:
        _cleanup(conv, ALICE)


# ---- taking it back ----------------------------------------------------------------------

def test_revoke_cascades_and_alices_next_chat_retrieves_nothing():
    """GOAL_ACCEPTANCE step 8. One DELETE takes back BOTH halves: the conv-scoped document
    grants (so her next question retrieves nothing of bob's) and the transcript row (so the
    grantor's half of the thread is gone from her transcript). Her OWN turns remain, which is
    the same direction `revoke_conversation_share` documents - the answers she was
    legitimately given are not retracted, only her access to bob's thread and documents."""
    _real_login(); _seed_accounts()
    conv = "c-600-revoke"
    d1 = _ingest("doc-600s-revoke")
    q = f"how many weeks of severance in {d1} Hamburg clause?"
    bob_turn = _ask_about(BOB, conv, d1)
    assert d1 in bob_turn["retrieved_docs"], bob_turn
    try:
        made = client.post(f"/conversations/{conv}/shares", cookies=_cookie(BOB),
                           json={"grantee_email": "alice@x.com"})
        assert made.status_code == 200, made.text[:300]
        share_id = made.json()["share_id"]

        before = _chat(ALICE, conv, q)
        assert d1 in before["retrieved_docs"], f"the share never opened: {before}"

        gone = client.delete(f"/conversations/shares/{share_id}", cookies=_cookie(BOB))
        assert gone.status_code == 200, gone.text[:200]
        assert gone.json()["grants_dropped"] >= 1, gone.json()

        after = _chat(ALICE, conv, q)
        assert d1 not in after["retrieved_docs"], (
            f"revoked, and she can still retrieve the document: {after}")

        t = client.get(f"/conversations/{conv}/transcript", cookies=_cookie(ALICE)).json()
        assert all(turn["own"] for turn in t["turns"]), (
            f"revoked, and the grantor's half of the transcript is still readable: {t}")
        assert t["own"] is True, t
        # Scoped to THIS conversation: other tests in this run leave live shares to alice
        # standing, and asserting the whole list is empty would pass or fail on their order.
        swm = client.get("/conversations/shared-with-me", cookies=_cookie(ALICE)).json()
        assert conv not in {s["conv_id"] for s in swm["shares"]}, swm
    finally:
        _cleanup(conv, ALICE)


def test_a_stranger_cannot_revoke_somebody_elses_share():
    """`drop_for_conversation` does NOT check who is asking - its docstring makes authorizing
    the caller a requirement on the route that wires it, which is `revoke_conversation_share`.
    Without that check, any signed-in caller who learned a share id (or, worse, a route that
    took conv_id/grantee_oid from the request) could un-share anybody's conversation."""
    _real_login(); _seed_accounts()
    conv = "c-600-stranger-revoke"
    d1 = _ingest("doc-600s-strangerrevoke")
    q = f"how many weeks of severance in {d1} Hamburg clause?"
    _ask_about(BOB, conv, d1)
    try:
        made = client.post(f"/conversations/{conv}/shares", cookies=_cookie(BOB),
                           json={"grantee_email": "alice@x.com"})
        assert made.status_code == 200, made.text[:300]
        share_id = made.json()["share_id"]

        for who in (CAROL, ALICE):        # a bystander, and the RECIPIENT herself
            r = client.delete(f"/conversations/shares/{share_id}", cookies=_cookie(who))
            assert r.status_code == 404, (
                f"{who} revoked a share they do not own: {r.status_code} {r.text[:200]}")

        still = _chat(ALICE, conv, q)
        assert d1 in still["retrieved_docs"], (
            f"a stranger's failed revoke still tore down the share: {still}")
        assert _edition.conversation_shares.live_share_for(conv, ALICE) is not None
    finally:
        _cleanup(conv, ALICE)


# =========================================================================================
# SINGLE-PARTITION RIG - the normal single-org Entra deployment
# =========================================================================================
# Everything above puts each identity in its own private `acct:` partition (ADR 0018), which
# is the right shape for the cross-account story #582 shipped and the WRONG shape to be the
# only rig this feature is tested in. `resolve_tenant` resolves every home-tenant session to
# the SAME partition, so in a normal one-org deployment colleagues share a partition - and
# both CRITICALs found in review round 1 were invisible in the acct: rig precisely because
# the partition filter was silently doing work that the missing authorization check should
# have been doing. Four identities, one partition, nothing between them but the rules.

DAVE = "acct_dave"
ONE = None          # the shared partition, resolved lazily from the edition below


def _seed_one_partition():
    """Four identities signing in with the HOME tenant tid, so `canonical_partition` puts
    all four in the deployment's own partition - a real org, where the partition filter
    protects nobody from anybody."""
    global ONE
    _real_login()
    _seed_accounts()
    ACCOUNTS.resolve("local", "dave@x.com", preferred_account_id=DAVE, email="dave@x.com")
    ONE = _edition.tenant_id
    return {who: {user_auth.COOKIE: user_auth.sign_session(
        {"oid": who, "tid": ONE, "exp": int(time.time()) + 3600})}
        for who in (ALICE, BOB, CAROL, DAVE)}


def _ingest_1p(doc_id: str, owner: str, ck: dict, text: "str | None" = None) -> str:
    r = client.post("/ingest", cookies=ck[owner], json={
        "external_id": doc_id, "title": f"policy {doc_id}", "acl": [owner],
        "text": text or _secret(doc_id), "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, f"seed ingest failed: {r.status_code} {r.text[:200]}"
    return doc_id


def test_one_partition_a_share_of_your_own_documents_still_works():
    """The positive control, and it is not optional: without it every refusal below could be
    passing because sharing is broken in this rig rather than because a rule fired."""
    ck = _seed_one_partition()
    conv = "c-1p-control"
    mine = _ingest_1p("doc-1p-mine", BOB, ck)
    r = client.post("/chat", cookies=ck[BOB], json={
        "conv_id": conv, "question": f"how many weeks of severance in {mine} Hamburg clause?"})
    assert r.status_code == 200 and mine in r.json()["retrieved_docs"], r.text[:300]
    try:
        made = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                           json={"grantee_email": "alice@x.com"})
        assert made.status_code == 200, made.text[:300]
        assert mine in _conv_grants(ALICE, conv), _conv_grants(ALICE, conv)
        got = client.post("/chat", cookies=ck[ALICE], json={
            "conv_id": conv,
            "question": f"how many weeks of severance in {mine} Hamburg clause?"})
        assert mine in got.json()["retrieved_docs"], got.text[:300]
    finally:
        _cleanup(conv, ALICE)


def test_one_partition_a_received_document_cannot_be_reshared_through_a_conversation():
    """CRITICAL-1, reproduced by the reviewer and pinned here.

    ADR 0017 s2: a share cannot be re-shared. `grant_document` enforces it through
    `_may_share`, which tests DIRECT principals with grant principals excluded. Sharing a
    CONVERSATION mints a grant per cited document and had no such test, so the rule held on
    one surface and not the other: carol grants bob a document, bob is refused when
    re-sharing the DOCUMENT and succeeded when sharing the CONVERSATION, and carol - the
    owner - could then see grants on her own document that she could not revoke, because
    `revoke_grant` requires `granted_by == requester`. Unbounded, each hop minting a new
    grantor."""
    ck = _seed_one_partition()
    conv = "c-1p-reshare"
    doc = _ingest_1p("doc-1p-carols", CAROL, ck,
                     text="The Valparaiso ferry subsidy in doc-1p-carols covers 19 crossings.")
    q = "how many crossings does the Valparaiso ferry subsidy cover?"

    # Carol shares the DOCUMENT with bob. Entirely legitimate.
    assert client.post(f"/documents/{doc}/grants", cookies=ck[CAROL],
                       json={"grantee_oid": BOB}).status_code == 200
    r = client.post("/chat", cookies=ck[BOB], json={"conv_id": conv, "question": q})
    assert doc in r.json()["retrieved_docs"], f"setup: bob never cited carol's doc: {r.text[:300]}"

    # The thread whose share is under test cites EXACTLY carol's document, appended the way a
    # real turn arrives (see test_share_of_a_conversation_citing_nothing_is_400 for why the
    # store is the honest way to pin a citation set: retrieval is fuzzy and the run-long index
    # would otherwise let a sibling document decide what this test proves).
    _edition.conversation_service._store.append(conv, BOB, Turn(
        question=q, standalone=q, answer="19 crossings.", cited_docs=[doc]))

    # CONTROL: the document surface refuses the re-share, and must keep doing so.
    assert client.post(f"/documents/{doc}/grants", cookies=ck[BOB],
                       json={"grantee_oid": ALICE}).status_code == 404, (
        "the document surface stopped enforcing ADR 0017 s2")
    try:
        r = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                        json={"grantee_oid": ALICE})
        assert r.status_code == 400, (
            f"a conversation share re-shared a document bob only holds via a grant: "
            f"{r.status_code} {r.text[:300]}")
        assert doc not in _conv_grants(ALICE, conv), _conv_grants(ALICE, conv)
        assert _edition.conversation_shares.live_share_for(conv, ALICE) is None, (
            "a refused share left a live transcript row behind")

        got = client.post("/chat", cookies=ck[ALICE], json={"conv_id": conv, "question": q})
        assert doc not in got.json()["retrieved_docs"], (
            f"carol's document reached alice, who was never granted it by its owner: "
            f"{got.text[:300]}")
        assert "19 crossings" not in got.text, got.text[:300]

        # The escalation the reviewer demonstrated: alice re-sharing onward to dave.
        assert client.post(f"/conversations/{conv}/shares", cookies=ck[ALICE],
                           json={"grantee_oid": DAVE}).status_code in (400, 404)
        dave = client.post("/chat", cookies=ck[DAVE], json={"conv_id": conv, "question": q})
        assert doc not in dave.json()["retrieved_docs"], dave.text[:300]

        # The owner keeps control: exactly ONE grant on her document, and it is hers to
        # revoke. Under the bug she saw three and could revoke none, because `revoke_grant`
        # requires `granted_by == requester` and each hop minted a new grantor.
        on_doc = client.get(f"/documents/{doc}/grants", cookies=ck[CAROL]).json()
        assert [g["granted_by"] for g in on_doc] == [CAROL], (
            f"somebody other than the owner is minting grants on her document: {on_doc}")
        assert client.delete(f"/grants/{on_doc[0]['grant_id']}",
                             cookies=ck[CAROL]).status_code == 200, (
            "the owner cannot revoke a grant on her own document")
    finally:
        _cleanup(conv, ALICE, DAVE)


def test_one_partition_a_turn_drawing_on_a_received_document_is_withheld_not_just_ungranted():
    """NEW-1, reproduced by the reviewer and pinned here - and the assertion is on TEXT.

    The round-1 version of this test asserted only the GRANT SET, which is exactly why it
    passed while the bug was live: filtering the grants closed the retrieval channel and left
    the content channel open, so a thread citing one of bob's own documents AND one carol
    shared with him shared "successfully" with `documents: 1` while the transcript handed
    alice BOTH turns - including the answer synthesized verbatim out of carol's document.
    Strictly worse for carol than the bug it replaced: no grant is minted, so she sees no
    trace of it at all.

    A turn travels only if every document it drew on is shareable. The rest are dropped, not
    refused, and counted back to the SHARER as `turns_withheld`."""
    ck = _seed_one_partition()
    conv = "c-1p-mixed"
    # Both documents get vocabulary disjoint from each other AND from the run's pile of
    # near-identical "Hamburg severance clause" documents. Turn 1 below is driven through the
    # real answer path, and `ask` condenses it against turn 0, so a shared vocabulary would
    # let the pile win top-k and the fixture would fail for a reason unrelated to this test.
    mine = _ingest_1p("doc-1p-bobs-own", BOB, ck,
                      text="The Osaka relocation stipend in doc-1p-bobs-own covers 74 nights.")
    hers = _ingest_1p("doc-1p-carols-2", CAROL, ck,
                      text="TOPSECRET-VALPARAISO: doc-1p-carols-2 ferry covers 19 crossings.")
    grant = client.post(f"/documents/{hers}/grants", cookies=ck[CAROL],
                        json={"grantee_oid": BOB})
    assert grant.status_code == 200, grant.text[:200]
    try:
        # Turn 0: bob's own document only. Asserted, not assumed - if fuzzy retrieval ever
        # drags carol's document into this turn the whole thread becomes withheld and this
        # test would quietly stop testing withholding at all.
        r0 = client.post("/chat", cookies=ck[BOB], json={
            "conv_id": conv,
            "question": "how many nights does the Osaka relocation stipend cover?"})
        assert mine in r0.json()["retrieved_docs"], r0.text[:300]
        assert hers not in r0.json()["retrieved_docs"], (
            f"fixture: turn 0 must NOT draw on carol's document: {r0.json()['retrieved_docs']}")
        # Turn 1: really synthesized from carol's document, by the real answer path.
        r1 = client.post("/chat", cookies=ck[BOB], json={
            "conv_id": conv,
            "question": "how many crossings does the TOPSECRET-VALPARAISO ferry cover?"})
        assert hers in r1.json()["retrieved_docs"], r1.text[:300]
        assert "19 crossings" in r1.json()["answer"], r1.json()["answer"][:200]

        made = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                           json={"grantee_oid": ALICE})
        assert made.status_code == 200, made.text[:300]
        assert made.json()["turns_withheld"] == 1, (
            f"the sharer was not told a turn did not travel: {made.json()}")
        granted = _conv_grants(ALICE, conv)
        assert mine in granted and hers not in granted, (
            f"the share passed on a document bob does not own: {granted}")

        # THE ASSERTION THAT MATTERS: the text, not the grant set.
        t = client.get(f"/conversations/{conv}/transcript", cookies=ck[ALICE]).json()
        blob = " ".join(x["question"] + " " + x["answer"] for x in t["turns"])
        assert "TOPSECRET-VALPARAISO" not in blob and "19 crossings" not in blob, (
            f"carol's document reached alice as transcript text: {t}")
        assert [x["seq"] for x in t["turns"]] == [0], (
            f"the withheld turn must be a gap, not renumbered away: {t}")

        # ...and the retrieval channel still agrees, as it did before.
        got = client.post("/chat", cookies=ck[ALICE], json={
            "conv_id": conv, "question": f"what does {hers} say about the ferry?"})
        assert hers not in got.json()["retrieved_docs"], got.text[:300]
        on_doc = client.get(f"/documents/{hers}/grants", cookies=ck[CAROL]).json()
        assert [g["granted_by"] for g in on_doc] == [CAROL], on_doc
    finally:
        _cleanup(conv, ALICE)
        # Carol's grant to bob is a LIVE #582 document grant with conv_id None, so it
        # contributes a doorway pair in every conversation bob opens for the rest of the run -
        # including the acct:-rig tests, whose threads would then cite a document from another
        # partition and be withheld. Left standing it makes a later test fail for a reason
        # that has nothing to do with what that test is about.
        client.delete(f"/grants/{grant.json()['grant_id']}", cookies=ck[CAROL])


def test_one_partition_the_shared_transcript_stops_at_the_share_boundary():
    """CRITICAL-2, reproduced by the reviewer and pinned here.

    The grants are a snapshot; the transcript was read live, so every turn bob added AFTER
    sharing reached alice as answer TEXT - including answers synthesized from a document she
    was never granted and provably cannot retrieve. The snapshot decided nothing, because the
    content arrived through the other channel."""
    ck = _seed_one_partition()
    conv = "c-1p-boundary"
    shared = _ingest_1p("doc-1p-shared", BOB, ck)
    # Vocabulary deliberately disjoint from every other seeded document in this run: the run
    # accumulates near-identical "Hamburg severance clause" documents, and a question sharing
    # their words gets crowded out of top-k, which would make the fixture precondition below
    # fail for a reason that has nothing to do with what this test is about.
    secret = _ingest_1p("doc-1p-secret", BOB, ck,
                        text="TOPSECRET-MERGER: the Zurich merger closes in March.")
    r = client.post("/chat", cookies=ck[BOB], json={
        "conv_id": conv,
        "question": f"how many weeks of severance in {shared} Hamburg clause?"})
    assert shared in r.json()["retrieved_docs"], r.text[:300]
    try:
        made = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                           json={"grantee_oid": ALICE})
        assert made.status_code == 200, made.text[:300]
        assert made.json()["turn_cutoff"] == 1, made.json()

        # Bob keeps working in the SAME thread, now on something he never meant to share.
        #
        # Appended through the store rather than driven through /chat, and the reason is
        # itself evidence for the propagation rule (#601 IMPORTANT-C): driving it through
        # /chat, the CONDENSE step rewrites the follow-up against the earlier turn's answer,
        # so the standalone question carries the first turn's Hamburg vocabulary and retrieval
        # returns those documents instead of this one. That is exactly the cross-turn coupling
        # the propagation rule exists for, and it makes the fixture non-deterministic here.
        # The row appended below is the row a real post-share turn leaves.
        _edition.conversation_service._store.append(conv, BOB, Turn(
            question="what does the Zurich merger close for?",
            standalone="Zurich merger closing date",
            answer="TOPSECRET-MERGER: the Zurich merger closes in March.",
            cited_docs=[secret]))

        t = client.get(f"/conversations/{conv}/transcript", cookies=ck[ALICE]).json()
        assert len(t["turns"]) == 1, (
            f"the transcript is live, not the transcript that was shared: {t}")
        joined = " ".join(turn["answer"] + turn["question"] for turn in t["turns"])
        assert "TOPSECRET-MERGER" not in joined and "Zurich" not in joined, (
            f"a post-share answer reached the recipient as text: {t}")
        assert t["turns"][0]["seq"] == 0 and t["turns"][0]["own"] is False, t

        # And the grant channel agrees, as it always did.
        got = client.post("/chat", cookies=ck[ALICE], json={
            "conv_id": conv, "question": f"what does {secret} say about Zurich?"})
        assert secret not in got.json()["retrieved_docs"], got.text[:300]

        # Her own continuation still appends after the frozen half, numbered within itself.
        both = client.get(f"/conversations/{conv}/transcript", cookies=ck[ALICE]).json()
        assert [(x["own"], x["seq"]) for x in both["turns"]] == [(False, 0), (True, 0)], both
    finally:
        _cleanup(conv, ALICE)


def test_one_partition_resharing_updates_one_row_and_one_revoke_ends_all_access():
    """NEW-2, reproduced by the reviewer and pinned here.

    Nothing deduped shares of the same (conversation, person), and `live_share_for` answered
    with the OLDEST live match. Three defects fell out of that: a deliberate re-share was
    ignored for the transcript (the boundary stayed at the first share, contradicting the
    promise that re-sharing is how new material gets included); revoking the older of two
    shares WIDENED what the recipient could read, because the read then resolved to the newer,
    later-cutoff row; and revoking either one dropped ALL of that grantee's conversation grants
    while removing one row, leaving a survivor share opening a transcript backed by nothing.

    One live row per (conversation, person, sharer) makes the first work, the second
    impossible and the third vacuous."""
    ck = _seed_one_partition()
    conv = "c-1p-reshare-same"
    # Disjoint vocabularies, same reason as above: turn 2 is condensed against turn 1.
    d1 = _ingest_1p("doc-1p-rs-one", BOB, ck,
                    text="The Bergen commuting allowance in doc-1p-rs-one covers 33 bus days.")
    d2 = _ingest_1p("doc-1p-rs-two", BOB, ck,
                    text="TOPSECRET-ZURICH: doc-1p-rs-two says the deal closes in March.")
    r = client.post("/chat", cookies=ck[BOB], json={
        "conv_id": conv, "question": "how many bus days does the Bergen commuting allowance cover?"})
    assert d1 in r.json()["retrieved_docs"], r.text[:300]
    try:
        s1 = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                         json={"grantee_oid": ALICE})
        assert s1.status_code == 200 and s1.json()["turn_cutoff"] == 1, s1.text[:300]
        t = client.get(f"/conversations/{conv}/transcript", cookies=ck[ALICE]).json()
        assert len(t["turns"]) == 1, t

        r = client.post("/chat", cookies=ck[BOB], json={
            "conv_id": conv, "question": "TOPSECRET-ZURICH when does the deal close?"})
        assert d2 in r.json()["retrieved_docs"], r.text[:300]

        # The deliberate re-share: one row, updated, not a second row beside it.
        s2 = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                         json={"grantee_oid": ALICE})
        assert s2.status_code == 200 and s2.json()["turn_cutoff"] == 2, s2.text[:300]
        assert s2.json()["share_id"] == s1.json()["share_id"], (
            f"a second share row was created for the same conversation and person: "
            f"{s1.json()['share_id']} then {s2.json()['share_id']}")
        mine = client.get(f"/conversations/{conv}/shares", cookies=ck[BOB]).json()["shares"]
        assert len(mine) == 1, f"the sharer sees more than one live share for one person: {mine}"
        assert len(client.get("/conversations/shared-with-me",
                              cookies=ck[ALICE]).json()["shares"]) >= 1

        # The fresh boundary is honoured - which is what the docstrings always promised.
        t = client.get(f"/conversations/{conv}/transcript", cookies=ck[ALICE]).json()
        assert len(t["turns"]) == 2, f"the deliberate re-share was ignored: {t}"

        # ONE revoke ends ALL of it. Under the bug a survivor share kept the transcript open.
        gone = client.delete(f"/conversations/shares/{s2.json()['share_id']}", cookies=ck[BOB])
        assert gone.status_code == 200, gone.text[:200]
        after = client.get(f"/conversations/{conv}/transcript", cookies=ck[ALICE])
        assert after.status_code == 404, (
            f"a revoke left a survivor share opening the thread: {after.status_code} "
            f"{after.text[:300]}")
        swm = client.get("/conversations/shared-with-me", cookies=ck[ALICE]).json()
        assert conv not in {s["conv_id"] for s in swm["shares"]}, (
            f"a revoked conversation is still listed as shared with the recipient - a "
            f"survivor share row outlived the revoke: {swm}")
        still = client.post("/chat", cookies=ck[ALICE], json={
            "conv_id": conv, "question": "TOPSECRET-ZURICH when does the deal close?"})
        assert d2 not in still.json()["retrieved_docs"], still.text[:300]
    finally:
        _cleanup(conv, ALICE)


def test_one_partition_a_conversation_share_reaches_no_other_read_path():
    """CRITICAL-A (#601 / ADR 0020), reproduced by the reviewer and pinned here, surface by
    surface, on CONTENT and on FILE BYTES rather than on status codes.

    Conversation scoping used to live only in `_request_scope`'s doorway, which is partition
    ROUTING - and `ReadScope.allows` returns True on its partition-equality arm before it ever
    looks at the doorway. So in the ordinary single-organization deployment the doorway was
    never consulted and nothing scoped the grant at all. The app.py docstring asserted BY NAME
    that none of these routes could see a conversation grant; every one of them could.

    Alice's only authorization anywhere in this test is one grant whose conv_id is this
    thread. Everything below is what that must NOT buy her."""
    ck = _seed_one_partition()
    conv = "c-1p-channel3"
    doc = "doc-1p-boardpack"
    secret = ("TOPSECRET-KRAKOW: the Meridian acquisition closes on 3 March for 412 million "
              "and the Hamburg severance clause pays out over 62 weeks.")
    r = client.post("/ingest", cookies=ck[BOB], json={
        "external_id": doc, "title": "Board pack Q3 (confidential)", "text": secret,
        "acl": [BOB], "uri": "upload://board-pack-q3.pdf"})
    assert r.status_code == 200, r.text[:200]
    r = client.post("/chat", cookies=ck[BOB], json={
        "conv_id": conv, "question": "when does the Meridian acquisition close TOPSECRET-KRAKOW?"})
    assert doc in r.json()["retrieved_docs"], r.text[:300]
    try:
        made = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                           json={"grantee_oid": ALICE})
        assert made.status_code == 200, made.text[:300]
        held = _edition.grant_registry.live_grants_for(ALICE)
        assert held and all(g.conv_id == conv for g in held), (
            f"fixture: alice must hold ONLY conv-scoped grants: "
            f"{[(g.doc_external_id, g.conv_id) for g in held]}")

        # POSITIVE CONTROL first: inside its own conversation the share must WORK, or every
        # refusal below could be passing because sharing is broken rather than scoped.
        inside = client.post("/chat", cookies=ck[ALICE], json={
            "conv_id": conv, "question": "when does the Meridian acquisition close?"})
        assert doc in inside.json()["retrieved_docs"], (
            f"the share does not work inside its own conversation: {inside.text[:300]}")

        # 1. /search - no conversation exists on this route at all.
        s = client.post("/search", cookies=ck[ALICE],
                        json={"question": "TOPSECRET-KRAKOW Meridian acquisition Hamburg"})
        assert doc not in s.json()["retrieved_docs"], s.text[:300]
        assert "KRAKOW" not in s.text and "412 million" not in s.text, s.text[:400]

        # 2. the "Your data" listing - a title is routinely the whole secret.
        rows = client.get("/admin/documents", cookies=ck[ALICE]).json()
        assert doc not in {d["doc_external_id"] for d in rows}, rows
        assert "Board pack Q3" not in str(rows) and "board-pack-q3" not in str(rows), rows

        # 3. segment previews - 200 with the chunk text in it was the leak, not the status.
        seg = client.get(f"/admin/documents/{doc}/segments", cookies=ck[ALICE])
        assert "KRAKOW" not in seg.text and "412 million" not in seg.text, seg.text[:400]

        # 4. the original file, in bytes.
        dl = client.get(f"/admin/documents/{doc}/download?form=text", cookies=ck[ALICE])
        assert dl.status_code == 404, f"{dl.status_code}, {len(dl.content)} bytes"
        assert b"KRAKOW" not in dl.content, dl.content[:200]

        # 5. the suggestions footer's entitlement count.
        sug = client.get("/ask/suggestions", cookies=ck[ALICE]).json()
        assert sug["authorized_docs"] == 0, sug

        # 6. /chat in an unrelated thread - the conversation exists but is the wrong one.
        other = client.post("/chat", cookies=ck[ALICE], json={
            "conv_id": "some-totally-unrelated-thread",
            "question": "when does the Meridian acquisition close and for how much?"})
        assert doc not in other.json()["retrieved_docs"], other.text[:300]
        assert "KRAKOW" not in other.text and "412 million" not in other.text, other.text[:400]
    finally:
        _cleanup(conv, ALICE)


def test_one_partition_a_withheld_turn_withholds_everything_after_it():
    """#601 IMPORTANT-C. Answers are not independent across turns: `ConversationService.ask`
    condenses each follow-up against a history window that INCLUDES prior answers (the real
    adapters fold answers into the condense prompt), so a turn citing only shareable documents
    can have been generated from a question synthesized out of a withheld turn's content, and
    a model that restates the question in its answer carries it forward. Withholding therefore
    propagates: the shared half is the prefix up to the first withheld turn.

    The turns are appended through the store so the citation pattern under test is the one
    this test means to test - retrieval is fuzzy and a run-long shared index would otherwise
    let a sibling document decide which turn is withheld."""
    ck = _seed_one_partition()
    conv = "c-1p-propagation"
    mine = _ingest_1p("doc-1p-prop-mine", BOB, ck)
    hers = _ingest_1p("doc-1p-prop-hers", CAROL, ck,
                      text="TOPSECRET-LISBON: doc-1p-prop-hers tram subsidy covers 11 lines.")
    grant = client.post(f"/documents/{hers}/grants", cookies=ck[CAROL],
                        json={"grantee_oid": BOB})
    assert grant.status_code == 200, grant.text[:200]
    store = _edition.conversation_service._store
    store.append(conv, BOB, Turn(question="severance?", standalone="severance?",
                                 answer="62 weeks.", cited_docs=[mine]))
    store.append(conv, BOB, Turn(question="and the tram subsidy?", standalone="tram subsidy",
                                 answer="TOPSECRET-LISBON: 11 lines.", cited_docs=[hers]))
    # Cites only bob's own document, and would travel under a per-turn rule - but its
    # question was condensed against the answer above, so it is downstream of it.
    store.append(conv, BOB, Turn(question="and how does that compare?",
                                 standalone="compare the 11 tram lines with severance",
                                 answer="AFTERMARKER: compared with the 11 lines, 62 weeks.",
                                 cited_docs=[mine]))
    try:
        made = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                           json={"grantee_oid": ALICE})
        assert made.status_code == 200, made.text[:300]
        assert made.json()["turn_cutoff"] == 1, made.json()
        assert made.json()["turns_withheld"] == 2, (
            f"withholding did not propagate past the first withheld turn: {made.json()}")
        t = client.get(f"/conversations/{conv}/transcript", cookies=ck[ALICE]).json()
        blob = " ".join(x["question"] + " " + x["answer"] for x in t["turns"])
        assert "TOPSECRET-LISBON" not in blob, f"the withheld turn travelled: {t}"
        assert "AFTERMARKER" not in blob, (
            f"a turn downstream of a withheld one travelled - its question was condensed "
            f"against content alice may not have: {t}")
        assert [x["seq"] for x in t["turns"]] == [0], t
    finally:
        _cleanup(conv, ALICE)
        client.delete(f"/grants/{grant.json()['grant_id']}", cookies=ck[CAROL])


def test_one_partition_sharing_a_colliding_conv_id_does_not_destroy_another_sharers_grants():
    """#601 IMPORTANT-B. `conv_id` is CLIENT-CHOSEN, so two people can hold threads under the
    same id. The share route drops the pair's older conversation grants before minting; drop
    it unscoped and bob's share of HIS thread destroys carol's grants to alice under the same
    id, while carol's share row stays live - so alice keeps a listed share backed by nothing
    and carol is never told. Owning your own thread under an id proves nothing about anybody
    else's grants under it."""
    ck = _seed_one_partition()
    conv = "c-1p-collision"
    carols = _ingest_1p("doc-1p-collide-carol", CAROL, ck)
    bobs = _ingest_1p("doc-1p-collide-bob", BOB, ck)
    for who, doc in ((CAROL, carols), (BOB, bobs)):
        r = client.post("/chat", cookies=ck[who], json={
            "conv_id": conv,
            "question": f"how many weeks of severance in {doc} Hamburg clause?"})
        assert doc in r.json()["retrieved_docs"], r.text[:300]
    try:
        assert client.post(f"/conversations/{conv}/shares", cookies=ck[CAROL],
                           json={"grantee_oid": ALICE}).status_code == 200
        carols_grants = {g.grant_id for g in _edition.grant_registry.live_grants_for(ALICE)
                         if g.granted_by == CAROL}
        assert carols_grants, "fixture: carol's share minted nothing"

        assert client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                           json={"grantee_oid": ALICE}).status_code == 200
        survived = {g.grant_id for g in _edition.grant_registry.live_grants_for(ALICE)
                    if g.granted_by == CAROL}
        assert survived == carols_grants, (
            f"bob's share of his own thread destroyed carol's grants under a colliding "
            f"conv_id: {carols_grants - survived}")
        # And carol's share still opens what she shared, rather than being a live row over
        # nothing - which is how the sharer would never find out.
        t = client.get(f"/conversations/{conv}/transcript", cookies=ck[ALICE]).json()
        assert t["turns"], f"a live share opens an empty transcript: {t}"

        # #601 round 4, NEW-2: the SAME collision on the REVOKE route, which round 3 left
        # unscoped. Bob revoking his own share must not end carol's.
        bobs = client.get(f"/conversations/{conv}/shares", cookies=ck[BOB]).json()["shares"]
        assert len(bobs) == 1, bobs
        gone = client.delete(f"/conversations/shares/{bobs[0]['share_id']}", cookies=ck[BOB])
        assert gone.status_code == 200, gone.text[:200]
        after = {g.grant_id for g in _edition.grant_registry.live_grants_for(ALICE)
                 if g.granted_by == CAROL}
        assert after == carols_grants, (
            f"bob's revoke of his OWN share destroyed carol's grants under a colliding "
            f"conv_id: {carols_grants - after}")
        # Carol's share must still OPEN, not merely be listed - a live row over nothing is
        # how the reproduction showed up (shared-with-me listed it, the transcript 404'd).
        t = client.get(f"/conversations/{conv}/transcript", cookies=ck[ALICE])
        assert t.status_code == 200 and t.json()["turns"], (
            f"carol's share survived as a row but opens nothing: {t.status_code} "
            f"{t.text[:200]}")
    finally:
        _cleanup(conv, ALICE)


def test_one_partition_a_refused_share_never_leaves_a_grant_standing():
    """#601 round 4, NEW-1. The `not final_cut` refusal was added in round 3 and, unlike the
    `not backed` case beside it (vacuous - backed 0 means nothing was minted), it is reachable
    with grants already minted AND confirmed. It discarded the share row and 400'd, leaving
    them live.

    Reproduced: the sharer is told nothing was shared; the recipient holds a live grant on a
    document that was never part of any successful share, retrieves it inside a conv_id an
    earlier share taught her, and the sharer has nothing on the conversation surface to
    revoke it with, because the share row is gone."""
    ck = _seed_one_partition()
    conv = "c-1p-refused"
    first = _ingest_1p("doc-1p-refused-first", BOB, ck)
    later = _ingest_1p("doc-1p-refused-later", BOB, ck)
    store = _edition.conversation_service._store
    store.append(conv, BOB, Turn(question="first?", standalone="first", answer="62 weeks.",
                                 cited_docs=[first]))
    try:
        made = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                           json={"grantee_oid": ALICE})
        assert made.status_code == 200, made.text[:300]

        # Bob works on; the new turn cites a document he never shared.
        store.append(conv, BOB, Turn(question="later?", standalone="later",
                                     answer="REFUSEDMARKER 41 weeks.", cited_docs=[later]))
        # ...and the FIRST document disappears between the shareability scan and the mint,
        # so nothing the shared prefix needs survives and the re-share is refused.
        real_add = _edition.index.add_doc_principals

        def _first_vanished(tenant_id, doc_external_id, principals):
            if doc_external_id == first:
                return 0
            return real_add(tenant_id, doc_external_id, principals)

        _edition.index.add_doc_principals = _first_vanished
        try:
            again = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                                json={"grantee_oid": ALICE})
            assert again.status_code == 400, again.text[:300]
        finally:
            _edition.index.add_doc_principals = real_add

        held = {g.doc_external_id for g in _edition.grant_registry.live_grants_for(ALICE)}
        assert later not in held, (
            f"a refused share left a live grant on a document that was never shared: {held}")
        got = client.post("/chat", cookies=ck[ALICE], json={
            "conv_id": conv, "question": f"REFUSEDMARKER how many weeks in {later}?"})
        assert later not in got.json()["retrieved_docs"], (
            f"the sharer was told nothing was shared and the recipient can retrieve: "
            f"{got.text[:300]}")
        assert "REFUSEDMARKER" not in got.text, got.text[:300]
    finally:
        _cleanup(conv, ALICE)


def test_one_partition_a_share_with_no_documents_left_opens_nothing():
    """#601 round 4, NEW-3. The propagation `break` stops at the first turn citing something
    ungranted - but a ZERO-citation turn cites nothing, so `set() <= granted` holds however
    empty `granted` is, and a LEADING zero-citation turn was still served after every document
    behind the share was gone. No document content travels that way, but the grantor's typed
    QUESTION does, and a question can be the whole secret.

    The rule: a conversation share is a transcript AND the documents it rests on. The create
    path refuses to mint one with no documents; the read path now refuses to serve one whose
    grants have since gone. The round-3 comment claimed the create-time refusal covered both."""
    ck = _seed_one_partition()
    conv = "c-1p-zerocite"
    doc = _ingest_1p("doc-1p-zerocite", BOB, ck)
    store = _edition.conversation_service._store
    store.append(conv, BOB, Turn(question="why was the Krakow office headcount cut in half?",
                                 standalone="Krakow headcount",
                                 answer="ZEROCITE-LEAD: I could not find anything.",
                                 cited_docs=[]))
    store.append(conv, BOB, Turn(question="severance?", standalone="severance",
                                 answer="62 weeks.", cited_docs=[doc]))
    try:
        made = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                           json={"grantee_oid": ALICE})
        assert made.status_code == 200 and made.json()["turn_cutoff"] == 2, made.text[:300]
        t = client.get(f"/conversations/{conv}/transcript", cookies=ck[ALICE]).json()
        assert len(t["turns"]) == 2, t          # while the share really rests on a document

        # Every document behind the share goes.
        dropped = _edition.grant_registry.drop_for_conversation(conv, ALICE, granted_by=BOB)
        assert dropped >= 1, dropped

        after = client.get(f"/conversations/{conv}/transcript", cookies=ck[ALICE])
        assert after.status_code == 404, (
            f"a share with no live document behind it still opened: {after.status_code} "
            f"{after.text[:300]}")
        assert "Krakow" not in after.text and "ZEROCITE-LEAD" not in after.text, (
            f"the grantor's typed question travelled after every grant was gone: "
            f"{after.text[:300]}")
    finally:
        _cleanup(conv, ALICE)


def test_one_partition_a_document_that_vanishes_mid_share_is_counted_as_withheld():
    """#601 MINOR-F. `turns_withheld` used to be computed before the mint loop, so a document
    that passed the shareability test and then failed `add_doc_principals` (deleted in
    between) was withheld from the recipient - the transcript checks live grants - but
    reported to the sharer as shared. A number she would act on that was never true."""
    ck = _seed_one_partition()
    conv = "c-1p-vanish"
    d1 = _ingest_1p("doc-1p-vanish-a", BOB, ck)
    d2 = _ingest_1p("doc-1p-vanish-b", BOB, ck)
    store = _edition.conversation_service._store
    store.append(conv, BOB, Turn(question="a?", standalone="a", answer="62 weeks.",
                                 cited_docs=[d1]))
    store.append(conv, BOB, Turn(question="b?", standalone="b", answer="VANISHMARKER 41.",
                                 cited_docs=[d2]))
    real_add = _edition.index.add_doc_principals

    def _d2_vanished(tenant_id, doc_external_id, principals):
        if doc_external_id == d2:
            return 0            # deleted between the shareability test and the mint
        return real_add(tenant_id, doc_external_id, principals)

    _edition.index.add_doc_principals = _d2_vanished
    try:
        made = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                           json={"grantee_oid": ALICE})
        assert made.status_code == 200, made.text[:300]
        assert made.json()["documents"] == 1, made.json()
        assert made.json()["turns_withheld"] == 1, (
            f"the sharer was told a turn travelled that the recipient cannot read: "
            f"{made.json()}")
        assert made.json()["turn_cutoff"] == 1, made.json()
    finally:
        _edition.index.add_doc_principals = real_add
    try:
        t = client.get(f"/conversations/{conv}/transcript", cookies=ck[ALICE]).json()
        assert "VANISHMARKER" not in str(t), t
        assert len(t["turns"]) == 1, t
    finally:
        _cleanup(conv, ALICE)


def test_one_partition_a_failed_mint_leaves_no_live_share_and_no_grants():
    """IMPORTANT-3. `add_doc_principals` is a live index round trip and can raise. Before the
    rollback, that returned 500 to the sharer while leaving the share row LIVE and the grants
    minted before the failure standing - her UI says the share failed and the recipient can
    read and ask. The first document is allowed through and the second raises, so this pins
    the rollback of an ALREADY-MINTED grant, not just of the share row."""
    ck = _seed_one_partition()
    conv = "c-1p-midfail"
    d1 = _ingest_1p("doc-1p-midfail-a", BOB, ck)
    d2 = _ingest_1p("doc-1p-midfail-b", BOB, ck)
    for d in (d1, d2):
        r = client.post("/chat", cookies=ck[BOB], json={
            "conv_id": conv, "question": f"how many weeks of severance in {d} Hamburg clause?"})
        assert r.status_code == 200, r.text[:300]

    real_add = _edition.index.add_doc_principals
    calls = {"n": 0}

    def _fails_on_the_second(tenant_id, doc_external_id, principals):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("index unreachable")
        return real_add(tenant_id, doc_external_id, principals)

    _edition.index.add_doc_principals = _fails_on_the_second
    try:
        r = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                        json={"grantee_oid": ALICE})
        assert r.status_code == 503, (
            f"a mid-share failure was not reported as a failure: {r.status_code} {r.text[:200]}")
    finally:
        _edition.index.add_doc_principals = real_add

    assert calls["n"] >= 2, f"the fixture never reached the failing call: {calls}"
    assert _edition.conversation_shares.live_share_for(conv, ALICE) is None, (
        "the share call failed and the share is live - the recipient can open the thread")
    assert _conv_grants(ALICE, conv) == set(), (
        f"the share call failed and grants survived it: {_conv_grants(ALICE, conv)}")
    assert conv not in {s["conv_id"] for s in client.get(
        "/conversations/shared-with-me", cookies=ck[ALICE]).json()["shares"]}
    got = client.post("/chat", cookies=ck[ALICE], json={
        "conv_id": conv, "question": f"how many weeks of severance in {d1} Hamburg clause?"})
    assert d1 not in got.json()["retrieved_docs"], (
        f"the share call failed and alice can retrieve: {got.text[:300]}")


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
            except Exception as e:
                # Not the neighbours' bare `except AssertionError`, on purpose: these tests
                # drive real routes, so a regression can surface as a 500 that raises out of
                # the TestClient rather than as a failed assertion. Under the plain
                # convention that aborts the run at the first such test and every test after
                # it goes unreported - which is exactly what the caller-ownership mutation
                # did while proving this file. An error is a failure, and is counted as one.
                print(f"  FAIL  {name}: {type(e).__name__}: {e}"); failed += 1
    for k in _VARS:
        os.environ.pop(k, None)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
