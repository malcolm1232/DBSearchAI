"""#611 / ADR 0021 consequences: the owner reads what strangers asked, and the stranger is
told so at the point of asking.

Two obligations, and they are inseparable - which is why they are one card and one test file.
The owner asked for the log ("yes, want to see what strangers asked", spec s5). ADR 0021's
accepted consequences turn that into a DISCLOSURE OBLIGATION on the other side of the same
link: "a visitor is typing into a page without knowing the owner reads it, so the share page
must say so plainly and visibly at the point of asking - not in a footer, not only in a policy
document a visitor never opens."

WHAT THIS FILE PINS, and why each one is the thing a later change would get wrong:

  QUESTIONS ONLY.       The log carries what a visitor typed and when, never the synthesized
                        ANSWER. Asserted on the SERIALIZED response, not field by field: a
                        leak arrives through a key nobody thought to check, and every defect
                        this feature has produced travelled the content channel while the
                        grant channel looked shut.
  ORDINALS, NEVER THE   Visitors are 1, 2, 3 in first-seen order. The fork-key cookie is a
  COOKIE.               tracking key; putting it in an owner-facing response would turn it
                        into a stable identifier the owner could correlate - worse than
                        content, because it is a person-tracker (LAW 1).
  OWNER ONLY.           `find(share_id, requester)` is the check, so "not yours" and "no such
                        share" are one 404.
  A PEOPLE SHARE LOGS   Its grantee signs in and reads her own thread; there is no anonymous
  NOTHING.              traffic to log. An empty list, never an error - the case a future
                        refactor is most likely to get wrong.
  THE VISITOR IS TOLD.  The disclosure sentence is pinned character for character ON THE
                        RESPONSE `GET /c/{token}` HANDS A COOKIE-LESS CLIENT. It was pinned by
                        grepping static/js/surfaces/ask.js when this file first shipped, and
                        that was the defect in miniature: all five assertions were green while
                        `/c` was absent from SHELL_PATHS, so the share page rendered the
                        LANDING view and the disclosure node was built inside a container
                        never displayed. A string in a module is not a sentence on a page.

    PYTHONPATH=src python3 tests/selftest_611_visitor_question_log.py
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import _domgate  # noqa: E402  the shared node/jsdom gate (#792)
os.environ.setdefault("SELFHOST_BACKEND", "memory")
# Same rig rule as selftest_605: the public demo's per-IP cap is installed at import time and
# every test here is several real HTTP calls.
os.environ["DBSEARCH_RATE_LIMIT"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.query.conversation import Turn  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import ACCOUNTS, app, _edition  # noqa: E402
from dbsearch.server.conversation_shares import AUDIENCE_LINK  # noqa: E402
from dbsearch.server.link_access import (  # noqa: E402
    VISITOR_COOKIE, fork_key, link_principal)

client = TestClient(app)

STATIC = ROOT / "src/dbsearch/server/static"
ASK_JS = (STATIC / "js/surfaces/ask.js").read_text()

#: The exact sentence. Owner-facing product copy, pinned character for character - a later UI
#: task may move it, style it, or translate the page around it, but it may not soften it into
#: "questions may be visible" or file it under a policy link.
DISCLOSURE = "The person who shared this link can see the questions you ask here."

ALICE = "acct_alice"
BOB = "acct_bob"
HOME_TID = "tid-611-log-home"
CK = {}

A_TEXT = "The Lisbon carryover allowance in doc-611-a carries over 26 days of unused leave."
A_MARKER = "26 days"


def _seed_one_partition():
    """selftest_605's rig verbatim: two identities in the deployment's OWN partition, the
    single-org shape where a partition filter protects nobody from anybody."""
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
    return CK


def _ingest(doc_id: str, text: str, owner: str = BOB) -> str:
    r = client.post("/ingest", cookies=CK[owner], json={
        "external_id": doc_id, "title": f"policy {doc_id}", "acl": [owner],
        "text": text, "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, f"seed ingest failed: {r.status_code} {r.text[:200]}"
    return doc_id


def _turn(conv: str, question: str, answer: str, docs: list, who: str = BOB) -> None:
    _edition.conversation_service._store.append(conv, who, Turn(
        question=question, standalone=question, answer=answer, cited_docs=list(docs)))


def _make_link(conv: str, owner: str = BOB) -> dict:
    r = client.post(f"/conversations/{conv}/shares", cookies=CK[owner],
                    json={"audience": AUDIENCE_LINK})
    assert r.status_code == 200, f"share failed: {r.status_code} {r.text[:300]}"
    out = r.json()
    assert out["audience"] == AUDIENCE_LINK and out["url"].startswith("/c/"), out
    return out


def _visitor() -> TestClient:
    return TestClient(app)


def _log(conv: str, share_id: str, who: str = BOB):
    return client.get(f"/conversations/{conv}/shares/{share_id}/questions", cookies=CK[who])


def _share_record(share_id: str, owner: str = BOB):
    return _edition.conversation_shares.find(share_id, owner)


def _cleanup(conv: str, *grantees: str):
    for g in grantees:
        try:
            _edition.grant_registry.drop_for_conversation(conv, g)
        except Exception:
            pass


def _seeded_link(conv: str, doc_id: str, owner: str = BOB) -> dict:
    _ingest(doc_id, A_TEXT, owner=owner)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [doc_id],
          who=owner)
    return _make_link(conv, owner=owner)


# ---- the log itself -----------------------------------------------------------------------

def test_two_visitors_ask_and_the_owner_reads_both_with_ordinals():
    """The feature, end to end. Two strangers ask through one link; the owner reads a log that
    tells them apart WITHOUT naming either - `visitor: 1` and `visitor: 2` are positions in
    first-seen order, and the second question from the first visitor stays visitor 1."""
    _seed_one_partition()
    conv = "c-611-log"
    made = _seeded_link(conv, "doc-611-a")
    url = made["url"]
    try:
        a, b = _visitor(), _visitor()
        assert a.get(url).status_code == 200 and b.get(url).status_code == 200
        assert a.post(url + "/chat",
                      json={"question": "QUESTION-A1 how many days carry over?"}
                      ).status_code == 200
        assert b.post(url + "/chat",
                      json={"question": "QUESTION-B1 what is the allowance?"}
                      ).status_code == 200
        assert a.post(url + "/chat",
                      json={"question": "QUESTION-A2 and for part timers?"}).status_code == 200

        r = _log(conv, made["share_id"])
        assert r.status_code == 200, f"the owner cannot read her own link's log: {r.text[:300]}"
        body = r.json()
        assert body["visitors"] == 2, (
            f"two strangers asked; the log counted {body.get('visitors')}: {body}")
        asked = [(q["question"], q["visitor"]) for q in body["questions"]]
        assert len(asked) == 3, asked
        first = {q: v for q, v in asked}
        a1 = [v for q, v in asked if q.startswith("QUESTION-A1")]
        a2 = [v for q, v in asked if q.startswith("QUESTION-A2")]
        b1 = [v for q, v in asked if q.startswith("QUESTION-B1")]
        assert a1 and a2 and b1, f"a question the visitors asked is missing: {asked}"
        assert a1 == [1] and b1 == [2], (
            f"ordinals are not first-seen order 1, 2: {asked}")
        assert a2 == a1, (
            f"one visitor's two questions were numbered as two different people: {asked}")
        assert all(q["asked_at"] for q in body["questions"]), (
            f"the owner has no idea WHEN any of this was asked: {body}")
        assert set(first) == {q for q, _ in asked}
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_the_log_carries_no_answer_text_anywhere_in_the_response():
    """QUESTIONS ONLY, and the assertion is on the WHOLE serialized response.

    The owner can re-ask anything herself, so returning the synthesized answer here buys
    nothing and costs a second content channel out of a surface whose entire justification is
    "the owner wants to know what people ask" - and it would reproduce document content into a
    new place besides. The positive control comes first: the visitor's answer really did carry
    the document's marker, so the absence below is a rule firing and not an empty rig."""
    _seed_one_partition()
    conv = "c-611-noanswer"
    made = _seeded_link(conv, "doc-611-a")
    url = made["url"]
    try:
        a = _visitor()
        answered = a.post(url + "/chat", json={"question": "how many days carry over?"})
        assert answered.status_code == 200, answered.text[:300]
        assert A_MARKER in answered.json()["answer"], (
            "control: the visitor's own answer must contain the document marker, or this "
            "test proves nothing")

        r = _log(conv, made["share_id"])
        assert r.status_code == 200, r.text[:300]
        assert A_MARKER not in r.text, (
            f"the synthesized ANSWER text reached the question log: {r.text[:400]}")
        assert "answer" not in r.text, (
            f"the log response carries an answer field: {r.text[:400]}")
        assert "citations" not in r.text and "cited_docs" not in r.text, (
            f"the log response carries retrieval detail it has no need for: {r.text[:400]}")
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_the_log_never_carries_the_visitors_cookie_or_fork_key():
    """LAW 1, sharpened: the visitor cookie is not merely content, it is a PERSON-TRACKER.

    It is host-wide today (one browser carries the same value to every owner's link), so an
    owner-facing response that echoed it - raw, or inside the fork key that embeds it - would
    hand two different owners a value they could compare. The ordinal exists precisely so the
    owner can tell two visitors apart without ever being given something that identifies
    either. Asserted on the serialized response for the reason above: a tracking value leaks
    through the field nobody thought to check."""
    _seed_one_partition()
    conv = "c-611-nocookie"
    made = _seeded_link(conv, "doc-611-a")
    url = made["url"]
    try:
        a = _visitor()
        assert a.get(url).status_code == 200
        assert a.post(url + "/chat",
                      json={"question": "how many days carry over?"}).status_code == 200
        vid = a.cookies[VISITOR_COOKIE]
        assert vid, "rig: no fork-key cookie was minted"

        r = _log(conv, made["share_id"])
        assert r.status_code == 200, r.text[:300]
        assert vid not in r.text, (
            f"the visitor's tracking cookie was handed to the owner: {r.text[:400]}")
        share = _share_record(made["share_id"])
        assert fork_key(share, vid) not in r.text, (
            f"the fork key (which embeds the cookie) reached the owner: {r.text[:400]}")
        assert "link:" not in r.text, (
            f"a synthetic principal / fork key rode out on the log: {r.text[:400]}")
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_the_same_browser_is_visitor_one_on_two_different_owners_links():
    """The known privacy edge, carried forward from #605's review, verified rather than
    assumed.

    `dbsearch_visitor` is scoped to `/c` - the DOORWAY, not the share - so ONE browser carries
    the SAME fork-key value to every owner's link. Ordinals are computed PER SHARE, over that
    share's own forks, so both owners see "visitor 1" and neither is given anything that
    survives a comparison between the two responses. This test is what makes that a checked
    property instead of a hope: if either log ever carried a cross-share-stable value, the
    two owners could put their logs side by side and learn they were talking to one person.

    Deliberately TWO DIFFERENT OWNERS, not two shares of one owner: the same-owner case is the
    weaker one (she may correlate her own visitors, and the ordinal is all she gets anyway)."""
    _seed_one_partition()
    bob_conv, alice_conv = "c-611-x-bob", "c-611-x-alice"
    bob_link = _seeded_link(bob_conv, "doc-611-x-bob", owner=BOB)
    alice_link = _seeded_link(alice_conv, "doc-611-x-alice", owner=ALICE)
    try:
        one = _visitor()
        assert one.post(bob_link["url"] + "/chat",
                        json={"question": "ASKED-OF-BOB how many days?"}).status_code == 200
        vid = one.cookies[VISITOR_COOKIE]
        assert one.post(alice_link["url"] + "/chat",
                        json={"question": "ASKED-OF-ALICE how many days?"}).status_code == 200
        assert one.cookies[VISITOR_COOKIE] == vid, (
            "rig: the browser did not carry ONE cookie to both links, so this test would "
            "pass without the property it exists to check")

        b = _log(bob_conv, bob_link["share_id"], who=BOB)
        al = _log(alice_conv, alice_link["share_id"], who=ALICE)
        assert b.status_code == 200 and al.status_code == 200, (b.text[:200], al.text[:200])
        assert [q["visitor"] for q in b.json()["questions"]] == [1], b.json()
        assert [q["visitor"] for q in al.json()["questions"]] == [1], al.json()
        for r in (b, al):
            assert vid not in r.text, (
                f"a cross-share-stable visitor value reached an owner: {r.text[:400]}")
        # Neither log names the other's share, so there is nothing to join on but a timestamp.
        assert alice_link["share_id"] not in b.text and bob_link["share_id"] not in al.text
    finally:
        _cleanup(bob_conv, link_principal(_share_record(bob_link["share_id"], BOB)))
        _cleanup(alice_conv, link_principal(_share_record(alice_link["share_id"], ALICE)))


