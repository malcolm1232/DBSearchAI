"""#606 / #610: the share POST learns AUDIENCES, and the owner can narrow the share first.

Two things land together here because they are one product act - the share modal shows the
owner exactly which documents a share would expose, she unchecks the ones she did not mean to,
and then picks who it is for:

  audience      "people" is ADR 0020 unchanged (a named grantee who signs in); "link" is
                ADR 0021 (anyone holding an unguessable token, who signs in to nothing).
  exclude_docs  can only NARROW, never widen and never probe.

WHY THE SINGLE-PARTITION RIG, and it is not a detail. tests/selftest_600_share_api.py's
top half puts every identity in its own private `acct:` partition (ADR 0018), and in that
shape the partition filter silently does the work a missing authorization check should be
doing - two real CRITICALs on the previous branch were invisible for exactly that reason.
Every test here therefore runs in the ordinary single-organization deployment: four
identities, ONE partition, nothing between them but the rules. The fixture helpers are
tests/selftest_600_share_api.py's, deliberately copied rather than re-invented.

THE ASSERTION THAT MATTERS IS ON CONTENT, NOT ON GRANTS. Three defects on this branch had the
identical shape: the fix closed the GRANT channel and left the CONTENT channel open beside
it. A document reaches somebody as a permission OR as prose in an answer synthesized from it,
and guarding one says nothing about the other. So an excluded document is asserted to stop the
transcript at the turn that cited it, not merely to be absent from the grant set.

DEFERRED TO TASK 4: the anonymous `/c/{token}` doorway does not exist yet, so "the visitor
transcript stops before the excluded document's turn" is asserted here through the boundary
that DECIDES it - the share row's `turn_cutoff`, which is what Task 4's transcript will slice
by - plus the text of the turns that boundary admits. When the route lands, the same property
should be re-asserted against the real visitor response.

    PYTHONPATH=src python3 tests/selftest_606_share_scope_narrowing.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")
# The public demo's per-IP cap is installed at import time and every test here is several real
# HTTP calls; without this the file starts returning 429s partway through and every later test
# fails on its own seed ingest - a rig failure that reads exactly like a regression.
os.environ["DBSEARCH_RATE_LIMIT"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.query.conversation import Turn  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.conversation_shares import LINK_GRANTEE_PREFIX  # noqa: E402
# The two refusal messages are imported rather than copied: this file pins WHICH cause the
# route reports, not how that cause is worded, and a copied string would turn a later rewording
# into a failing test while leaving the two causes free to swap places unnoticed.
from dbsearch.server.app import (ACCOUNTS, app, _edition,  # noqa: E402
                                 _EXCLUDED_EVERYTHING, _NOTHING_TO_SHARE)

client = TestClient(app)

ALICE = "acct_alice"
BOB = "acct_bob"
CAROL = "acct_carol"
DAVE = "acct_dave"
HOME_TID = "tid-606-narrowing-home"
_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET")

# The two markers every content assertion in this file is written against. Vocabulary
# deliberately disjoint from each other and from the rest of the suite's pile of near-identical
# seed documents - retrieval is fuzzy and a shared vocabulary would let a sibling document
# decide what a test proves.
DOC_A_TEXT = "The Osaka relocation stipend in doc-606-a covers 74 nights."
DOC_B_TEXT = "TOPSECRET-VALPARAISO: doc-606-b ferry covers 19 crossings."


def _real_login():
    for k in _VARS:
        os.environ.pop(k, None)
    os.environ.update({"AUTH_TENANT_ID": HOME_TID, "AUTH_CLIENT_ID": "cid",
                       "AUTH_CLIENT_SECRET": "sec"})


def _seed_one_partition():
    """Four identities signing in with the HOME tenant tid, so `canonical_partition` puts all
    four in the deployment's own partition - a real org, where the partition filter protects
    nobody from anybody."""
    _real_login()
    for who, email in ((ALICE, "alice@x.com"), (BOB, "bob@x.com"),
                       (CAROL, "carol@x.com"), (DAVE, "dave@x.com")):
        ACCOUNTS.resolve("local", email, preferred_account_id=who, email=email)
    one = _edition.tenant_id
    return {who: {user_auth.COOKIE: user_auth.sign_session(
        {"oid": who, "tid": one, "exp": int(time.time()) + 3600})}
        for who in (ALICE, BOB, CAROL, DAVE)}


def _ingest(doc_id: str, owner: str, ck: dict, text: str) -> str:
    r = client.post("/ingest", cookies=ck[owner], json={
        "external_id": doc_id, "title": f"policy {doc_id}", "acl": [owner],
        "text": text, "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, f"seed ingest failed: {r.status_code} {r.text[:200]}"
    return doc_id


def _thread(conv: str, ck: dict, doc_a: str, doc_b: str):
    """A two-turn thread: turn 0 cites doc_a, turn 1 cites doc_b.

    Appended through the store, the way tests/selftest_600_share_api.py does and for the same
    reason: the citation PATTERN is the thing under test, and driving it through /chat lets a
    run-long shared in-memory index and a condense step decide which turn cites what."""
    store = _edition.conversation_service._store
    store.append(conv, BOB, Turn(question="how many nights does the Osaka stipend cover?",
                                 standalone="Osaka relocation stipend nights",
                                 answer="74 nights.", cited_docs=[doc_a]))
    store.append(conv, BOB, Turn(question="and the TOPSECRET-VALPARAISO ferry?",
                                 standalone="TOPSECRET-VALPARAISO ferry crossings",
                                 answer="TOPSECRET-VALPARAISO: 19 crossings.",
                                 cited_docs=[doc_b]))


def _link_grants(share_id: str, conv: str) -> set:
    """What the link's SENTINEL principal can actually reach in this conversation - the grant
    channel, read through the one ACL enforcement point (`live_principals_for`), scoped to
    this conversation exactly as a read would scope it."""
    principal_of = {g.principal: g.doc_external_id
                    for g in _edition.grant_registry.live_grants_for(
                        LINK_GRANTEE_PREFIX + share_id)}
    return {principal_of[p] for p in _edition.grant_registry.live_principals_for(
        LINK_GRANTEE_PREFIX + share_id, conv) if p in principal_of}


def _served_text(conv: str, cutoff: int) -> str:
    """The transcript a visitor would be handed: the grantor's first `cutoff` turns, which is
    exactly what the share row's boundary means and what Task 4's `/c/{token}/transcript` will
    slice by (`conversation_transcript` already slices the named-grantee case this way)."""
    turns = _edition.conversation_service.history(BOB, conv)[:cutoff]
    return " ".join(t.question + " " + t.standalone + " " + t.answer for t in turns)


def _revoke(share_id: str, ck: dict):
    try:
        client.delete(f"/conversations/shares/{share_id}", cookies=ck[BOB])
    except Exception:
        pass


# ---- exclude_docs narrows BOTH channels --------------------------------------------------

def test_exclude_docs_narrows_the_share_and_the_transcript():
    """THE test of this task. The thread cites doc_a (turn 0) and doc_b (turn 1); the owner
    unchecks doc_b in the modal.

    Both channels are asserted, because closing one of them proves nothing about the other:

      GRANT channel   the link's sentinel principal reaches doc_a and only doc_a.
      CONTENT channel the shared transcript STOPS BEFORE doc_b's turn, so the answer text
                      synthesized out of doc_b never travels. An excluded document is treated
                      exactly like an unshareable one - if the exclusion only filtered the
                      grants, `documents: 1` would be reported while the recipient read
                      "TOPSECRET-VALPARAISO: 19 crossings" in the transcript, which is the
                      disclosure this feature exists to prevent, by the other route."""
    ck = _seed_one_partition()
    conv = "c-606-narrow"
    doc_a = _ingest("doc-606-a", BOB, ck, DOC_A_TEXT)
    doc_b = _ingest("doc-606-b", BOB, ck, DOC_B_TEXT)
    _thread(conv, ck, doc_a, doc_b)
    share_id = None
    try:
        r = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB], json={
            "audience": "link", "expires_in_days": 7, "exclude_docs": [doc_b]})
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        share_id = body["share_id"]
        assert body["url"].startswith("/c/") and len(body["url"]) > 10, body
        assert body["documents"] == 1, f"the exclusion did not narrow the share: {body}"
        assert body["audience"] == "link", body
        assert body["grantee_oid"] == LINK_GRANTEE_PREFIX + share_id, body

        # THE GRANT CHANNEL.
        reached = _link_grants(share_id, conv)
        assert reached == {doc_a}, (
            f"the link grants a document the owner unchecked: {reached}")

        # THE CONTENT CHANNEL - and this is the assertion that would still have failed after
        # every "fix" that only filtered grants.
        assert body["turn_cutoff"] == 1, (
            f"the shared transcript does not stop before the excluded document's turn: {body}")
        assert body["turns_withheld"] == 1, body
        served = _served_text(conv, body["turn_cutoff"])
        assert "TOPSECRET-VALPARAISO" not in served and "19 crossings" not in served, (
            f"the excluded document reached the visitor as transcript text: {served[:300]}")
        assert "Osaka" in served, f"the fixture shared nothing at all: {served[:300]}"
    finally:
        if share_id:
            _revoke(share_id, ck)


def test_exclude_docs_cannot_widen_or_probe():
    """Unknown ids subtract nothing and reveal nothing.

    An id that is not in the thread, not in the corpus, or not this caller's must behave
    exactly like one that was never cited: it does nothing. If it errored, `exclude_docs`
    would be a PROBE - pass an id, read the status, learn whether that document exists inside
    a thread. And it can never WIDEN: the ids are subtracted from a server-computed set, so
    naming doc_b's neighbour cannot add it."""
    ck = _seed_one_partition()
    conv = "c-606-probe"
    doc_a = _ingest("doc-606-probe-a", BOB, ck, DOC_A_TEXT.replace("doc-606-a", "doc-606-probe-a"))
    doc_b = _ingest("doc-606-probe-b", BOB, ck, DOC_B_TEXT.replace("doc-606-b", "doc-606-probe-b"))
    # Carol's document exists and is NOT in bob's thread: naming it must not tell him so.
    carols = _ingest("doc-606-probe-carols", CAROL, ck,
                     "The Bergen commuting allowance in doc-606-probe-carols covers 33 bus days.")
    _thread(conv, ck, doc_a, doc_b)
    share_id = None
    try:
        r = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB], json={
            "audience": "link", "exclude_docs": ["not-a-doc", "../../etc/passwd", carols]})
        assert r.status_code == 200, (
            f"an unknown exclude_docs id was answered differently from a known one - the "
            f"parameter is a probe: {r.status_code} {r.text[:300]}")
        body = r.json()
        share_id = body["share_id"]
        # Nothing was subtracted, so the share is exactly what it would have been.
        assert body["documents"] == 2, body
        assert body["turn_cutoff"] == 2 and body["turns_withheld"] == 0, body
        assert _link_grants(share_id, conv) == {doc_a, doc_b}, _link_grants(share_id, conv)
        # ...and carol's document did not come along for the ride.
        assert carols not in _link_grants(share_id, conv)
    finally:
        if share_id:
            _revoke(share_id, ck)


