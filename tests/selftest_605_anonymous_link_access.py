"""#605 / ADR 0021: the anonymous doorway. A visitor with a URL and NO ACCOUNT reads a shared
conversation and asks their own questions against the owner's documents.

This is the highest-risk surface in the product - the first time it answers a caller it cannot
identify - so every test here drives the REAL FastAPI routes with real cookies, and every
assertion that matters is on the ANSWER TEXT a visitor receives, not only on grant rows or
counts. Three defects on the previous branch closed the grant channel and left the content
channel open beside it; a test that reads only grants would have passed through all three.

THE RIG IS SINGLE-PARTITION, deliberately, and that is not a detail. `tests/
selftest_600_share_api.py` documents why: giving every account its own `acct:` partition makes
a same-partition disclosure unrepresentable, and both CRITICALs in that feature's first review
were invisible in the per-account rig precisely because the partition filter was silently doing
work the missing authorization check should have been doing. The whole reason a link visitor's
partition is SYNTHETIC (`linkvisitor:<share_id>`) is a same-partition property, so a rig that
cannot express one cannot test it.

    PYTHONPATH=src python3 tests/selftest_605_anonymous_link_access.py
"""
import os
import sys
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")
# The public demo's per-IP cap is installed at import time and every test here is several real
# HTTP calls - without this the file starts 429ing partway through and every later test fails
# on its own seed ingest, a rig failure that reads exactly like a regression.
os.environ["DBSEARCH_RATE_LIMIT"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.query.conversation import Turn  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import ACCOUNTS, app, _edition  # noqa: E402
from dbsearch.server.conversation_shares import (  # noqa: E402
    AUDIENCE_LINK, AUDIENCE_PEOPLE, _now, _token_digest)
from dbsearch.server.link_access import (  # noqa: E402
    VISITOR_COOKIE, fork_key, link_principal, visitor_scope)

client = TestClient(app)

ALICE = "acct_alice"
BOB = "acct_bob"
HOME_TID = "tid-605-link-home"
ONE = None          # the shared partition, resolved lazily from the edition
CK = {}


def _seed_one_partition():
    """Two identities signing in with the HOME tenant tid, so both land in the deployment's
    own partition - a real single-org deployment, where the partition filter protects nobody
    from anybody, and where a link visitor given a REAL partition would therefore be given the
    whole workspace."""
    global ONE, CK
    for k in ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET"):
        os.environ.pop(k, None)
    os.environ.update({"AUTH_TENANT_ID": HOME_TID, "AUTH_CLIENT_ID": "cid",
                       "AUTH_CLIENT_SECRET": "sec"})
    ACCOUNTS.resolve("local", "alice@x.com", preferred_account_id=ALICE, email="alice@x.com")
    ACCOUNTS.resolve("local", "bob@x.com", preferred_account_id=BOB, email="bob@x.com")
    ONE = _edition.tenant_id
    CK = {who: {user_auth.COOKIE: user_auth.sign_session(
        {"oid": who, "tid": ONE, "exp": int(time.time()) + 3600})}
        for who in (ALICE, BOB)}
    return CK


# The two document markers every content assertion in this file turns on. Distinct vocabulary
# on purpose: retrieval is fuzzy and a shared vocabulary would let one document decide what a
# test about the other proves.
A_TEXT = "The Lisbon carryover allowance in doc-605-a carries over 26 days of unused leave."
A_MARKER = "26 days"
B_TEXT = "TOPSECRET-KRAKOW: doc-605-b records the headcount was cut by 41 posts last year."
B_MARKER = "41 posts"


def _ingest(doc_id: str, text: str, owner: str = BOB, uri: str = None) -> str:
    r = client.post("/ingest", cookies=CK[owner], json={
        "external_id": doc_id, "title": f"policy {doc_id}", "acl": [owner],
        "text": text, "uri": uri or f"upload://{doc_id}.txt"})
    assert r.status_code == 200, f"seed ingest failed: {r.status_code} {r.text[:200]}"
    return doc_id


# The SHAPE the uri leak actually has in production, and the reason `upload://...` above is not
# good enough to test it with. A connector-backed deployment stores an internal LOCATION here:
# the tenant, the site, the library, the folder path and the filename, every one of which is a
# fact about how the owner's organization is arranged. `components.js`'s `sourceCard` renders
# an `https?://` uri as a clickable link AND prints the URL text underneath it, so this is not
# a field that merely sits in a payload - the visitor is SHOWN it.
SP_URI = "https://acme.sharepoint.com/sites/Finance/Shared%20Documents/HR/Q3-layoffs.docx"
#: Substrings hunted through the WHOLE serialized body below. Split deliberately: percent
#: encoding, a JSON re-encode or a partial render would each defeat a single whole-string match
#: while still handing the stranger the tenant or the folder path.
SP_LEAK_MARKERS = ("acme.sharepoint.com", "sites/Finance", "Q3-layoffs")
#: Its own vocabulary, for the same reason A_TEXT and B_TEXT have theirs: the rig's index is
#: shared across tests in this file and every other seed is owned by BOB too, so a question
#: phrased in A_TEXT's words retrieves whichever leave-policy document top-k happened to like.
#: The signed-in half of the uri test asserts on a SPECIFIC citation, so it has to be able to
#: name which document it got.
C_TEXT = "SEVERANCEBRIEF-OPORTO: doc-612-uri records the severance schedule pays 19 weeks."
C_MARKER = "19 weeks"
C_QUESTION = "how many weeks does the severance schedule pay?"


def _turn(conv: str, question: str, answer: str, docs: list, who: str = BOB) -> None:
    """Append a turn to the grantor's thread through the store.

    The honest way to pin a citation set - the same reasoning selftest_600_share_api.py records:
    retrieval is fuzzy, and a test asserting on WHICH turns travel must control which documents
    each turn cited rather than hope top-k cooperated. THIS row is exactly what a real answered
    question leaves behind, and every share rule downstream reads it and nothing else."""
    _edition.conversation_service._store.append(conv, who, Turn(
        question=question, standalone=question, answer=answer, cited_docs=list(docs)))


def _make_link(conv: str, exclude=(), days=None) -> dict:
    body = {"audience": AUDIENCE_LINK}
    if exclude:
        body["exclude_docs"] = list(exclude)
    if days is not None:
        body["expires_in_days"] = days
    r = client.post(f"/conversations/{conv}/shares", cookies=CK[BOB], json=body)
    assert r.status_code == 200, f"share failed: {r.status_code} {r.text[:300]}"
    out = r.json()
    assert out["audience"] == AUDIENCE_LINK and out["url"].startswith("/c/"), out
    return out


def _visitor() -> TestClient:
    """A browser with NO account: its own cookie jar, never given a session cookie. Whatever
    the server sets on it (the fork-key cookie) is stored and re-sent exactly as a browser
    would, which is the only way a test can tell a fork apart from a fresh visitor."""
    return TestClient(app)


def _share_record(share_id: str):
    return _edition.conversation_shares.find(share_id, BOB)


def _cleanup(conv: str, *grantees: str):
    for g in grantees:
        try:
            _edition.grant_registry.drop_for_conversation(conv, g)
        except Exception:
            pass


# ---- the whole point: a stranger reads and asks, with no account anywhere -----------------

def test_a_visitor_reads_the_transcript_and_asks_without_any_session():
    """GOAL_ACCEPTANCE (260810) steps 5-6, and the positive control every refusal below needs:
    without it, a test asserting "the visitor cannot see docB" could be passing because link
    sharing is broken outright rather than because a rule fired."""
    _seed_one_partition()
    conv = "c-605-happy"
    doc = _ingest("doc-605-a", A_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER} of unused leave.",
          [doc])
    made = _make_link(conv)
    url = made["url"]
    try:
        anon = _visitor()
        page = anon.get(url)
        assert page.status_code == 200, f"the share page did not open: {page.status_code}"
        assert VISITOR_COOKIE in anon.cookies, (
            f"no fork-key cookie was minted: {dict(anon.cookies)}")

        t = anon.get(url + "/transcript")
        assert t.status_code == 200, t.text[:300]
        body = t.json()
        assert body["own"] is False and body["disclosure"] is True, body
        # THE CONTENT CHANNEL: the answer TEXT arrived, not merely a turn count.
        assert A_MARKER in body["turns"][0]["answer"], body["turns"][0]
        assert body["turns"][0]["own"] is False, body["turns"][0]

        r = anon.post(url + "/chat", json={"question": "how many days carry over?"})
        assert r.status_code == 200, r.text[:300]
        ans = r.json()
        assert ans["corpus"]["authorized_docs"] == 1, (
            f"the visitor's honest denominator is wrong: {ans['corpus']}")
        assert doc in ans["retrieved_docs"], (
            f"a visitor holding the link retrieved nothing from the shared document: {ans}")
        assert A_MARKER in ans["answer"], ans["answer"][:200]

        # ...and their own question is now part of THEIR half of the thread, marked own.
        after = anon.get(url + "/transcript").json()["turns"]
        assert [x for x in after if x["own"]], after
        assert "how many days carry over?" in repr(after), after
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_the_six_surface_probe_holds_for_an_anonymous_caller():
    """ADR 0021, "What is NOT granted": every route outside the `/c/{token}` family depends on
    `current_user`, which 401s when there is nothing to resolve - so all six refuse a visitor
    before any authorization logic runs, with no per-route hardening written anywhere.

    Probed TWICE: once by a browser that never opened the link, and once by a browser that
    HOLDS the fork-key cookie. The second is the one that matters. It is the assertion that
    the cookie is a scratchpad key and not a credential, and it is the test that would go red
    the day somebody "hardens" it into something `resolve_identity` accepts."""
    _seed_one_partition()
    conv = "c-605-probe"
    doc = _ingest("doc-605-probe", A_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [doc])
    made = _make_link(conv)
    try:
        cookied = _visitor()
        assert cookied.get(made["url"]).status_code == 200
        assert VISITOR_COOKIE in cookied.cookies

        for anon, who in ((_visitor(), "no cookie"), (cookied, "holding the visitor cookie")):
            probes = [
                ("POST", "/search"),
                ("GET", "/admin/documents"),
                ("GET", f"/admin/documents/{doc}/segments"),
                ("GET", f"/admin/documents/{doc}/download?form=text"),
                ("GET", "/ask/suggestions"),
                ("POST", "/chat"),
            ]
            for method, path in probes:
                resp = (anon.post(path, json={"question": "q", "conv_id": "c-605-elsewhere"})
                        if method == "POST" else anon.get(path))
                assert resp.status_code == 401, (
                    f"{path} answered {resp.status_code} to an anonymous caller ({who}): "
                    f"{resp.text[:200]}")
                assert A_MARKER not in resp.text, f"{path} leaked content to {who}"
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_two_visitors_never_see_each_other_and_the_owner_thread_is_unwritable():
    """The private fork. A visitor's turns land under `link:<share_id>:<visitor_id>`, so the
    owner's thread is STRUCTURALLY unwritable by a stranger rather than merely un-written by
    convention - nothing in the doorway ever names the grantor's key - and one visitor's
    questions are not in another's thread to read."""
    _seed_one_partition()
    conv = "c-605-forks"
    doc = _ingest("doc-605-forks", A_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [doc])
    made = _make_link(conv)
    url = made["url"]
    before = len(_edition.conversation_service.history(BOB, conv))
    try:
        a, b = _visitor(), _visitor()
        assert a.get(url).status_code == 200 and b.get(url).status_code == 200
        assert a.cookies[VISITOR_COOKIE] != b.cookies[VISITOR_COOKIE], (
            "two visitors were given the SAME fork key - their threads would merge")

        ra = a.post(url + "/chat", json={"question": "PRIVATE-QUESTION-FROM-A about leave"})
        assert ra.status_code == 200, ra.text[:300]

        tb = b.get(url + "/transcript")
        assert tb.status_code == 200, tb.text[:300]
        assert "PRIVATE-QUESTION-FROM-A" not in repr(tb.json()), (
            f"one visitor read another visitor's question: {tb.text[:400]}")
        # A sees their own, so the negative above is not passing because forks are empty.
        assert "PRIVATE-QUESTION-FROM-A" in repr(a.get(url + "/transcript").json())

        owner = client.get(f"/conversations/{conv}/transcript", cookies=CK[BOB])
        assert owner.status_code == 200, owner.text[:300]
        assert "PRIVATE-QUESTION-FROM-A" not in repr(owner.json()), (
            f"a stranger's question appeared in the owner's own thread: {owner.text[:400]}")
        assert len(_edition.conversation_service.history(BOB, conv)) == before, (
            "a visitor appended a turn under the OWNER's conversation key")
        # And it really did land on the fork, so "not on the owner's key" is not "nowhere".
        share = _share_record(made["share_id"])
        vid = a.cookies[VISITOR_COOKIE]
        forked = _edition.conversation_service.history(fork_key(share, vid), conv)
        assert any("PRIVATE-QUESTION-FROM-A" in t.question for t in forked), forked
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_revoke_kills_every_fork_and_the_dead_link_is_a_404():
    """ADR 0021 invariant 4. The token resolves to a row; the row is gone; every route in the
    family answers 404 - the page, the transcript and both ask routes alike, because each one
    resolves the share BEFORE it does anything else."""
    _seed_one_partition()
    conv = "c-605-revoke"
    doc = _ingest("doc-605-revoke", A_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [doc])
    made = _make_link(conv)
    url = made["url"]
    a = _visitor()
    assert a.post(url + "/chat", json={"question": "q1 about leave"}).status_code == 200

    killed = client.delete(f"/conversations/shares/{made['share_id']}", cookies=CK[BOB])
    assert killed.status_code == 200, killed.text[:300]

    assert a.get(url).status_code == 404, "a revoked link still opened its page"
    assert a.get(url + "/transcript").status_code == 404, (
        "a revoked link still served the transcript to a visitor who had already read it")
    assert a.post(url + "/chat", json={"question": "q2"}).status_code == 404
    assert a.post(url + "/chat/stream", json={"question": "q2"}).status_code == 404
    # A fresh visitor sees the same nothing, and sees it identically.
    assert _visitor().get(url).status_code == 404