def test_a_non_owner_gets_the_same_404_as_a_share_that_never_existed():
    """`find(share_id, requester_oid)` is the check, and its one KeyError for both "no such
    share" and "not yours" is what keeps this 404 from being an oracle. Alice is a real
    signed-in account with a real session - the caller most likely to try - and an anonymous
    caller never reaches the route at all, because `current_user` 401s first."""
    _seed_one_partition()
    conv = "c-611-notyours"
    made = _seeded_link(conv, "doc-611-a")
    url = made["url"]
    try:
        a = _visitor()
        assert a.post(url + "/chat",
                      json={"question": "STRANGER-QUESTION about leave"}).status_code == 200

        mine = _log(conv, made["share_id"], who=BOB)
        assert mine.status_code == 200, "control: the owner must be able to read it"
        assert "STRANGER-QUESTION" in mine.text

        theirs = _log(conv, made["share_id"], who=ALICE)
        assert theirs.status_code == 404, (
            f"somebody else's link question log answered {theirs.status_code}: "
            f"{theirs.text[:300]}")
        assert "STRANGER-QUESTION" not in theirs.text, (
            f"a non-owner was given the questions anyway: {theirs.text[:300]}")
        nonexistent = _log(conv, "no-such-share-611", who=ALICE)
        assert nonexistent.status_code == theirs.status_code, (
            "'not yours' and 'never existed' answer differently - the 404 is an oracle")
        assert nonexistent.json() == theirs.json(), (nonexistent.text[:200], theirs.text[:200])

        anon = _visitor().get(f"/conversations/{conv}/shares/{made['share_id']}/questions")
        assert anon.status_code == 401, (
            f"an anonymous caller reached the question log: {anon.status_code}")
        assert "STRANGER-QUESTION" not in anon.text
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_a_people_audience_share_logs_nothing_and_is_not_an_error():
    """A people share's grantee signs in and reads her OWN thread under her OWN account key -
    there is no anonymous traffic to log, and her questions are hers, not the grantor's to
    read. So the answer is an empty list, not a 404 and not a 500.

    This is the case a future refactor is most likely to get wrong (an audience check dropped,
    a prefix scan widened), and getting it wrong in the wide direction would turn this route
    into a way for a grantor to read a named colleague's private follow-up questions."""
    _seed_one_partition()
    conv = "c-611-people"
    doc = _ingest("doc-611-people", A_TEXT)
    _turn(conv, "how much leave carries over?", f"It carries over {A_MARKER}.", [doc])
    r = client.post(f"/conversations/{conv}/shares", cookies=CK[BOB],
                    json={"grantee_oid": ALICE})
    assert r.status_code == 200, f"people share failed: {r.text[:300]}"
    share_id = r.json()["share_id"]
    try:
        # Alice asks her own follow-up inside the shared thread, under her own account key.
        asked = client.post("/chat", cookies=CK[ALICE],
                            json={"question": "ALICES-PRIVATE-FOLLOWUP about leave",
                                  "conv_id": conv})
        assert asked.status_code == 200, asked.text[:300]

        log = _log(conv, share_id)
        assert log.status_code == 200, (
            f"a people share's log is an error rather than an empty list: "
            f"{log.status_code} {log.text[:300]}")
        assert log.json() == {"questions": [], "visitors": 0}, log.json()
        assert "ALICES-PRIVATE-FOLLOWUP" not in log.text, (
            f"a named grantee's own questions were handed to the grantor: {log.text[:400]}")
    finally:
        _cleanup(conv, ALICE)