def test_excluding_everything_refuses_the_share():
    """A share of nothing is refused, not minted. 400, with no share row and no grant left
    behind - a link whose URL opens an empty transcript is worse than an error, because the
    owner sends it to somebody and only they find out."""
    ck = _seed_one_partition()
    conv = "c-606-all"
    doc_a = _ingest("doc-606-all-a", BOB, ck, DOC_A_TEXT.replace("doc-606-a", "doc-606-all-a"))
    doc_b = _ingest("doc-606-all-b", BOB, ck, DOC_B_TEXT.replace("doc-606-b", "doc-606-all-b"))
    _thread(conv, ck, doc_a, doc_b)
    r = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB], json={
        "audience": "link", "exclude_docs": [doc_a, doc_b]})
    assert r.status_code == 400, f"{r.status_code} {r.text[:300]}"
    assert "unchecked" in r.json()["detail"], (
        f"the owner is told the SERVER refused her documents, which is not what happened: "
        f"{r.json()}")
    assert client.get(f"/conversations/{conv}/shares",
                      cookies=ck[BOB]).json()["shares"] == [], (
        "a refused share left a phantom share row behind")
    held = [g for g in _edition.grant_registry.live_grants_for(BOB) if g.conv_id == conv]
    assert not held, f"a refused share left grants standing: {held}"