def test_a_link_visitor_cannot_reach_a_doc_the_share_does_not_cover():
    """#610's narrowing, seen from the visitor's side, on BOTH channels.

    The thread cites two documents and the owner unchecks the second. The grant channel closes
    (one grant, an honest denominator of 1) AND the content channel closes with it: the turn
    that cited the excluded document is not in the transcript, so its answer text never travels
    either. Asserting only the count is exactly the mistake that let three defects ship."""
    _seed_one_partition()
    conv = "c-605-excluded"
    a_doc = _ingest("doc-605-a", A_TEXT)
    b_doc = _ingest("doc-605-b", B_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [a_doc])
    _turn(conv, "what happened to the Krakow headcount?", f"It was cut by {B_MARKER}.", [b_doc])
    made = _make_link(conv, exclude=[b_doc])
    url = made["url"]
    try:
        assert made["documents"] == 1, made
        anon = _visitor()
        assert anon.get(url).status_code == 200

        t = anon.get(url + "/transcript").json()
        assert A_MARKER in repr(t), t
        assert B_MARKER not in repr(t), (
            f"the excluded document's ANSWER TEXT reached a link visitor: {t}")
        assert "Krakow" not in repr(t), f"the excluded document's turn travelled: {t}"

        r = anon.post(url + "/chat", json={"question": "what happened to the Krakow headcount?"})
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["corpus"]["authorized_docs"] == 1, (
            f"the denominator is not honest about what this link covers: {body['corpus']}")
        assert b_doc not in body["retrieved_docs"], body
        assert B_MARKER not in repr(body), (
            f"the excluded document's content reached a visitor as prose: {r.text[:400]}")
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_the_visitor_partitions_routing_arm_can_never_fire():
    """THE #601 REGRESSION PIN, and the reason `visitor_scope` looks the way it does.

    `ReadScope.allows` is `tenant_id == partition or (tenant_id, doc) in doorway` - the
    partition arm SHORT-CIRCUITS TRUE before the doorway is consulted. #601 shipped because a
    scoping rule was expressed only in the doorway while grantor and grantee shared a partition,
    which is every self-host box and every single-org tenant - so the rule was never applied at
    all. A visitor handed a REAL partition is that shape by construction.

    The ACL overlap is a second, independent refusal and would still stand - which is what
    defence in depth means, and why the HTTP-level test above survives this mutation. So this
    test probes the ROUTING LAYER on its own, with a principal set that WOULD pass the ACL: it
    asserts the layer refuses a document the share does not cover regardless of what the ACL
    would have said. If this ever goes green with a real partition, the routing half is dead
    code and only one refusal is left standing where the design says two."""
    _seed_one_partition()
    conv = "c-605-partition"
    a_doc = _ingest("doc-605-a", A_TEXT)
    b_doc = _ingest("doc-605-b", B_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [a_doc])
    _turn(conv, "what happened to the Krakow headcount?", f"It was cut by {B_MARKER}.", [b_doc])
    made = _make_link(conv, exclude=[b_doc])
    try:
        share = _share_record(made["share_id"])
        scope = visitor_scope(_edition, share)

        assert scope.partition == f"linkvisitor:{share.share_id}", scope.partition
        assert scope.partition != ONE and not scope.partition.startswith("acct:"), (
            f"the visitor partition can collide with a real one: {scope.partition}")
        assert scope.active_conv_id == conv, scope

        assert scope.allows(ONE, a_doc) is True, "the shared document is not reachable at all"
        assert scope.allows(ONE, b_doc) is False, (
            "the routing layer would let a visitor look at a document this share does not "
            "cover - the #601 partition short-circuit is live again")

        # The same statement made on retrieved CONTENT: hand the index a principal set that
        # passes docB's ACL outright and prove the SCOPE alone still refuses it.
        hits = _edition.index.lexical_search(["krakow", "headcount"], [BOB], 10, scope)
        assert all(h["doc_external_id"] != b_doc for h in hits), (
            f"a visitor scope routed a caller to a document the share excludes: {hits}")
        # Positive control on the same call: the doorway really does open what it should.
        opened = _edition.index.lexical_search(["lisbon", "carryover"], [BOB], 10, scope)
        assert any(h["doc_external_id"] == a_doc for h in opened), (
            f"the doorway opened nothing - this test would pass vacuously: {opened}")
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_a_people_audience_row_carrying_a_token_never_opens_as_a_link():
    """The `audience != "link"` refusal in `_live_share`.

    A people share has no token hash, so `find_by_token` can never reach one in normal
    operation - which is exactly why this row is built by hand. It represents the shapes that
    refusal exists for: a migration artefact, a future code path, or a bug that leaves a token
    hash on a row whose recipient was named on the understanding that she would sign in.
    Opening it anonymously would hand her conversation to anybody holding the value.

    The link-audience CONTROL beside it proves the rig can produce an openable row at all, so a
    green result here cannot come from the page being broken for some unrelated reason."""
    _seed_one_partition()
    conv = "c-605-audience"
    doc = _ingest("doc-605-audience", A_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [doc])

    people_token = "tok-605-people"
    _edition.conversation_shares._admit(
        conv, BOB, ALICE, turn_cutoff=1, share_id="share-605-people",
        audience=AUDIENCE_PEOPLE, token_hash=_token_digest(people_token))
    link_token = "tok-605-link"
    _edition.conversation_shares._admit(
        conv, BOB, "link:share-605-link", turn_cutoff=1, share_id="share-605-link",
        audience=AUDIENCE_LINK, token_hash=_token_digest(link_token))
    try:
        assert _edition.conversation_shares.find_by_token(people_token) is not None, (
            "rig: the people row must be resolvable by token, or this test proves nothing")
        assert _visitor().get(f"/c/{people_token}").status_code == 404, (
            "a named-people share opened anonymously through a link token")
        assert _visitor().get(f"/c/{link_token}").status_code == 200, (
            "rig control: a link-audience row with the same shape did not open")
    finally:
        _edition.conversation_shares.discard_unconfirmed("share-605-people")
        _edition.conversation_shares.discard_unconfirmed("share-605-link")