def test_the_log_is_read_from_the_share_record_not_the_path():
    """`conv_id` arrives in the path and is client-chosen; the share RECORD is the fact. A
    share id paired with somebody else's conversation id must not read that conversation - so
    a mismatch is the same 404 everything else here answers."""
    _seed_one_partition()
    conv = "c-611-mismatch"
    made = _seeded_link(conv, "doc-611-a")
    try:
        assert _log(conv, made["share_id"]).status_code == 200, "control"
        wrong = _log("c-611-some-other-thread", made["share_id"])
        assert wrong.status_code == 404, (
            f"the route trusted a path conv_id the share does not name: {wrong.status_code}")
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


# ---- the disclosure, on the other side of the same link -------------------------------------
#
# EVERY ASSERTION BELOW IS ON `GET /c/{token}` - THE BYTES A COOKIE-LESS VISITOR RECEIVES.
#
# The five tests here used to grep static/js/surfaces/ask.js, on disk and as a served asset.
# All five were green, and no visitor had been told anything: `/c` was not in SHELL_PATHS, so
# `/c/{token}` rendered the LANDING view and the disclosure node was built inside a container
# that was never displayed. A string in a module is not a sentence on a page, and this file
# shipped the proof of that.
#
# So they now open a real link as a client with no cookies and no session, and read the
# response. If the disclosure is ever removed from the visitor's page, these fail - and they
# fail whatever module does or does not still contain the string.


