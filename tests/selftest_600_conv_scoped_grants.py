"""#600: a conv-scoped grant opens its doorway pair ONLY inside its own conversation.

The property under test is GOAL_ACCEPTANCE step 7 by construction: /documents endpoints
never pass active_conv_id, so a conversation share is structurally incapable of widening
into general document access - the same shape as ADR 0019 D3's "a pair opens ONE document,
never the partition".

    PYTHONPATH=src python3 tests/selftest_600_conv_scoped_grants.py
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from dbsearch.api.grants import Grant, GrantRegistry  # noqa: E402


class _DownStore:
    """save is swallowed at create (best-effort, same as `create`'s own stance) so a
    grant can exist in-process with no live store to protect it - and delete always
    raises, standing in for an unreachable Postgres at revoke time."""

    def load_all(self):
        return []

    def save(self, g):
        raise RuntimeError("store down")

    def delete(self, grant_id):
        raise RuntimeError("store down")


def _mk(reg, conv_id=None):
    return reg.create(doc_external_id="hr-1", tenant_id="acct_bob", grantee_oid="alice",
                      granted_by="bob", conv_id=conv_id)


def test_a_plain_grant_still_defaults_conv_id_none_and_round_trips():
    g = _mk(GrantRegistry())
    assert g.conv_id is None and g.to_dict()["conv_id"] is None


def test_an_empty_or_blank_conv_id_normalizes_to_a_plain_document_grant():
    """#600 review Finding C. `conv_id=""` (a route forwarding a blank body field, not a
    real conversation) must not mint a grant that LOOKS conv-scoped but can never open -
    nothing a request carries can ever equal "" as `active_conv_id` on purpose, since
    ChatRequest.conv_id is a required field. Normalizing to None at create() makes it an
    ordinary, always-contributing document grant instead of a silently-dead share."""
    reg = GrantRegistry()
    g_empty = _mk(reg, conv_id="")
    g_blank = _mk(reg, conv_id="   ")
    assert g_empty.conv_id is None, g_empty.conv_id
    assert g_blank.conv_id is None, g_blank.conv_id


def test_conv_scoped_grant_expands_ONLY_inside_its_own_conversation():
    """#601 / ADR 0020. THIS TEST USED TO ASSERT THE OPPOSITE, and it was protecting the bug.

    It read `assert g.principal in reg.live_principals_for("alice")` and explained that the
    ACL side stays conversation-blind on purpose, because the doorway is what scopes and
    scoping in two places would let the two disagree. The premise was wrong: the doorway is
    partition ROUTING, and `ReadScope.allows` short-circuits on partition equality before it
    ever reads the doorway - so whenever grantor and grantee share a partition (every
    self-host box, every single-org Entra tenant) NOTHING scoped the grant, and a share of
    one conversation handed the grantee /search, the document listing, segment previews, the
    original file bytes and /chat in any other thread.

    Expansion is authorization, so the rule belongs here. The default is fail-closed."""
    reg = GrantRegistry()
    g = _mk(reg, conv_id="c1")
    assert g.principal not in reg.live_principals_for("alice"), (
        "a conversation grant expanded with no conversation active - every read path that "
        "has no conversation concept would see it")
    assert g.principal not in reg.live_principals_for("alice", "c2"), (
        "a conversation grant expanded inside somebody else's conversation")
    assert g.principal in reg.live_principals_for("alice", "c1"), (
        "the grant does not work inside its OWN conversation - the share grants nothing")


def test_a_plain_document_grant_expands_everywhere_exactly_as_before():
    """The other half of ADR 0020's default: a #582 document share has no conv_id and must
    keep expanding unconditionally. If this ever failed, the fail-closed default would have
    quietly broken ordinary document sharing while looking like a security improvement."""
    reg = GrantRegistry()
    g = _mk(reg)
    assert g.principal in reg.live_principals_for("alice")
    assert g.principal in reg.live_principals_for("alice", "any-conversation-at-all")


def test_drop_for_conversation_kills_only_that_conversations_grants():
    reg = GrantRegistry()
    keep = _mk(reg)                     # a #582 document share, conv_id None
    kill = _mk(reg, conv_id="c1")
    assert reg.drop_for_conversation("c1", "alice") == 1
    live = reg.live_principals_for("alice")
    assert keep.principal in live and kill.principal not in live


def test_drop_for_conversation_fails_closed_on_a_down_store():
    reg = GrantRegistry(store=_DownStore())     # save swallowed at create (best-effort)
    g = _mk(reg, conv_id="c1")
    try:
        reg.drop_for_conversation("c1", "alice")
        assert False, "expected the store failure to raise"
    except Exception:
        pass
    # Still live INSIDE ITS OWN CONVERSATION = honest. (ADR 0020 moved the conversation test
    # into expansion, so asking conversation-blind here would now say "gone" for a grant that
    # is very much still there, and the assertion would pass for the wrong reason.)
    assert g.principal in reg.live_principals_for("alice", "c1"), (
        "the store delete failed and the grant stopped expanding anyway - the route raises "
        "'cannot revoke right now, the share is still active' while the access is really "
        "gone, so the honest failure becomes a lie in the other direction")


def test_only_the_two_chat_routes_may_declare_an_active_conversation():
    """#601 round 4: the TRIPWIRE, because fail-closed is invisible.

    ADR 0020 makes a conv-scoped grant expand only inside its own conversation, and the
    default - say nothing, get nothing - is what makes every read path correct without being
    edited. The cost of a default that good is that widening is SILENT: no route has to opt
    OUT, so nothing goes red if one wrongly opts IN.

    And the realistic widening move is not somebody calling `expand_groups_scoped` by hand.
    It is somebody building a `ReadScope` with `active_conv_id=` and handing it to a path
    that already runs `expand_principals` - which is one greppable seam,
    `_request_scope(request, user, <third argument>)`. Exactly two routes may cross it: the
    conversational rail. Every other route must call `_request_scope` with two arguments, so
    a conversation grant can never reach it.

    Parsed with `ast` rather than grepped, so a call spread over two lines or written with
    the keyword still counts. If this fails, do not relax it: either the new caller is a
    third conversational surface and belongs in the set with a reason recorded here, or it
    is CRITICAL-A being reintroduced through the one door ADR 0020 leaves open."""
    import ast

    ALLOWED = {"chat", "chat_stream"}
    src = (ROOT / "src" / "dbsearch" / "server" / "app.py").read_text()
    tree = ast.parse(src)
    # Innermost enclosing function for every line, so a nested helper is attributed to
    # itself rather than to whatever module-level def happens to precede it.
    owner: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                owner[line] = node.name

    declaring = set()
    calls = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_request_scope"):
            continue
        calls += 1
        if len(node.args) > 2 or any(k.arg == "active_conv_id" for k in node.keywords):
            declaring.add(owner.get(node.lineno, f"<line {node.lineno}>"))

    assert calls >= 3, f"only found {calls} _request_scope calls - did it get renamed?"
    assert declaring == ALLOWED, (
        f"the set of routes declaring an active conversation to _request_scope is "
        f"{sorted(declaring)}, not {sorted(ALLOWED)}. A route that passes active_conv_id "
        f"hands conv-scoped grant principals to principal expansion (ADR 0020), which is "
        f"how a conversation share stops being scoped to its conversation.")


# ---- app-level: the doorway is real only inside its own conversation --------------------
# Same client/bootstrap fixture as tests/selftest_582_share_across_partitions.py: a real
# FastAPI TestClient, two real account partitions, real cookies signed the way a real
# sign-in would leave them.

os.environ.setdefault("SELFHOST_BACKEND", "memory")
from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.api.auth import ACCT_TENANT_PREFIX  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import ACCOUNTS, app, _edition  # noqa: E402

client = TestClient(app)

ALICE = "acct_alice"
BOB = "acct_bob"
BOB_PARTITION = ACCT_TENANT_PREFIX + BOB      # ADR 0018: the local account's own partition
HOME_TID = "tid-600-home"
_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET")


def _real_login():
    """Turns on real-login mode (`real_login_enabled`), which is what makes a local
    account resolve to its own private `acct:<oid>` partition (ADR 0018) instead of the
    dev-rig fallback of the deployment constant - same as selftest_582_share_across_
    partitions.py's fixture of the same name."""
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