def test_an_expired_link_is_the_same_404_as_a_token_that_never_existed():
    """ADR 0021 invariant 3, and the liveness half of `find_by_token`.

    There is no sweeper - expiry is evaluated per read - so the `is_live` clause is not a
    tightening of a check, it is the only thing standing between an expired link and one that
    works forever. And the refusal is identical to an unknown token's: the caller here is
    anonymous by construction, so telling "expired" apart from "never existed" would confirm to
    anyone sweeping the token space that a value was once real."""
    _seed_one_partition()
    conv = "c-605-expired"
    doc = _ingest("doc-605-expired", A_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [doc])
    made = _make_link(conv, days=7)
    token = made["url"].rsplit("/", 1)[-1]
    try:
        anon = _visitor()
        assert anon.get(made["url"]).status_code == 200, "control: the live link must open"

        share = _share_record(made["share_id"])
        _edition.conversation_shares._by_id[share.share_id] = replace(
            share, expires_at=_now() - timedelta(minutes=1))

        dead = anon.get(made["url"])
        never = anon.get("/c/" + "0" * len(token))
        assert dead.status_code == 404, "an expired link still opened"
        # BYTE-IDENTICAL, not merely equal-shaped (#605 task 12). The page route now renders a
        # human "this link is no longer available" document rather than a JSON detail, and a
        # page is a much larger surface to leak a difference through than a two-word string:
        # a token echoed back, a date, an owner, a cache header. Comparing the raw bodies and
        # the headers a client can see is the only comparison that stays true as that page
        # grows. `Set-Cookie` is included on purpose - a fork-key cookie minted for one dead
        # token and not another would itself be the distinguishable refusal.
        assert dead.status_code == never.status_code, (
            f"expired and never-existed answer with different statuses: "
            f"{dead.status_code} vs {never.status_code}")
        assert dead.content == never.content, (
            f"expired and never-existed serve different bytes: {dead.text[:200]} vs "
            f"{never.text[:200]}")
        for h in ("content-type", "cache-control", "set-cookie"):
            assert dead.headers.get(h) == never.headers.get(h), (
                f"expired and never-existed differ on {h}: "
                f"{dead.headers.get(h)!r} vs {never.headers.get(h)!r}")
        assert anon.post(made["url"] + "/chat",
                         json={"question": "q"}).status_code == 404
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_a_turn_the_owner_adds_after_sharing_never_reaches_a_visitor():
    """`turn_cutoff`, read from the visitor's side. The share hands over a TRANSCRIPT AS IT WAS
    READ, not a subscription to a thread that keeps growing - so a turn the owner adds
    afterwards, synthesized from whatever she asks next, is not the visitor's to read even
    though the conversation id is the same and the link is still live."""
    _seed_one_partition()
    conv = "c-605-cutoff"
    doc = _ingest("doc-605-cutoff", A_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [doc])
    made = _make_link(conv)
    try:
        assert made["turn_cutoff"] == 1, made
        _turn(conv, "and the Krakow headcount?", "AFTER-THE-SHARE it was cut by 41 posts.",
              [doc])
        t = _visitor().get(made["url"] + "/transcript").json()
        assert A_MARKER in repr(t), t
        assert "AFTER-THE-SHARE" not in repr(t), (
            f"a turn added after the share reached a link visitor: {t}")
        assert len([x for x in t["turns"] if not x["own"]]) == 1, t
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_the_stream_route_holds_the_same_rules_as_the_json_one():
    """Two endpoints onto one capability: a rule held by only one of them is a rule a caller
    picks its way around by choosing the other. Same scope, same fork, same denominator - and
    `retrieved_owners` (an ACCOUNT ID, #576 Finding B) never reaches the wire, which matters
    more here than on /chat because the recipient is a stranger."""
    _seed_one_partition()
    conv = "c-605-stream"
    a_doc = _ingest("doc-605-a", A_TEXT)
    b_doc = _ingest("doc-605-b", B_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [a_doc])
    _turn(conv, "what happened to the Krakow headcount?", f"It was cut by {B_MARKER}.", [b_doc])
    made = _make_link(conv, exclude=[b_doc])
    url = made["url"]
    try:
        anon = _visitor()
        r = anon.post(url + "/chat/stream", json={"question": "how many days carry over?"})
        assert r.status_code == 200, r.text[:300]
        assert "retrieved_owners" not in r.text, (
            f"an account id rode out to an anonymous visitor: {r.text[:400]}")
        done = [ln for ln in r.text.splitlines() if '"done"' in ln]
        assert done, r.text[:400]
        import json as _json
        ev = _json.loads(done[-1][len("data: "):])
        assert ev["corpus"]["authorized_docs"] == 1, ev["corpus"]
        assert a_doc in ev["retrieved_docs"], ev
        assert b_doc not in ev["retrieved_docs"], ev
        assert B_MARKER not in r.text, r.text[:400]
        # It landed on the fork, not on the owner's thread.
        share = _share_record(made["share_id"])
        vid = anon.cookies[VISITOR_COOKIE]
        assert _edition.conversation_service.history(fork_key(share, vid), conv), (
            "the streamed turn was not recorded on the visitor's fork")
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_the_two_ask_routes_return_the_same_keys():
    """#736. The rule above is about behaviour; this one is about the SHAPE, and the shape is
    where the doorway actually drifted.

    `link_chat` hand-picks keys off a `QueryResult`; `link_chat/stream` forwards its done event
    by SUBTRACTION. Those are opposite defaults: adding a field to `QueryResult` reaches the
    stream automatically and the JSON route never, so the two halves of one capability diverge
    by DOING NOTHING. That is how `referenced` (#724) shipped on the stream and not on the JSON
    route and sat there unnoticed - the parity test above asserts four specific rules, and a
    missing key is not one of them, so nothing here could see it.

    Asserting the key sets rather than the presence of `referenced` is the point: this fails on
    the NEXT field too, in whichever direction it goes."""
    _seed_one_partition()
    conv = "c-736-keys"
    doc = _ingest("doc-736", A_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [doc])
    made = _make_link(conv)
    url = made["url"]
    try:
        q = {"question": "how many days carry over?"}
        j = _visitor().post(url + "/chat", json=q)
        assert j.status_code == 200, j.text[:300]
        json_keys = set(j.json())

        s = _visitor().post(url + "/chat/stream", json=q)
        assert s.status_code == 200, s.text[:300]
        done = [ln for ln in s.text.splitlines() if '"done"' in ln]
        assert done, s.text[:400]
        import json as _json
        stream_keys = set(_json.loads(done[-1][len("data: "):]))

        # `type` is the SSE envelope's own discriminator and `retrieved_owners` is the account
        # id #576 deliberately strips from the wire - neither is a capability the JSON route is
        # missing. Everything else must match in BOTH directions.
        stream_keys -= {"type", "retrieved_owners"}
        # `conv_id` is JSON-only and stays that way: the stream's caller already holds the
        # token the id is reachable from, and the done event is consumed mid-render. It is
        # listed HERE, once, so that it is a decision rather than an accident - which is
        # exactly what `referenced` was not.
        json_only = json_keys - stream_keys
        assert json_only == {"conv_id"}, (
            f"the JSON ask route carries keys its stream twin does not: {sorted(json_only)}")
        stream_only = stream_keys - json_keys
        assert not stream_only, (
            "the stream ask route carries keys its JSON twin does not - a caller disables "
            f"that capability by choosing /chat: {sorted(stream_only)}")
        # ...and the field that started it, named explicitly so the failure says #736 rather
        # than "sets differ". Its VALUE is not compared across the two calls: they are two
        # separate generations, and a test that pinned their answers to each other would be
        # asserting a property of the stub llm rather than of this route.
        assert "referenced" in json_keys, j.json()
        assert isinstance(j.json()["referenced"], list), j.json()["referenced"]
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_a_visitor_question_never_asks_a_directory_who_the_sentinel_is():
    """#605 review, Finding 3 - a REAL functional defect, and one no memory-backed rig could
    have shown on its own.

    `link:<share_id>` names no person. The memory identity adapter answers `[oid]` for any
    string, so nothing here noticed that a visitor's question was asking a DIRECTORY to resolve
    it; `EntraIdentity.expand_groups` POSTs `/users/link:<share_id>/getMemberGroups` to
    Microsoft Graph and calls `raise_for_status()`, so on `SELFHOST_BACKEND=azure` every single
    visitor question 500s and the feature is dead on arrival. Fail-closed, so never a
    disclosure - just broken, on the deployment that matters most.

    The double raises for EVERY oid, deliberately: a double that only raised for `link:` oids
    would pass whether or not the adapter was consulted, which is the whole question. A visitor
    must be answered while it is installed, which is only possible if the sentinel never
    reaches it at all."""
    _seed_one_partition()
    conv = "c-605-nodirectory"
    doc = _ingest("doc-605-nodirectory", A_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [doc])
    made = _make_link(conv)
    anon = _visitor()
    assert anon.get(made["url"]).status_code == 200

    class _NoSuchUser:
        """An identity backend that behaves like Graph asked about somebody who is not there."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def expand_groups(self, user_oid):
            raise RuntimeError(f"404 Not Found: /users/{user_oid}/getMemberGroups")

    real = _edition.identity._inner
    _edition.identity._inner = _NoSuchUser(real)
    try:
        r = anon.post(made["url"] + "/chat", json={"question": "how many days carry over?"})
        assert r.status_code == 200, (
            f"a visitor question went to the directory adapter: {r.status_code} {r.text[:300]}")
        body = r.json()
        assert doc in body["retrieved_docs"] and A_MARKER in body["answer"], body
        assert body["corpus"]["authorized_docs"] == 1, body["corpus"]
        # The double really is installed and really does raise - so the pass above is the
        # sentinel being short-circuited, not the double being inert.
        try:
            _edition.identity._inner.expand_groups(BOB)
            raise AssertionError("the exploding directory double did not raise")
        except RuntimeError:
            pass
    finally:
        _edition.identity._inner = real
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_opening_a_link_is_counted_for_the_owner():
    """ADR 0021's accepted consequence: how often a link was opened is the only signal an owner
    gets that it travelled further than she meant. A counter, never an authorization fact - and
    it must never be able to break the page it counts."""
    _seed_one_partition()
    conv = "c-605-opens"
    doc = _ingest("doc-605-opens", A_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [doc])
    made = _make_link(conv)
    try:
        assert made["opens"] == 0, made
        _visitor().get(made["url"])
        _visitor().get(made["url"])
        share = _share_record(made["share_id"])
        assert share.opens == 2, share.opens
        assert share.last_open_at is not None
        # Reads and asks are NOT opens - only a page load is.
        anon = _visitor()
        anon.get(made["url"])
        anon.get(made["url"] + "/transcript")
        anon.post(made["url"] + "/chat", json={"question": "how many days carry over?"})
        assert _share_record(made["share_id"]).opens == 3, (
            "something other than a page load is counting as an open")
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_a_forged_visitor_cookie_cannot_reach_another_fork_or_become_an_identity():
    """The cookie is a fork key, not a credential.

    Two properties, both asserted: a value that is not a freshly-minted-shaped fork id is
    REPLACED rather than trusted (so nothing hand-written can be concatenated into a
    conversation store key), and no value in this cookie ever becomes an identity - the six
    surfaces stay 401 whatever it holds. The token in the URL is the only thing that authorizes
    anything here."""
    _seed_one_partition()
    conv = "c-605-forged"
    doc = _ingest("doc-605-forged", A_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [doc])
    made = _make_link(conv)
    url = made["url"]
    try:
        forged = _visitor()
        forged.cookies.set(VISITOR_COOKIE, "v_aaa:../../acct_bob",
                           domain="testserver", path="/c")
        r = forged.get(url)
        assert r.status_code == 200
        # The jar can legitimately hold the forged entry AND the server's replacement, so read
        # what the RESPONSE actually set rather than whichever one the jar hands back first.
        minted = r.cookies[VISITOR_COOKIE]
        assert minted.startswith("v_"), minted
        assert ":" not in minted and "/" not in minted, (
            f"a hand-written fork key survived into the store key: {minted}")
        # Holding it is not an identity anywhere else.
        assert forged.post("/search", json={"question": "leave"}).status_code == 401
        # ...and it is not an identity HERE either: without the token there is no doorway.
        assert forged.get("/c/" + "f" * 32).status_code == 404
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def _keys_anywhere(obj) -> set:
    """Every dict key reachable in a decoded body, at any depth.

    Asserting on `body["citations"][0]` would only ever check the field somebody remembered to
    look at. The uri reached a stranger for a whole branch precisely because nothing swept the
    WHOLE payload, so the sweep is the assertion here and the named field is the convenience."""
    found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(k)
            found |= _keys_anywhere(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= _keys_anywhere(v)
    return found


def _no_leak(raw: str, decoded, where: str) -> None:
    """The whole serialized body, twice over: no source-location KEY survives, and no piece of
    the fixture's URL appears as TEXT anywhere in the bytes that were actually sent."""
    keys = _keys_anywhere(decoded)
    assert "uri" not in keys, f"{where}: a source uri key reached an anonymous visitor: {keys}"
    assert "locator" not in keys, f"{where}: a locator key reached an anonymous visitor: {keys}"
    for marker in SP_LEAK_MARKERS:
        assert marker not in raw, (
            f"{where}: '{marker}' - part of the document's internal source URL - was in the "
            f"body served to a stranger: {raw[:400]}")