def _visitor_page(url):
    """`GET /c/{token}` as a client that has never been anywhere. A fresh TestClient carries
    no cookie jar, which is the whole point: the page a stranger receives, not the page a
    visitor receives after the rig has already warmed one up for them."""
    r = _visitor().get(url)
    assert r.status_code == 200, f"the share page did not open: {r.status_code}"
    return r.text


def test_the_share_page_tells_the_visitor_the_owner_reads_their_questions():
    """ADR 0021's accepted consequence, in the product rather than in a document.

    The copy is pinned character for character, on the RESPONSE. A later UI task that rebuilds
    the visitor page cannot quietly drop the sentence or soften it, and - unlike the version of
    this test that shipped first - it cannot pass by leaving a constant behind in a module the
    page does not load."""
    _seed_one_partition()
    conv = "c-611-disclosure"
    made = _seeded_link(conv, "doc-611-disclosure")
    try:
        page = _visitor_page(made["url"])
        assert DISCLOSURE in page, (
            "the page a link visitor receives does not tell them that the person who shared "
            f"the link can read their questions - the exact copy is: {DISCLOSURE!r}")
        assert page.count(DISCLOSURE) == 1, (
            "the disclosure sentence is written more than once - two copies drift, and the "
            "one that drifts is the one somebody stops trusting")
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_the_disclosure_sits_directly_above_the_question_input():
    '''"At the point of asking" is a placement, not a wording. Above the input, in the same
    document, before the form it warns about - not a footer, and not somewhere a visitor
    reaches only after they have already typed. Asserted on the served page, so the ORDER
    checked is the order the browser builds the DOM in.'''
    _seed_one_partition()
    conv = "c-611-disclosure-place"
    made = _seeded_link(conv, "doc-611-disclosure-place")
    try:
        page = _visitor_page(made["url"])
        at = page.index(DISCLOSURE)
        form = page.index("<form")
        thread = page.index('id="visitor-thread"')
        assert thread < at < form, (
            "the disclosure is not between the transcript and the question form - it must sit "
            f"directly above the input a visitor types into (thread {thread}, disclosure {at}, "
            f"form {form})")
        # ...and there is exactly one form on the page, so "above the form" is unambiguous.
        assert page.count("<form") == 1, "the visitor page has more than one form"
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_the_disclosure_is_permanent_and_cannot_be_dismissed():
    """Not a toast, not a dismissable banner. A visitor who clicks it away has not been told
    anything at the moment that matters, which is every subsequent question.

    STRUCTURAL, and stronger than the rule it replaces: the sentence is STATIC MARKUP in the
    document, so there is no builder to gate it, no timer to expire it and no handler to
    remove it. What is asserted is that the element carrying it has no interactive attribute
    at all, and that no script on the page names it."""
    _seed_one_partition()
    conv = "c-611-disclosure-permanent"
    made = _seeded_link(conv, "doc-611-disclosure-permanent")
    try:
        page = _visitor_page(made["url"])
        at = page.index(DISCLOSURE)
        tag_start = page.rindex("<", 0, at)
        tag = page[tag_start:page.index(">", tag_start) + 1]
        for banned in ("onclick", "hidden", "aria-hidden", "role=\"alert\"", "style="):
            assert banned not in tag, (
                f"the disclosure element carries {banned!r} - it must be plain, permanent "
                f"markup: {tag}")
        # The id it does carry must not be a handle anything scripts against.
        assert "link-disclosure" not in _served_visitor_js(), (
            "a script on the visitor page reaches for the disclosure node - the sentence must "
            "be markup nothing can take away")
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def test_the_disclosure_is_shown_on_the_share_page_and_not_on_the_owners_own_surface():
    """The owner reading her own thread is not being told anything true by this sentence -
    nobody shared a link with her - and a warning that fires where it does not apply is how a
    warning stops being read.

    This used to be checked by grepping for a `location.pathname.startsWith("/c/")` guard
    inside a builder. It is now a stronger fact and a simpler one: the sentence is not in the
    app shell at all, on ANY of its paths, because the visitor's page is a different
    document."""
    for path in ("/ask", "/app", "/chat", "/draft", "/admin", "/developer"):
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.status_code}"
        assert DISCLOSURE not in r.text, (
            f"the link disclosure is rendered on {path}, which is the owner's own surface")
    ask_js = client.get("/static/js/surfaces/ask.js")
    assert ask_js.status_code == 200, ask_js.status_code
    assert DISCLOSURE not in ask_js.text, (
        "the owner's Ask surface still carries the visitor disclosure string - it belongs to "
        "the visitor page and nowhere else, or the two copies will drift")