def test_the_refusal_names_the_cause_that_actually_fired():
    """Fix round 1, FINDING 2. The 400 that refuses an empty share reports a CAUSE, and the
    cause must be the one that fired.

    The branch used to be on whether `exclude_docs` was PRESENT in the request, so a thread
    citing only a document somebody else shared with the owner - refused by ADR 0017 s2, not by
    her - was told "every document this conversation can pass on was unchecked" the moment the
    request carried an id that matched nothing, while the identical request without the field
    told her the truth. No leak (the input is her own), but a wrong explanation sends her to
    un-tick boxes that are already ticked instead of to the document's owner.

    THE CASE THAT DISTINGUISHES THEM is one request, run twice: same thread, same refusal,
    once with an `exclude_docs` that removes nothing. Both must give the s2 message. The third
    call is the control - an exclusion that really does empty the share gets the other one, or
    this test would pass just as well against a route that only ever said one thing."""
    ck = _seed_one_partition()
    conv = "c-606-cause"
    hers = _ingest("doc-606-cause-hers", CAROL, ck,
                   "TOPSECRET-LISBON: doc-606-cause-hers tram subsidy covers 11 lines.")
    grant = client.post(f"/documents/{hers}/grants", cookies=ck[CAROL],
                        json={"grantee_oid": BOB})
    assert grant.status_code == 200, grant.text[:200]
    try:
        # A thread whose ONLY citation is the document carol shared with bob: nothing here is
        # his to pass on, and no exclusion he could write would change that.
        _edition.conversation_service._store.append(conv, BOB, Turn(
            question="how many tram lines does the subsidy cover?",
            standalone="TOPSECRET-LISBON tram subsidy lines",
            answer="TOPSECRET-LISBON: 11 lines.", cited_docs=[hers]))

        plain = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                            json={"audience": "link"})
        assert plain.status_code == 400, plain.text[:300]
        assert plain.json()["detail"] == _NOTHING_TO_SHARE, plain.json()

        noop = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB], json={
            "audience": "link", "exclude_docs": ["totally-unknown-id"]})
        assert noop.status_code == 400, noop.text[:300]
        assert noop.json()["detail"] == plain.json()["detail"], (
            f"an exclusion that removed nothing changed the reason the share was refused:\n"
            f"  without exclude_docs: {plain.json()['detail']}\n"
            f"  with an unknown id:   {noop.json()['detail']}")

        # CONTROL: an exclusion that really does empty the share says so, or the assertions
        # above would hold against a route that never distinguishes the two causes at all.
        mine = _ingest("doc-606-cause-mine", BOB, ck,
                       DOC_A_TEXT.replace("doc-606-a", "doc-606-cause-mine"))
        own_conv = "c-606-cause-own"
        _edition.conversation_service._store.append(own_conv, BOB, Turn(
            question="how many nights does the Osaka stipend cover?",
            standalone="Osaka relocation stipend nights", answer="74 nights.",
            cited_docs=[mine]))
        real = client.post(f"/conversations/{own_conv}/shares", cookies=ck[BOB], json={
            "audience": "link", "exclude_docs": [mine, "totally-unknown-id"]})
        assert real.status_code == 400, real.text[:300]
        assert real.json()["detail"] == _EXCLUDED_EVERYTHING, real.json()
    finally:
        client.delete(f"/grants/{grant.json()['grant_id']}", cookies=ck[CAROL])