def test_a_visitor_is_never_handed_the_source_uri_of_a_cited_document():
    """#612 review, Finding 1. ADR 0021 invariant 2: "Citations expose filenames and the cited
    passages, nothing more." Before this fix the code did not do that.

    `QueryService._context_and_citations` builds `{doc, title, uri, locator}` and the link
    routes returned it verbatim, so a stranger holding a forwarded URL was handed - and, via
    `sourceCard`/`safeHref`, SHOWN as a clickable link with the URL printed beneath it - the
    tenant, the site, the library, the folder path and the filename of every document the
    answer drew on. None of that is a filename and none of it is a cited passage.

    Asserted on the SERIALIZED body of all three visitor routes rather than on a citation dict,
    because a leak arrives through the field nobody thought to check; and the signed-in path is
    asserted to still HAVE both keys, because the rule is about the AUDIENCE - a link is
    forwardable so its readership is unbounded - and not about citations being too detailed."""
    _seed_one_partition()
    conv = "c-612-uri"
    doc = _ingest("doc-612-uri", C_TEXT, uri=SP_URI)
    _turn(conv, C_QUESTION, f"The schedule pays {C_MARKER}.", [doc])
    made = _make_link(conv)
    url = made["url"]
    try:
        anon = _visitor()
        anon.get(url)

        t = anon.get(url + "/transcript")
        assert t.status_code == 200, t.text[:300]
        _no_leak(t.text, t.json(), "the visitor transcript")

        r = anon.post(url + "/chat", json={"question": C_QUESTION})
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        # POSITIVE CONTROL: the citation still arrived, so this is a narrowed citation and not
        # an empty one. Without it every assertion below passes on a broken retrieval.
        assert body["citations"], f"the visitor received no citation at all: {body}"
        assert body["citations"][0]["doc"] == doc, body["citations"][0]
        assert body["citations"][0].get("title"), body["citations"][0]
        _no_leak(r.text, body, "the visitor's /chat answer")

        s = anon.post(url + "/chat/stream", json={"question": C_QUESTION})
        assert s.status_code == 200, s.text[:300]
        done = [ln for ln in s.text.splitlines() if '"done"' in ln]
        assert done, s.text[:400]
        import json as _json
        ev = _json.loads(done[-1][len("data: "):])
        assert ev["citations"], f"the streamed answer carried no citation at all: {ev}"
        _no_leak(s.text, ev, "the visitor's /chat/stream answer")

        # THE OTHER DIRECTION. The owner signs in and asks the same question in the same
        # conversation: her citation keeps the uri, because she has an account here and the uri
        # is how she opens a source she is already entitled to open (#165 provenance).
        mine = client.post("/chat", cookies=CK[BOB],
                           json={"question": C_QUESTION, "conv_id": conv})
        assert mine.status_code == 200, mine.text[:300]
        cites = mine.json()["citations"]
        assert cites, f"the signed-in owner received no citation: {mine.json()}"
        # Every citation, not only ours: the property is "the signed-in shape is unchanged".
        for c in cites:
            assert "uri" in c and "locator" in c, (
                f"the signed-in path lost a source-location key - the strip was applied to the "
                f"shared citation builder instead of to the visitor's response: {c}")
        ours = [c for c in cites if c["doc"] == doc]
        assert ours, f"the owner's own question did not retrieve the fixture document: {cites}"
        assert ours[0]["uri"] == SP_URI, ours[0]
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


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