def _sse_events(text: str) -> list:
    """Parse an SSE body ("data: {...}\\n\\n" blocks) into the JSON events it carries."""
    events = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def test_chat_with_the_conversation_opens_the_doorway_but_documents_do_not():
    """bob ingests hr-1 in acct_bob; a conv-scoped grant (c1) names alice.

    POST /chat as alice with conv_id=c1  -> answer cites hr-1 (doorway open)
    POST /chat as alice with conv_id=c2  -> no retrieval from hr-1 (doorway shut)
    GET  /admin/documents/hr-1/download as alice -> 404 (document endpoints never pass a conv)
    GET  "Your data" listing as alice            -> hr-1 absent
    """
    _real_login()
    _seed_accounts()
    doc_id = "doc-600-hr-1"
    secret = "The Hamburg severance policy in doc-600-hr-1 pays out over 62 weeks."
    r = client.post("/ingest", cookies=_cookie(BOB), json={
        "external_id": doc_id, "title": "hr policy", "text": secret,
        "acl": [BOB], "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, r.text[:200]

    # A conversation share, built directly on the registry - #600 lands the MECHANISM, and
    # nothing in this codebase yet mints a conv-scoped grant through an API route (that is
    # a later task). This is the same "build the row the way it would really arrive"
    # pattern selftest_582_share_across_partitions.py uses for a legacy foreign grant.
    grant = _edition.grant_registry.create(
        doc_external_id=doc_id, tenant_id=BOB_PARTITION, grantee_oid=ALICE,
        granted_by=BOB, conv_id="c1")
    touched = _edition.index.add_doc_principals(BOB_PARTITION, doc_id, [grant.principal])
    assert touched, "seed grant never reached the document's ACL"

    try:
        # -- doorway OPEN: alice asks inside c1, the conversation the grant names --
        r = client.post("/chat", cookies=_cookie(ALICE),
                        json={"conv_id": "c1", "question": "how many weeks of severance?"})
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert doc_id in body["retrieved_docs"], (
            f"the doorway did not open inside its own conversation: {body}")

        # -- document endpoints never declare a conversation, so they never see the pair --
        # Checked FIRST, right after the doorway is proven open: this is the assertion the
        # mutation check (task-3 brief step 6) must kill - deleting the conv-scoping filter
        # makes every live grant contribute unconditionally, so a document endpoint (which
        # always calls _request_scope with active_conv_id=None) would see the c1 grant's
        # pair too, and this download would leak from 404 to 200.
        r = client.get(f"/admin/documents/{doc_id}/download?form=text", cookies=_cookie(ALICE))
        assert r.status_code == 404, (
            f"a conversation share widened into a document download: {r.status_code}")

        listed = {d["doc_external_id"] for d in
                  client.get("/admin/documents", cookies=_cookie(ALICE)).json()}
        assert doc_id not in listed, (
            "a conversation share widened into the 'Your data' listing")

        # -- doorway SHUT: alice asks the exact same thing inside a DIFFERENT conversation --
        r = client.post("/chat", cookies=_cookie(ALICE),
                        json={"conv_id": "c2", "question": "how many weeks of severance?"})
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert doc_id not in body["retrieved_docs"], (
            f"a conv-scoped grant leaked into a DIFFERENT conversation: {body}")
    finally:
        _edition.grant_registry.drop_for_conversation("c1", ALICE)


def test_the_corpus_denominator_agrees_with_citations_inside_a_shared_conversation():
    """#600 review Finding B. Before this fix, `/chat` inside a shared conversation could
    cite a document (retrieval used `_request_scope(..., active_conv_id=req.conv_id)`) while
    `corpus.authorized_docs` said 0 (`_corpus_block` built its own, conv-blind scope and saw
    no doorway at all) - reproduced end to end as `retrieved: ['doc-600-hr-2']` next to
    `corpus: {'indexed': False, 'authorized_docs': 0}`. The recipient's UI would cite a
    document while its own footer claimed she may see none of them - the exact class of bug
    #392 exists to prevent, reintroduced here one call site at a time."""
    _real_login()
    _seed_accounts()
    doc_id = "doc-600-hr-2"
    secret = "The Rotterdam relocation policy in doc-600-hr-2 covers 8 weeks of housing."
    r = client.post("/ingest", cookies=_cookie(BOB), json={
        "external_id": doc_id, "title": "hr policy 2", "text": secret,
        "acl": [BOB], "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, r.text[:200]

    grant = _edition.grant_registry.create(
        doc_external_id=doc_id, tenant_id=BOB_PARTITION, grantee_oid=ALICE,
        granted_by=BOB, conv_id="c-corpus")
    touched = _edition.index.add_doc_principals(BOB_PARTITION, doc_id, [grant.principal])
    assert touched, "seed grant never reached the document's ACL"

    try:
        r = client.post("/chat", cookies=_cookie(ALICE),
                        json={"conv_id": "c-corpus", "question": "how many weeks of housing?"})
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert doc_id in body["retrieved_docs"], (
            f"the doorway did not open inside its own conversation: {body}")
        corpus = body["corpus"]
        assert corpus is not None, "the corpus denominator was silently dropped"
        assert corpus["authorized_docs"] >= 1, (
            f"cited a document but the footer said authorized_docs={corpus['authorized_docs']} "
            f"- citations and the denominator disagree: {body}")
    finally:
        _edition.grant_registry.drop_for_conversation("c-corpus", ALICE)


def test_the_streaming_corpus_denominator_agrees_with_citations_too():
    """#600 review Finding E. The previous test only drove `/chat` (the JSON surface); the
    reviewer showed reverting app.py's `/chat/stream` corpus call left the whole suite
    green, meaning the SAME #392 mismatch was live on the surface the product actually
    serves chat on, with nothing testing it. Drives `/chat/stream`, parses the SSE body,
    and checks the `done` event's corpus denominator against its own citations."""
    _real_login()
    _seed_accounts()
    doc_id = "doc-600-hr-3"
    secret = "The Singapore notice period in doc-600-hr-3 runs for 6 weeks."
    r = client.post("/ingest", cookies=_cookie(BOB), json={
        "external_id": doc_id, "title": "hr policy 3", "text": secret,
        "acl": [BOB], "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, r.text[:200]

    grant = _edition.grant_registry.create(
        doc_external_id=doc_id, tenant_id=BOB_PARTITION, grantee_oid=ALICE,
        granted_by=BOB, conv_id="c-corpus-stream")
    touched = _edition.index.add_doc_principals(BOB_PARTITION, doc_id, [grant.principal])
    assert touched, "seed grant never reached the document's ACL"

    try:
        r = client.post("/chat/stream", cookies=_cookie(ALICE),
                        json={"conv_id": "c-corpus-stream",
                              "question": "how many weeks is the notice period?"})
        assert r.status_code == 200, r.text[:200]
        events = _sse_events(r.text)
        done = [e for e in events if e.get("type") == "done"]
        assert done, f"no done event in the stream: {r.text[:400]}"
        body = done[-1]
        assert doc_id in body["retrieved_docs"], (
            f"the doorway did not open inside its own conversation: {body}")
        corpus = body.get("corpus")
        assert corpus is not None, "the streaming corpus denominator was silently dropped"
        assert corpus["authorized_docs"] >= 1, (
            f"cited a document but the streamed footer said "
            f"authorized_docs={corpus['authorized_docs']} - citations and the denominator "
            f"disagree: {body}")
    finally:
        _edition.grant_registry.drop_for_conversation("c-corpus-stream", ALICE)


def test_the_streaming_corpus_scope_is_reused_not_rederived():
    """#600 review Finding F. `_corpus_block` must not independently rebuild the ReadScope
    `/chat/stream` already built for retrieval - a second derivation is a seam: a revoke
    landing between the two calls would desync the denominator from the citations it
    describes, even though nothing today makes that happen on a normal request.

    A live race is not something this test can honestly reproduce end-to-end here: revoking
    a conv-scoped grant removes the SAME record that backs both the doorway (routing) and
    the ACL principal (authorization), and `/chat/stream` computes the corpus block BEFORE
    the generator that actually runs retrieval - so any side effect timed to a SECOND
    `_request_scope` call still lands before retrieval's own ACL check, which would fail
    retrieval outright rather than produce a clean "cited but not counted" split. Constructing
    that would test the wrong thing.

    What this pins instead is the mechanical fact Finding F actually asks for: the scope is
    derived EXACTLY ONCE per request, not once per caller of `_request_scope`, by counting
    real calls to `grant_registry.live_grants_for` - the one thing `_request_scope`'s
    grant-side derivation touches - across a single `/chat/stream` call. One derivation
    means there is no seam for a race to land in at all; a second derivation is the seam
    Finding F named, whether or not this particular test can force a state change into the
    gap between them."""
    _real_login()
    _seed_accounts()
    doc_id = "doc-600-hr-race"
    secret = "The Toronto sabbatical policy in doc-600-hr-race allows 4 weeks unpaid."
    r = client.post("/ingest", cookies=_cookie(BOB), json={
        "external_id": doc_id, "title": "hr policy race", "text": secret,
        "acl": [BOB], "uri": f"upload://{doc_id}.txt"})
    assert r.status_code == 200, r.text[:200]

    grant = _edition.grant_registry.create(
        doc_external_id=doc_id, tenant_id=BOB_PARTITION, grantee_oid=ALICE,
        granted_by=BOB, conv_id="c-race")
    touched = _edition.index.add_doc_principals(BOB_PARTITION, doc_id, [grant.principal])
    assert touched, "seed grant never reached the document's ACL"

    real_live_grants_for = _edition.grant_registry.live_grants_for
    calls = {"n": 0}

    def _counting_live_grants_for(oid):
        calls["n"] += 1
        return real_live_grants_for(oid)

    _edition.grant_registry.live_grants_for = _counting_live_grants_for
    try:
        r = client.post("/chat/stream", cookies=_cookie(ALICE),
                        json={"conv_id": "c-race", "question": "how many weeks of sabbatical?"})
        assert r.status_code == 200, r.text[:200]
        events = _sse_events(r.text)
        done = [e for e in events if e.get("type") == "done"][-1]
        assert doc_id in done["retrieved_docs"], done
        assert done["corpus"]["authorized_docs"] >= 1, done
        assert calls["n"] == 1, (
            f"live_grants_for ran {calls['n']} times for one /chat/stream request - the "
            f"corpus footer is re-deriving its own scope instead of reusing the one "
            f"retrieval already built, reopening the desync seam Finding F closed")
    finally:
        _edition.grant_registry.live_grants_for = real_live_grants_for
        _edition.grant_registry.drop_for_conversation("c-race", ALICE)


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