# ---- the pre-share scope confirmation ----------------------------------------------------

def test_shareable_lists_cited_docs_for_the_owner_only():
    """What the modal renders BEFORE a share exists - and only to the person whose thread it
    is. 404 for everybody else, never 403: existence is the secret (the #549 rule), and the
    refusal comes from the same empty-history mechanism the share POST uses, so the two cannot
    drift into disagreeing about whose thread this is."""
    ck = _seed_one_partition()
    conv = "c-606-shareable"
    doc_a = _ingest("doc-606-sh-a", BOB, ck, DOC_A_TEXT.replace("doc-606-a", "doc-606-sh-a"))
    doc_b = _ingest("doc-606-sh-b", BOB, ck, DOC_B_TEXT.replace("doc-606-b", "doc-606-sh-b"))
    _thread(conv, ck, doc_a, doc_b)

    r = client.get(f"/conversations/{conv}/shareable", cookies=ck[BOB])
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert {d["id"] for d in body["documents"]} == {doc_a, doc_b}, body
    assert all(d["shareable"] is True for d in body["documents"]), (
        f"bob's own documents are not offered as shareable: {body}")
    assert body["turns"] == 2, body
    assert all(d["title"] for d in body["documents"]), body

    for stranger in (CAROL, ALICE, DAVE):
        r2 = client.get(f"/conversations/{conv}/shareable", cookies=ck[stranger])
        assert r2.status_code == 404, (
            f"{stranger} can see what bob's thread would share: {r2.status_code} "
            f"{r2.text[:200]}")
        assert doc_a not in r2.text and "Osaka" not in r2.text, r2.text[:200]