def test_the_disclosure_is_served_to_a_real_visitor_and_is_styled():
    """A class the stylesheet knows about, so the sentence renders as a notice rather than as
    bare text indistinguishable from the placeholder it sits next to. Both halves read off
    responses: the page that carries the class, and the stylesheet that page loads."""
    _seed_one_partition()
    conv = "c-611-disclosure-styled"
    made = _seeded_link(conv, "doc-611-disclosure-styled")
    try:
        page = _visitor_page(made["url"])
        assert 'class="link-disclosure"' in page, (
            "the disclosure has no class - it must read as a notice, not as stray text")
        assert "/static/css/app.css" in page, "the visitor page loads no stylesheet at all"
        css = client.get("/static/css/app.css")
        assert css.status_code == 200, css.status_code
        assert ".link-disclosure" in css.text, (
            "the disclosure has no style rule in the stylesheet the visitor page loads")
    finally:
        _cleanup(conv, link_principal(_share_record(made["share_id"])))


def _served_visitor_js():
    r = client.get("/static/js/visitor.js")
    assert r.status_code == 200, f"the visitor page's module is not served: {r.status_code}"
    return r.text


def test_ask_js_still_parses():
    """The selftest_557 / selftest_600 convention: a copy edit that breaks the module would
    blank every shell surface, and every string assertion above would still pass."""
    if not _domgate.gate("the ask.js parse check"):
        return          # permitted skip (DBSEARCH_ALLOW_DOM_SKIP), already counted
    r = subprocess.run(["node", "--check", "--input-type=module", "-"],
                       input=ASK_JS, capture_output=True, text=True)
    assert r.returncode == 0, f"ask.js does not parse: {r.stderr[:300]}"


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
