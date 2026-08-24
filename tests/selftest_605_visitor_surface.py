"""#605 task 12 - THE VISITOR SURFACE. What `GET /c/{token}` actually returns to a stranger.

WHY THIS FILE EXISTS, and the one rule every assertion in it obeys.

Tasks 4, 5 and 6 built the anonymous doorway: a visitor with a share URL, no account and no
session reads a shared conversation and asks their own questions against the owner's
documents. Task 6 added the disclosure ADR 0021 makes mandatory - the owner can read what you
type here - and pinned it with four tests, all green.

NOBODY HAD EVER BUILT THE PAGE. `/c/{token}` served index.html, `/c` was absent from
SHELL_PATHS, so login.js rendered the LANDING (marketing) view and the app node was built
inside a container that was never displayed. A real visitor got a marketing page. The owner
could already read what strangers asked while no stranger had been told. The four tests stayed
green throughout, because every one of them grepped `static/js/surfaces/ask.js` - on disk or
as a served asset - and a string in a module is not a sentence on a page.

SO: EVERY CONTENT ASSERTION HERE IS ON THE RESPONSE `GET /c/{token}` HANDS A COOKIE-LESS
CLIENT, or on a DOM built from exactly those bytes. There is not one disk grep in this file.
The mutation that proves it: delete the disclosure paragraph from static/visitor.html and
these fail; delete a string from any module and they do not care, because no module carries
the sentence any more.

TWO CLASSES OF ASSERTION, labelled in the test names:

  test_*        RESPONSE assertions. A fresh TestClient (no cookie jar, no session) opens a
                real link minted through the real share API, and the bytes are read.
  test_dom_*    DOM assertions. The SERVED bytes are written to a file, mounted in jsdom, the
                page's real module is imported against them, `/c/{token}/transcript` and
                `/c/{token}/chat` are answered with the shapes link_access.py really returns,
                and the resulting DOM is inspected.

WHAT IS STILL OWED. jsdom is not a browser: no layout, no paint, no CSS. These prove the
disclosure is in the document and in the DOM directly above the input; they cannot prove it is
painted where a visitor's eye lands, that the page does not scroll it out of reach on a phone,
or that the read-only half reads as read-only rather than merely lacking buttons.

    PYTHONPATH=src python3 tests/selftest_605_visitor_surface.py
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import _domgate  # noqa: E402  the shared jsdom gate (#792)
os.environ.setdefault("SELFHOST_BACKEND", "memory")
# Same rig rule as selftest_605_anonymous_link_access: the public demo's per-IP cap is
# installed at import time and every test here is several real HTTP calls.
os.environ["DBSEARCH_RATE_LIMIT"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.query.conversation import Turn  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import ACCOUNTS, app, _edition  # noqa: E402
from dbsearch.server.conversation_shares import AUDIENCE_LINK  # noqa: E402
from dbsearch.server.link_access import VISITOR_COOKIE, link_principal  # noqa: E402

client = TestClient(app)

ALICE = "acct_alice"
BOB = "acct_bob"
HOME_TID = "tid-605-visitor-home"
CK = {}

A_TEXT = "The Lisbon carryover allowance in doc-605v carries over 26 days of unused leave."

#: The exact sentence, character for character. ADR 0021's accepted consequences, and the
#: whole reason this task was pulled forward out of order.
DISCLOSURE = "The person who shared this link can see the questions you ask here."

#: What the LANDING (marketing) view says. If any of these reaches a visitor, `/c/{token}` is
#: serving the app shell again and the visitor is looking at a sales page. These are copied
#: from static/index.html; they are checked to still be present at `/ask` by
#: `test_the_landing_markers_are_real`, so this list can never rot into a set of strings that
#: match nothing and therefore prove nothing.
LANDING_MARKERS = [
    "Try it free",
    "Enterprise RAG",
    "Answers from your company knowledge",
    'id="view-landing"',
    'id="lp-signin"',
    "Permission-trimmed retrieval",
]

#: Controls that would be a lie on this page. A visitor has no account and no workspace: there
#: is nothing to administer, no source to connect, no developer view to open, no model to pick
#: and pay for on somebody else's bill, no conversation of their own to start, and no data of
#: their own to list.
WORKSPACE_MARKERS = [
    "model-pick", "model-select",           # the model selector (a knob on the owner's bill)
    "navrail", "rail.css",                  # the workspace rail
    "Connectors", "Admin", "Developer",     # the rail's destinations
    "New conversation", "Your data",
    "Sign out", "sign-out",
    'id="view-app"', "main.js",             # the app shell itself, and its entry point
]


def _seed_one_partition():
    """selftest_605_anonymous_link_access's rig verbatim: two identities in the deployment's
    own partition, the single-org shape where a partition filter protects nobody."""
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


def _link(conv: str, doc_id: str) -> dict:
    """A real link, minted through the real share route by a real signed-in owner."""
    _seed_one_partition()
    _ingest(doc_id, A_TEXT)
    _turn(conv, "how much leave carries over?", "It carries over 26 days of unused leave.",
          [doc_id])
    r = client.post(f"/conversations/{conv}/shares", cookies=CK[BOB],
                    json={"audience": AUDIENCE_LINK})
    assert r.status_code == 200, f"share failed: {r.status_code} {r.text[:300]}"
    out = r.json()
    assert out["url"].startswith("/c/"), out
    return out


def _cleanup(conv: str, share_id: str) -> None:
    try:
        share = _edition.conversation_shares.find(share_id, BOB)
        if share is not None:
            _edition.grant_registry.drop_for_conversation(conv, link_principal(share))
    except Exception:
        pass


def _stranger() -> TestClient:
    """A client that has never been anywhere: no session cookie, no fork-key cookie, nothing.
    A NEW TestClient per call - reusing one would accumulate a cookie jar and quietly stop
    testing the thing this file is about."""
    return TestClient(app)


def _page(url: str):
    r = _stranger().get(url)
    assert r.status_code == 200, f"the visitor page did not open: {r.status_code} {r.text[:200]}"
    return r


# ---- the disclosure, in the bytes a stranger receives ------------------------------------

def test_the_visitor_receives_the_disclosure_verbatim():
    """THE assertion this task exists for. Not ask.js, not a served module: the response."""
    made = _link("c-605v-disclosure", "doc-605v-disclosure")
    try:
        body = _page(made["url"]).text
        assert DISCLOSURE in body, (
            "a link visitor is NOT told that the owner can read their questions. The exact "
            f"copy ADR 0021 requires is: {DISCLOSURE!r}")
        assert body.count(DISCLOSURE) == 1, (
            "the sentence appears more than once in the page - two copies drift")
    finally:
        _cleanup("c-605v-disclosure", made["share_id"])


def test_the_disclosure_sits_between_the_transcript_and_the_question_form():
    """"At the point of asking" is a placement. In the served document the transcript comes
    first, then the disclosure, then the one form on the page."""
    made = _link("c-605v-place", "doc-605v-place")
    try:
        body = _page(made["url"]).text
        thread = body.index('id="visitor-thread"')
        at = body.index(DISCLOSURE)
        form = body.index("<form")
        assert thread < at < form, (
            f"disclosure is out of place (thread {thread}, disclosure {at}, form {form})")
        assert body.count("<form") == 1, "the visitor page has more than one form"
        assert body.count("<input") == 1, (
            "the visitor page offers more than one input - there is exactly one thing to type "
            "into here")
    finally:
        _cleanup("c-605v-place", made["share_id"])


def test_the_disclosure_needs_no_javascript_to_appear():
    """It is STATIC MARKUP, and that is the point rather than an implementation detail. A
    disclosure that arrives after a fetch is a disclosure that can be missing while every
    test is green - which is exactly how this feature reached task 12. Asserted by finding it
    in the response body outside every <script> element."""
    made = _link("c-605v-static", "doc-605v-static")
    try:
        import re
        body = _page(made["url"]).text
        stripped = re.sub(r"<script.*?</script>", "", body, flags=re.S)
        assert DISCLOSURE in stripped, (
            "the disclosure only exists inside a script - it must be markup the browser has "
            "before it runs a single line of JavaScript")
    finally:
        _cleanup("c-605v-static", made["share_id"])


# ---- what the visitor is NOT shown --------------------------------------------------------

def test_the_landing_markers_are_real():
    """The guard on the guard. `test_the_visitor_page_is_not_the_marketing_landing` proves a
    NEGATIVE, and a negative over strings nothing contains proves nothing at all. Every marker
    must actually appear where the landing is served, or the next test is vacuous."""
    shell = client.get("/ask")
    assert shell.status_code == 200, shell.status_code
    missing = [m for m in LANDING_MARKERS if m not in shell.text]
    assert not missing, (
        f"these landing markers no longer appear in the app shell, so asserting their absence "
        f"from the visitor page proves nothing: {missing}")


def test_the_visitor_page_is_not_the_marketing_landing():
    """The defect this task was raised for, stated directly: a stranger who follows a share
    link must not be handed a sales page."""
    made = _link("c-605v-landing", "doc-605v-landing")
    try:
        body = _page(made["url"]).text
        found = [m for m in LANDING_MARKERS if m in body]
        assert not found, (
            f"a link visitor is being served the marketing landing page: {found}")
    finally:
        _cleanup("c-605v-landing", made["share_id"])


def test_the_visitor_is_offered_no_workspace_they_do_not_have():
    """No Admin, no Connectors, no Developer, no model selector, no rail, no "New
    conversation", no "Your data", no Sign out. A visitor has no account; anything here that
    implies otherwise is a lie in the UI, and a control that 401s reads as a broken page
    rather than as a refusal."""
    made = _link("c-605v-chrome", "doc-605v-chrome")
    try:
        body = _page(made["url"]).text
        found = [m for m in WORKSPACE_MARKERS if m in body]
        assert not found, (
            f"the visitor page offers controls belonging to an account the visitor does not "
            f"have: {found}")
    finally:
        _cleanup("c-605v-chrome", made["share_id"])


def test_the_page_loads_nothing_that_would_401_on_a_visitor():
    """"Check what the shell fetches on boot." The app shell's entry point loads /config, the
    identity control, the rail and the shell router; every data route behind them depends on
    `current_user` and 401s for a visitor, which does not look like a refusal, it looks like a
    broken page. So the visitor page must load its OWN module, and that module must talk to
    the four `/c/{token}` routes and nothing else.

    Both halves are read off responses: the page names the module, and the served module is
    searched for any route literal outside the doorway."""
    made = _link("c-605v-boot", "doc-605v-boot")
    try:
        body = _page(made["url"]).text
        assert "/static/js/visitor.js" in body, (
            "the visitor page does not load a module of its own")
        js = client.get("/static/js/visitor.js")
        assert js.status_code == 200, f"the visitor module is not served: {js.status_code}"
        forbidden = ["/config", "/auth/me", "/auth/login", "/ask/suggestions", "/conversations",
                     "/admin", "/search", "/router", "/documents", "/upload", "/ingest"]
        hit = [r for r in forbidden if f'"{r}' in js.text or f"`{r}" in js.text
               or f"'{r}" in js.text]
        assert not hit, (
            f"the visitor's module calls routes that require a session, which 401 for a "
            f"visitor and render as a broken page: {hit}")
        # And it must not pull in a module that would do it on its behalf.
        for banned in ("./api.js", "../api.js", "./identity.js", "./router.js", "./login.js",
                       "./ui/rail.js"):
            assert banned not in js.text, (
                f"the visitor module imports {banned}, which reaches session-only routes")
    finally:
        _cleanup("c-605v-boot", made["share_id"])


def test_the_served_visitor_module_parses():
    node = subprocess.run(["which", "node"], capture_output=True, text=True)
    if node.returncode != 0:
        print("      (node not installed - skipping the parse check)")
        return
    src = client.get("/static/js/visitor.js").text
    r = subprocess.run(["node", "--check", "--input-type=module", "-"],
                       input=src, capture_output=True, text=True)
    assert r.returncode == 0, f"the served visitor.js does not parse: {r.stderr[:300]}"


def test_the_rendered_page_carries_a_build_id_and_versioned_assets():
    """#415 / #557, on the new document. A page whose assets are not cache-busted keeps
    serving a warm browser the old module after a deploy - the failure that once deleted the
    whole navigation - and a build id substituted into an IDENTIFIER position is a SyntaxError
    62% of deploys. Both are properties of the RENDERED page, not of the template."""
    import re
    made = _link("c-605v-build", "doc-605v-build")
    try:
        body = _page(made["url"]).text
        assert "__DBS_BUILD__" not in body, "the build placeholder was never substituted"
        assert re.search(r'src="/static/js/visitor\.js\?v=[0-9a-f]{6,}"', body), \
            "the visitor module is loaded without a versioned URL"
        assert re.search(r'href="/static/css/app\.css\?v=[0-9a-f]{6,}"', body), \
            "the stylesheet is loaded without a versioned URL"
        for js in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", body, re.S):
            assert not re.search(r"window\.\s*\d", js), \
                f"the rendered page assigns to a numeric identifier: {js.strip()[:80]}"
    finally:
        _cleanup("c-605v-build", made["share_id"])


# ---- a dead link ---------------------------------------------------------------------------

def test_a_revoked_link_and_a_token_that_never_existed_are_byte_identical():
    """Existence is the secret (#549, ADR 0021 invariants 3 and 4). The caller is anonymous by
    construction, so a page that distinguished "revoked" from "no such link" would confirm to
    anyone sweeping the 128-bit token space that a value was once real.

    Compared on the RAW BYTES and on the headers a client can see - including Set-Cookie,
    because a fork-key cookie minted for one dead token and not another would itself be the
    distinguishable refusal."""
    made = _link("c-605v-dead", "doc-605v-dead")
    token = made["url"].rsplit("/", 1)[-1]
    try:
        live = _stranger().get(made["url"])
        assert live.status_code == 200, "control: the live link must open"

        killed = client.delete(f"/conversations/shares/{made['share_id']}", cookies=CK[BOB])
        assert killed.status_code == 200, killed.text[:300]

        revoked = _stranger().get(made["url"])
        never = _stranger().get("/c/" + "0" * len(token))
        assert revoked.status_code == never.status_code == 404, (
            f"{revoked.status_code} vs {never.status_code}")
        assert revoked.content == never.content, (
            f"revoked and never-existed serve different bytes:\n{revoked.text[:300]}\n---\n"
            f"{never.text[:300]}")
        for h in ("content-type", "cache-control", "set-cookie"):
            assert revoked.headers.get(h) == never.headers.get(h), (
                f"revoked and never-existed differ on {h}: {revoked.headers.get(h)!r} vs "
                f"{never.headers.get(h)!r}")
    finally:
        _cleanup("c-605v-dead", made["share_id"])


def test_the_dead_link_page_is_a_page_and_tells_the_visitor_nothing_else():
    """A human landed here by clicking, so `{"detail":"not found"}` is not an answer. It must
    be a page - and a page that says only that the link is gone: no token echoed back, no
    owner, no date, no reason, and no script that could fetch one."""
    r = _stranger().get("/c/" + "f" * 32)
    assert r.status_code == 404, r.status_code
    assert "text/html" in r.headers.get("content-type", ""), r.headers.get("content-type")
    assert "This link is no longer available." in r.text, r.text[:300]
    assert "<script" not in r.text, (
        "the dead-link page runs JavaScript - there must be no code path that could fetch a "
        "fact and render a difference between one dead token and another")
    for leak in ("expired", "revoked", "deleted", "f" * 32):
        assert leak not in r.text.lower(), (
            f"the dead-link page discloses why, or echoes the token: {leak!r}")
    assert VISITOR_COOKIE not in r.headers.get("set-cookie", ""), (
        "a dead link minted a fork-key cookie - there is no thread to key, and a cookie on "
        "one dead token's response and not another's is a distinguishable refusal")


def test_a_people_share_token_gets_the_same_dead_page():
    """A people share is not openable anonymously, and must not be told apart from a token
    that was never minted."""
    made = _link("c-605v-people", "doc-605v-people")
    try:
        never = _stranger().get("/c/" + "0" * 32)
        r = client.post("/conversations/c-605v-people/shares", cookies=CK[BOB],
                        json={"audience": "people", "grantee_email": "alice@x.com"})
        assert r.status_code == 200, r.text[:200]
        # A people share mints no token, so the closest a caller gets is a value that resolves
        # to no live LINK row - which must be the same page, byte for byte.
        assert _stranger().get("/c/" + "1" * 32).content == never.content
    finally:
        _cleanup("c-605v-people", made["share_id"])


# ---- the DOM a visitor actually gets -------------------------------------------------------
#
# Everything above reads bytes. Everything below builds a DOM out of exactly those bytes,
# imports the page's real module against it, and answers the questions bytes cannot: is the
# grantor's half read only, does the visitor's own answer land below it, and what does a
# visitor see when the link's question cap fires.

JSDOM = _domgate.JSDOM
PROBE = ROOT / "tests/visitor_page_dom_probe.mjs"
VISITOR_JS = ROOT / "src/dbsearch/server/static/js/visitor.js"
SCRATCH = Path(os.environ.get("TMPDIR", "/tmp")) / "dbs-605-visitor-page.html"
_dom = {}


def _probe(scenario):
    """Mount the SERVED page in a real DOM and drive it.

    Returns None when node or jsdom is unavailable, and the DOM assertions then skip - the
    stance selftest_557 and selftest_606 already take. A skipped check is reported, never
    silently counted as a pass."""
    if scenario in _dom:
        return _domgate.resolve(_dom[scenario])
    if not _domgate.gate(f"the visitor-page DOM check ({scenario})"):
        _dom[scenario] = None                          # permitted skip, already counted
        return None
    made = _link(f"c-605v-dom-{scenario}", f"doc-605v-dom-{scenario}")
    try:
        # THE BYTES THE SERVER ACTUALLY SENT, not the template on disk. A probe driven from
        # the file would be one more test that cannot see the defect this task was raised for.
        SCRATCH.write_text(_page(made["url"]).text, encoding="utf-8")
        _dom[scenario] = _domgate.run_node(
            ["node", str(PROBE), str(JSDOM), str(SCRATCH), str(VISITOR_JS), scenario],
            f"the visitor page ({scenario})")
    finally:
        _cleanup(f"c-605v-dom-{scenario}", made["share_id"])
    return _domgate.resolve(_dom[scenario])


def _skip_dom():
    """The DOM half did not run. `_probe` has already printed and counted why."""
    return True


def test_dom_the_disclosure_is_in_the_live_dom_directly_above_the_input():
    r = _probe("read")
    if r is None: return _skip_dom()
    assert r["disclosure_text"] == DISCLOSURE, r["disclosure_text"]
    assert r["disclosure_precedes_input"], \
        "the disclosure does not precede the question input in the rendered DOM"
    assert r["disclosure_after_thread"], \
        "the disclosure sits above the transcript rather than directly above the input"
    assert r["disclosure_is_visible"], \
        "the disclosure node is hidden from the visitor, or from a screen reader"


def test_dom_the_only_controls_on_the_page_are_the_question_box_and_ask():
    """Counted, not grepped. "A visitor is offered no workspace" is a fact about the controls
    that exist on the page, and this is the form of it that survives somebody renaming a
    class."""
    r = _probe("read")
    if r is None: return _skip_dom()
    kinds = sorted((c["tag"], c["type"]) for c in r["controls"])
    assert kinds == [("button", "submit"), ("input", "text")], \
        f"unexpected controls on the visitor page: {r['controls']}"
    assert not [c for c in r["controls"] if c["href"]], \
        f"the visitor page offers links to navigate away into an app they cannot use: " \
        f"{r['controls']}"


def test_dom_the_grantors_half_renders_read_only_and_the_visitors_fork_below_it():
    """The `own` flag, used for what it is for. The grantor's turns are labelled and carry NO
    control at all - not a disabled one, not a hidden one: a visitor cannot edit, retry or
    delete somebody else's turn because there is nothing there to press."""
    r = _probe("read")
    if r is None: return _skip_dom()
    assert [t["own"] for t in r["turns"]] == ["false", "true"], \
        f"the transcript did not render the shared prefix then the visitor's own fork: " \
        f"{r['turns']}"
    shared, own = r["turns"]
    assert shared["shared_marker"], \
        "the grantor's turn is not marked, so a visitor could mistake it for their own"
    assert shared["controls"] == 0, \
        f"the grantor's read-only turn carries {shared['controls']} control(s)"
    assert own["controls"] == 0, \
        "the visitor's own turn carries a control this page has no endpoint for"
    assert "26 days" in r["thread_text"], \
        "the shared answer TEXT never reached the visitor's page"


def test_dom_a_visitors_question_is_answered_and_cited_below_the_shared_half():
    r = _probe("ask")
    if r is None: return _skip_dom()
    assert r["asked"] and r["asked"][0]["url"].endswith("/chat"), r["asked"]
    assert r["asked"][0]["body"] == {"question": "does that apply to contractors?"}, \
        f"the question that was sent is not the question that was typed: {r['asked']}"
    assert "Contractors are out of scope." in r["last_block_text"], r["last_block_text"]
    # #629: the sources collapsed into a pill that opens a panel. A visitor must still get
    # a CITED answer - the whole premise of a shared link is that it can be checked - so the
    # affordance has to be offered AND has to lead to the source. Asserting only that the
    # pill exists would pass on a pill that opens nothing.
    assert r["sources_pill_text"], (
        "the answer arrived with no sources affordance - a visitor gets cited answers or none")
    assert r["sources_panel_open"], "clicking the visitor's sources pill opened nothing"
    assert "Leave policy" in r["sources_panel_text"], (
        f"the panel does not name the source the answer drew on: {r['sources_panel_text']!r}")
    assert r["own_flags_in_order"] == ["false", "true", "true"], \
        f"the visitor's new turn did not land below the shared half: {r['own_flags_in_order']}"


def test_dom_hitting_the_links_question_cap_is_explained_not_spun():
    """A 429 with Retry-After is the rate cap doing its job. A visitor who has hit it must be
    TOLD - a spinner or "something went wrong" turns a working product into what looks like a
    fault, and the copy must not accuse this visitor, because the cap is per LINK and somebody
    else may have spent it."""
    r = _probe("capped")
    if r is None: return _skip_dom()
    text = r["last_block_text"]
    assert "as many questions as it can for now" in text, text
    # Retry-After was 1800 seconds, so the page must say 30 minutes rather than a bare "later".
    assert "30 minutes" in text, \
        f"the Retry-After the server sent was not turned into something actionable: {text}"
    assert "error" not in text.lower() and "went wrong" not in text.lower(), \
        f"a rate-cap refusal is rendered as a fault: {text}"
    assert not r["form_hidden_after"], \
        "the composer was taken away over a temporary cap - the visitor may ask again later"


def test_dom_a_link_revoked_under_an_open_page_says_so_and_takes_the_composer_away():
    """Revoke kills every fork immediately, including one somebody has open. The next question
    404s, and the page must say the link is gone rather than leave a live composer under a
    dead link - which invites a visitor to type into something that can only fail."""
    r = _probe("revoked")
    if r is None: return _skip_dom()
    assert "This link is no longer available." in r["after_text"], r["after_text"]
    assert r["form_hidden_after"], \
        "the question box is still offered under a link that no longer works"


def test_dom_a_link_that_died_before_the_page_loaded_never_shows_a_composer():
    r = _probe("dead")
    if r is None: return _skip_dom()
    assert "This link is no longer available." in r["thread_text"], r["thread_text"]
    assert r["form_hidden_on_load"], \
        "a dead link still renders a question box on load"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