def test_shareable_flags_a_received_document_rather_than_hiding_it():
    """ADR 0017 s2 through the SAME `_shareable_docs` the mint runs: a document bob only holds
    through carol's grant is not his to pass on. It is LISTED with `shareable: false` rather
    than hidden - he cited it in his own thread and can already see it on "Your data", and
    hiding it would leave him counting fewer documents than the thread visibly drew on with no
    way to learn why half the transcript will not travel."""
    ck = _seed_one_partition()
    conv = "c-606-received"
    mine = _ingest("doc-606-rc-mine", BOB, ck, DOC_A_TEXT.replace("doc-606-a", "doc-606-rc-mine"))
    hers = _ingest("doc-606-rc-hers", CAROL, ck,
                   "TOPSECRET-LISBON: doc-606-rc-hers tram subsidy covers 11 lines.")
    grant = client.post(f"/documents/{hers}/grants", cookies=ck[CAROL],
                        json={"grantee_oid": BOB})
    assert grant.status_code == 200, grant.text[:200]
    try:
        _thread(conv, ck, mine, hers)
        body = client.get(f"/conversations/{conv}/shareable", cookies=ck[BOB]).json()
        flags = {d["id"]: d["shareable"] for d in body["documents"]}
        assert flags == {mine: True, hers: False}, (
            f"the modal would offer to share a document that is not bob's to pass on: {body}")
    finally:
        # A live #582 grant with conv_id None contributes a doorway pair in every conversation
        # bob opens for the rest of the run; left standing it makes a later test fail for a
        # reason that has nothing to do with what that test is about.
        client.delete(f"/grants/{grant.json()['grant_id']}", cookies=ck[CAROL])


# ---- the audience selects a path; it is never the authorization ---------------------------

def test_a_link_is_always_bounded_in_time_and_the_people_path_is_untouched():
    """ADR 0021 invariant 3: every link is bounded in time, default 7 days. `create_link`
    refuses 0 and negatives but ACCEPTS None, so the default has to be supplied by the route -
    a link minted with no expiry is the one shape the invariant forbids.

    The people path is asserted in the same test, deliberately: ADR 0020 never bounded a named
    share in time, and a default that leaked across would silently expire every existing
    share."""
    ck = _seed_one_partition()
    conv = "c-606-expiry"
    doc_a = _ingest("doc-606-ex-a", BOB, ck, DOC_A_TEXT.replace("doc-606-a", "doc-606-ex-a"))
    _edition.conversation_service._store.append(conv, BOB, Turn(
        question="how many nights does the Osaka stipend cover?",
        standalone="Osaka relocation stipend nights", answer="74 nights.",
        cited_docs=[doc_a]))
    ids = []
    try:
        r = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                        json={"audience": "link"})
        assert r.status_code == 200, r.text[:300]
        ids.append(r.json()["share_id"])
        assert r.json()["expires_at"], (
            f"a link was minted that never expires - ADR 0021 invariant 3: {r.json()}")

        # An explicit 0 is REFUSED rather than quietly turned into the default: it is the one
        # value that would mint an unbounded link, and answering it with 7 days would hide a
        # caller error instead of reporting it.
        zero = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                           json={"audience": "link", "expires_in_days": 0})
        assert zero.status_code == 400, f"{zero.status_code} {zero.text[:300]}"

        # The people path: no expiry named stays no expiry, exactly as before #606.
        p = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                        json={"grantee_oid": ALICE})
        assert p.status_code == 200, p.text[:300]
        ids.append(p.json()["share_id"])
        assert p.json()["expires_at"] is None, (
            f"the link default leaked into a named share: {p.json()}")
        assert p.json()["audience"] == "people" and "url" not in p.json(), (
            f"a people share was handed a bearer URL: {p.json()}")
    finally:
        for sid in ids:
            _revoke(sid, ck)
        _edition.grant_registry.drop_for_conversation(conv, ALICE)


def test_an_unrecognised_audience_is_refused_not_defaulted():
    """`audience` is a client-supplied string that SELECTS A CODE PATH and is never an
    authorization fact - what gates a link visitor is the token hash on the row and the row's
    liveness. Since the two paths hand over different things, a typo is refused rather than
    quietly resolved to one of them."""
    ck = _seed_one_partition()
    conv = "c-606-audience"
    doc_a = _ingest("doc-606-au-a", BOB, ck, DOC_A_TEXT.replace("doc-606-a", "doc-606-au-a"))
    _edition.conversation_service._store.append(conv, BOB, Turn(
        question="how many nights does the Osaka stipend cover?",
        standalone="Osaka relocation stipend nights", answer="74 nights.",
        cited_docs=[doc_a]))
    # "people " is NOT in this list: a padded value of a real audience is normalized, the same
    # stance `_conv_id` takes on a padded conv_id. What is refused is a value that names no
    # audience at all - including "LINK", because a case-insensitive match here would be a
    # second spelling of a code-path selector that other modules compare exactly.
    for bad in ("links", "LINK", "anyone", "public", "people,link"):
        r = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                        json={"audience": bad, "grantee_oid": ALICE})
        assert r.status_code == 400, (
            f"audience={bad!r} was accepted: {r.status_code} {r.text[:200]}")
        assert "url" not in r.text, r.text[:200]
    assert client.get(f"/conversations/{conv}/shares",
                      cookies=ck[BOB]).json()["shares"] == [], "a refused audience minted a row"


def test_the_token_leaves_the_server_exactly_once():
    """ADR 0021: the plaintext token exists in this one response and nowhere else. The row
    keeps only a SHA-256 digest, so no management surface, no listing and no later read can
    hand it back - which is the whole reason a leaked database read authorizes nothing."""
    ck = _seed_one_partition()
    conv = "c-606-token"
    doc_a = _ingest("doc-606-tk-a", BOB, ck, DOC_A_TEXT.replace("doc-606-a", "doc-606-tk-a"))
    _edition.conversation_service._store.append(conv, BOB, Turn(
        question="how many nights does the Osaka stipend cover?",
        standalone="Osaka relocation stipend nights", answer="74 nights.",
        cited_docs=[doc_a]))
    share_id = None
    try:
        r = client.post(f"/conversations/{conv}/shares", cookies=ck[BOB],
                        json={"audience": "link"})
        assert r.status_code == 200, r.text[:300]
        share_id = r.json()["share_id"]
        token = r.json()["url"].split("/c/")[1]
        assert len(token) >= 32, f"the token is not 128 bits of entropy: {token}"
        assert "token_hash" not in r.text, r.text[:300]

        listed = client.get(f"/conversations/{conv}/shares", cookies=ck[BOB])
        assert token not in listed.text and "token_hash" not in listed.text, (
            f"the management listing hands the bearer token back out: {listed.text[:300]}")
        # It really is the credential, and the row really only holds its digest.
        row = _edition.conversation_shares.find(share_id, BOB)
        assert row.token_hash and row.token_hash != token, row
        assert _edition.conversation_shares.find_by_token(token) is not None, (
            "the token this response returned does not open its own share")
    finally:
        if share_id:
            _revoke(share_id, ck)


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
            except Exception as e:
                # An error is a failure, and is counted as one: these tests drive real routes,
                # so a regression can surface as a 500 raised out of the TestClient rather than
                # as a failed assertion, and a bare `except AssertionError` would abort the run
                # and leave every later test unreported.
                print(f"  FAIL  {name}: {type(e).__name__}: {e}"); failed += 1
    for k in _VARS:
        os.environ.pop(k, None)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
