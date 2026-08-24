"""#689 / ADR 0025 slice 1, at the wire: /chat and /chat/stream delegate to the router.

This is the file that would have caught the founding defect. #689 was found by TYPING THE
SAME QUESTION INTO TWO SURFACES and reading two different answers - invisible to an API drive
of /router/ask, which answers it correctly, and invisible to every unit test of either half.
So these assertions are made against the shipped endpoints, with a real edition, a real
composed workspace and real uploaded documents.

WHAT IS PINNED:
  · flag OFF is byte-identical to before this card                -> test_flag_off_*
  · flag ON, something composed: the answer carries the router's
    proof apparatus, and /chat agrees with /chat/stream           -> test_flag_on_*
  · flag ON, nothing composed: the document answer, unchanged     -> test_nothing_composed_*
  · a shared thread's continuation stays on the document plane    -> test_shared_thread_*
  · no account id reaches the wire on the routed path (#576/#549) -> test_no_owner_oid_*

    PYTHONPATH=src python3 tests/selftest_689_chat_delegation.py
"""
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
os.environ["DBSEARCH_RATE_LIMIT"] = "0"    # this file asks a lot of questions as one identity
os.environ.pop("DBSEARCH_ASK_ROUTES", None)          # every test sets it explicitly
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
ALICE = {"X-DBSearch-User": "alice"}
BOB = {"X-DBSearch-User": "bob"}

#: A store whose content nothing in the document index could answer from, so an answer that
#: names it can only have come through the router.
LEDGER = {"regional_totals": {"columns": ["region", "amount"],
                              "rows": [["apac", 205000], ["emea", 125000],
                                       ["amer", 195000]]}}


def _routes_on(on=True):
    os.environ["DBSEARCH_ASK_ROUTES"] = "1" if on else "0"


def _compose(headers=ALICE, store_id="ledger-1"):
    r = client.post("/router/compose", headers=headers, json={"manifest": {
        "tenant": "acme", "stores": [{
            "id": store_id, "kind": "csv", "business_unit": "finance",
            "title": "Regional totals", "description": "total amount by region",
            "acl": ["alice", "bob"], "config": {"tables": LEDGER}}]}})
    assert r.status_code == 200, r.text


def _seed_doc(doc_id, title, text, acl=("alice",), headers=ALICE):
    r = client.post("/ingest", headers=headers, json={
        "external_id": doc_id, "title": title, "text": text, "acl": list(acl), "uri": ""})
    assert r.status_code == 200, r.text


def _stream(question, conv_id, headers=ALICE):
    """POST /chat/stream and return (final done event, every event)."""
    r = client.post("/chat/stream", headers=headers,
                    json={"conv_id": conv_id, "question": question})
    assert r.status_code == 200, r.text
    events = [json.loads(line[6:]) for line in r.text.splitlines()
              if line.startswith("data: ")]
    done = [e for e in events if e.get("type") == "done"]
    assert len(done) == 1, f"expected exactly one done event, got {len(done)}"
    return done[0], events


def test_flag_off_leaves_the_document_path_untouched():
    """The safe intermediate state the deploy relies on: ship dark, verify, then switch."""
    _routes_on(False)
    _compose()
    _seed_doc("hol-off", "Holiday policy", "Staff receive 25 days of annual leave.")
    done, _ = _stream("how many days of annual leave", "c-689-off")
    assert "footnotes" not in done, \
        f"the router apparatus appeared with the flag OFF: {sorted(done)}"
    assert "routing" not in done, sorted(done)
    assert done["answer"], "the document path stopped answering"


def test_flag_on_routes_a_question_the_documents_cannot_answer():
    """THE CARD, at the wire. Before this, /ask said 'I do not have that information' to a
    question /canvas answered from Azure SQL, minutes apart, same account."""
    _routes_on()
    _compose()
    done, events = _stream("what is the total amount by region", "c-689-on")
    assert done.get("footnotes"), f"no router footnotes on the routed path: {sorted(done)}"
    assert any(f["store_id"] == "ledger-1" for f in done["footnotes"]), done["footnotes"]
    assert any(e.get("type") == "token" for e in events), \
        "the routed path did not stream - the surface would sit blank until the whole answer"


def test_flag_on_still_answers_from_documents():
    """The no-regression half, and the reason the overlay exists at all: /router/ask cannot
    see the edition's uploads, so a naive delegation would trade one blindness for another."""
    _routes_on()
    _compose()
    _seed_doc("hol-on", "Holiday and Annual Leave Policy",
              "Full-time staff receive 25 days of paid annual leave each calendar year.")
    done, _ = _stream("how many days of paid annual leave do staff receive", "c-689-docs")
    assert done["citations"], f"the document plane vanished once routing turned on: {done}"
    assert done["retrieved_docs"], done


def test_a_caller_who_can_see_no_store_degrades_to_the_document_answer():
    """ADR 0025's degrade clause, which is why the empty-state copy needs no change.

    THE PRECONDITION IS ASSERTED, not assumed. The first version of this test took "bob
    composed nothing" to mean "bob sees no catalog" and passed for the wrong reason twice
    over: on a dev-header rig every caller shares ONE workspace (#368), so bob sat behind
    alice's catalog - and the store alice composed was ACL'd to bob as well. A degrade test
    that never reached the degrade proves nothing, so this one composes a store bob is
    explicitly NOT in the ACL of and checks gate #1 agrees before asking anything."""
    _routes_on()
    r = client.post("/router/compose", headers=ALICE, json={"manifest": {
        "tenant": "acme", "stores": [{
            "id": "alice-private", "kind": "csv", "business_unit": "finance",
            "title": "Private ledger", "description": "total amount by region",
            "acl": ["alice"], "config": {"tables": LEDGER}}]}})
    assert r.status_code == 200, r.text
    seen = client.get("/router/catalog", headers=BOB)
    assert seen.status_code != 200 or not _stores_in(seen.json()), \
        f"PRECONDITION FAILED: bob can see a composed store, so this cannot test the degrade: {seen.text}"
    _seed_doc("bob-doc", "Bob handbook", "Bob's team works from Singapore.",
              acl=("bob",), headers=BOB)
    done, _ = _stream("where does bob's team work", "c-689-nocompose", headers=BOB)
    assert done["answer"], done
    assert not done.get("footnotes"), \
        "a caller who can see no composed store got the router's answer pipeline anyway"
    assert "routing" not in done, sorted(done)


def _stores_in(tree: dict) -> list:
    return [s for bu in tree.get("business_units", [])
            for src in bu.get("sources", []) for s in src.get("stores", [])]


def test_chat_and_chat_stream_agree():
    """A routed stream beside a document-only /chat is a divergence a client trips over by
    choosing an endpoint - the same shape of bug as #689 itself."""
    _routes_on()
    _compose()
    body = client.post("/chat", headers=ALICE, json={
        "conv_id": "c-689-nonstream", "question": "what is the total amount by region"}).json()
    assert body.get("footnotes"), f"/chat did not route while /chat/stream does: {sorted(body)}"
    assert any(f["store_id"] == "ledger-1" for f in body["footnotes"]), body["footnotes"]


def test_no_owner_oid_reaches_the_wire_on_the_routed_path():
    """#576 Finding B / #549. The routed done event is built by a DIFFERENT producer and
    carries the same server-internal key for the same reason, so a strip that named only the
    document path would have reopened the leak the day the flag went on."""
    _routes_on()
    _compose()
    _seed_doc("owner-probe", "Owner probe", "The Krakow office has twelve staff.")
    done, events = _stream("how many staff does the krakow office have", "c-689-owner")
    assert "retrieved_owners" not in done, \
        "an account id rode out on the routed stream (#576 Finding B)"
    body = client.post("/chat", headers=ALICE, json={
        "conv_id": "c-689-owner2", "question": "how many staff in krakow"}).json()
    assert "retrieved_owners" not in body, "an account id rode out on routed /chat"


def test_shared_thread_continuation_stays_on_the_document_plane():
    """A share widens the reader's DOCUMENT scope for one conversation (ADR 0020) and every
    piece of machinery around it - the readable prefix, the per-turn live-grant re-check,
    turns_withheld - is defined over documents. The router workspace is the caller's own and
    knows none of it, so a recipient asking inside a shared thread stays where the share was
    built. Widening what a share reaches is a product decision, not a flag's side effect."""
    _routes_on(False)
    conv = "c-689-shared"
    _seed_doc("shared-doc", "Shared policy", "The travel budget is 3000 per person.")
    client.post("/chat", headers=ALICE, json={"conv_id": conv,
                                              "question": "what is the travel budget"})
    r = client.post(f"/conversations/{conv}/shares", headers=ALICE,
                    json={"grantee_oid": "bob"})
    assert r.status_code == 200, f"the share did not mint, so this test cannot run: {r.text}"
    assert r.json()["documents"] >= 1, \
        f"PRECONDITION FAILED: the share grants no document, so bob's continuation would " \
        f"be refused for a reason that has nothing to do with routing: {r.json()}"
    _routes_on()
    _compose(headers=BOB, store_id="bob-ledger")
    done, _ = _stream("what is the total amount by region", conv, headers=BOB)
    assert not done.get("footnotes"), \
        "a shared thread's continuation was answered from the recipient's router workspace"


def test_bind_refuses_a_shared_thread():
    """The rule above, asserted directly on the helper, so it is pinned even where minting a
    share through the API needs fixtures this file does not build."""
    from dbsearch.server import app as appmod

    real = appmod._edition.conversation_shares.live_share_for
    appmod._edition.conversation_shares.live_share_for = lambda conv_id, user: object()
    try:
        _routes_on()
        assert appmod._bind_ask_producer("alice", "any-conv", None, None) is None, \
            "a shared thread was handed a routed producer"
    finally:
        appmod._edition.conversation_shares.live_share_for = real


def test_849_content_titles_survives_a_readscope():
    """#849, found by #689's overlay being the first caller to pass a scope here.

    `content_titles` forwarded its argument raw to `distinct_titles`, which compares it
    against `chunk.tenant_id` as a STRING - so a ReadScope (what every server-layer read path
    passes) matched nothing and returned an empty list. No error, no log: a document store
    simply lost its whole content routing signal and scored ~0 against every question, which
    reads from outside as the router declining a question its own index can answer.

    Asserted on the OBSERVABLE property, not on the normalization call: the titles come back
    for a scope exactly as they do for the bare partition it names."""
    from dbsearch.ports.base import ReadScope
    _seed_doc("titles-849", "Quarterly Freight Costs", "Freight cost by route.")
    qs = _edition_of_app().query_service
    partition = qs._tenant_id
    bare = qs.content_titles(tenant_id=partition)
    scoped = qs.content_titles(tenant_id=ReadScope(partition=partition))
    assert "Quarterly Freight Costs" in bare, bare
    assert scoped == bare, \
        f"a ReadScope returned different titles from the partition it names: {scoped} != {bare}"


def _edition_of_app():
    from dbsearch.server import app as appmod
    return appmod._edition


def test_a_reopened_routed_turn_replays_its_proof_and_can_verify_it():
    """ADR 0025: "transcripts must carry router provenance". A turn answered from Azure SQL
    persists its citations + proof the way a document turn persists doc citations, and the
    reopened thread renders them through the same builder.

    The rerun token is minted AT READ TIME, never stored: it binds (store, sql, user), so a
    stored one would be a token issued to somebody else or one outliving its identity. Proven
    by SPENDING it - a token that verifies is the only kind worth rendering a button for."""
    _routes_on()
    _compose()
    conv = "c-689-transcript"
    done, _ = _stream("what is the total amount by region", conv)
    assert done.get("footnotes"), f"the fixture never produced a routed turn: {done}"
    t = client.get(f"/conversations/{conv}/transcript", headers=ALICE)
    assert t.status_code == 200, t.text
    rows = [c for turn in t.json()["turns"] for c in turn.get("citations", [])]
    proofs = [c for c in rows if c.get("kind") == "sql"]
    assert proofs, f"a routed turn reopened with no proof row: {rows}"
    p = proofs[0]
    assert p.get("sql") and p.get("origin"), p
    assert p.get("rerun_token"), "the reopened proof carries no token, so Verify data is dead"
    spent = client.post("/router/rerun", headers=ALICE, json={
        "store_id": p["store_id"], "sql": p["sql"], "token": p["rerun_token"]})
    assert spent.status_code == 200, \
        f"the token the transcript minted does not actually re-run: {spent.status_code} {spent.text}"
    assert spent.json()["rows"], spent.json()


def test_a_persisted_proof_carries_every_row_its_query_returned():
    """A three-region query must not record one region. The snippets were being collected into
    a dict keyed by (store, sql), so the last footnote won and all three proof rows recorded
    the same single row - a transcript that says "emea 125000" under an answer about three
    regions is evidence that contradicts the answer above it.

    THE CLAIM IS UNCHANGED BY #855; the shape of the answer to it is. The first fix joined
    every row onto every citation, which made the citations identical and let the persisted
    list dedupe them down to one - and the answer's [2] and [3], numbered against the live
    list, were left pointing at nothing. Now each citation carries ITS OWN row, so the same
    question is asked of the rows COLLECTIVELY: every region the query returned must still be
    findable in what was persisted, wherever it now lives."""
    _routes_on()
    _compose()
    conv = "c-689-snips"
    d, _ = _stream("what is the total amount by region", conv)
    assert len([f for f in d.get("footnotes", []) if f["kind"] == "sql"]) >= 2, \
        f"PRECONDITION FAILED: the query returned fewer than two rows, so nothing could be lost: {d.get('footnotes')}"
    stored = [c for t in _edition_of_app().conversation_service.history("alice", conv)
              for c in (t.citations or []) if c.get("store_id")]
    assert stored, "no proof row was persisted at all"
    blob = " ".join(c.get("snippet", "") for c in stored).lower()
    for region in ("apac", "emea", "amer"):
        assert region in blob, \
            f"the persisted proof lost rows its query returned - {region} missing from {blob!r}"
    # #855: and they are DISTINCT rows, not one row copied. A dict that kept only the last
    # footnote passes the loop above the moment the snippets are joined, so the loop alone
    # cannot tell the two failures apart.
    snippets = [c.get("snippet", "") for c in stored]
    assert len(set(snippets)) == len(snippets), \
        f"the persisted proof rows are indistinguishable, so any dedupe collapses them: {snippets}"


def test_a_stored_proof_row_holds_no_token_at_rest():
    """The read path mints; the store must not hold one. Asserted against the STORE rather
    than the response, because a token absent from the wire but present at rest is still a
    credential sitting in a database outliving the identity it was bound to."""
    _routes_on()
    _compose()
    conv = "c-689-atrest"
    _stream("what is the total amount by region", conv)
    turns = _edition_of_app().conversation_service.history("alice", conv)
    stored = [c for t in turns for c in (t.citations or [])]
    assert any(c.get("store_id") for c in stored), f"no proof row was persisted at all: {stored}"
    for c in stored:
        assert "rerun_token" not in c, f"a rerun token was persisted: {c}"


def test_a_routed_turn_TRAVELS_when_the_grantor_consented():
    """#851, the owner's ruling on #850, and the default case.

    Sharing exists to reach people who do NOT have access - an HR thread handed to an
    onboarding hire is the canonical case. So a routed turn travels when the grantor agreed to
    that source, which is the default in the checklist, and the recipient reads the answer and
    the FROZEN evidence she saw. Not a capability: no re-run token travels with it."""
    _routes_on()
    _compose()
    conv = "c-851-consented"
    _seed_doc("consent-doc", "Onboarding basics", "New joiners get a laptop on day one.")
    _stream("what do new joiners get", conv)
    d1, _ = _stream("what is the total amount by region", conv)
    assert any(f["kind"] == "sql" for f in d1.get("footnotes", [])), \
        f"PRECONDITION FAILED: turn 1 did not reach the store: {d1.get('footnotes')}"
    _stream("and what do new joiners get again", conv)
    r = client.post(f"/conversations/{conv}/shares", headers=ALICE, json={"grantee_oid": "bob"})
    assert r.status_code == 200, r.text
    assert r.json()["turns_withheld"] == 0, \
        f"a consented source still withheld turns: {r.json()}"
    assert "ledger-1" in r.json()["shared_stores"], r.json()
    t = client.get(f"/conversations/{conv}/transcript", headers=BOB)
    assert t.status_code == 200, t.text
    theirs = [x for x in t.json()["turns"] if not x["own"]]
    assert len(theirs) == 3, \
        f"the whole thread did not travel: {[x['question'] for x in theirs]}"
    proofs = [c for x in theirs for c in x.get("citations", []) if c.get("kind") == "sql"]
    assert proofs, "the recipient got the answer with no evidence behind it"
    assert all("rerun_token" not in c for c in proofs), \
        "a re-run token travelled to the recipient - consent hands over a record, not access"


def test_an_unticked_store_stops_the_share_and_the_sharer_is_told():
    """The narrowing half. Unticking a source is how the grantor says "not that one", and it
    behaves exactly as an unchecked document does: the transcript STOPS there rather than
    skipping, because every later turn was condensed against the withheld answer (#601
    IMPORTANT-C), and the sharer is told through turns_withheld."""
    _routes_on()
    _compose()
    conv = "c-689-sharestop"
    _seed_doc("share-stop-doc", "Travel policy", "The travel budget is 3000 per person.")
    # turn 0: answered from DOCUMENTS. With the flag on this still goes through the router -
    # the caller's documents are a store in the ask scope - so "was it routed" is the wrong
    # question. What decides whether a turn travels is what it PERSISTED: a document row
    # (joins to a grant, can be shared) or a store proof row (cannot). Assert that.
    _stream("what is the travel budget", conv)
    # turn 1: answered from the composed store - must stop the share here.
    d1, _ = _stream("what is the total amount by region", conv)
    assert any(f["kind"] == "sql" for f in d1.get("footnotes", [])), \
        f"PRECONDITION FAILED: turn 1 did not reach the SQL store, so the stop rule is untested: {d1}"
    stored = _edition_of_app().conversation_service.history("alice", conv)
    assert not any(c.get("store_id") for c in (stored[0].citations or [])), \
        f"PRECONDITION FAILED: turn 0 persisted a proof row, so nothing could ever travel: {stored[0].citations}"
    assert any(c.get("store_id") for c in (stored[1].citations or [])), \
        f"PRECONDITION FAILED: turn 1 persisted no proof row, so there is nothing to stop on: {stored[1].citations}"
    # turn 2: documents again - must NOT travel, because it is downstream of turn 1.
    _stream("and what is the travel budget again", conv)
    # The owner UNTICKS the source. The list is offered by /shareable, so check she was
    # actually shown it before asserting what unticking does.
    offered = client.get(f"/conversations/{conv}/shareable", headers=ALICE).json()
    assert any(s["id"] == "ledger-1" for s in offered.get("stores", [])), \
        f"the modal was never offered the source, so there was nothing to untick: {offered}"
    r = client.post(f"/conversations/{conv}/shares", headers=ALICE,
                    json={"grantee_oid": "bob", "exclude_stores": ["ledger-1"]})
    assert r.status_code == 200, r.text
    assert r.json()["shared_stores"] == [], r.json()
    assert r.json()["turns_withheld"] == 2, \
        f"the sharer was not told both downstream turns stayed behind: {r.json()}"
    t = client.get(f"/conversations/{conv}/transcript", headers=BOB)
    assert t.status_code == 200, t.text
    theirs = [x for x in t.json()["turns"] if not x["own"]]
    assert len(theirs) == 1, \
        f"the shared transcript did not stop at the routed turn: {[x['question'] for x in theirs]}"
    blob = json.dumps(t.json())
    assert "regional_totals" not in blob and "ledger-1" not in blob, \
        f"a routed turn's store reached the recipient's transcript: {blob[:400]}"


def test_the_read_path_stops_a_share_ROW_that_predates_this_rule():
    """WHY THE READ-SIDE STOP EXISTS AT ALL, made individually falsifiable.

    Three places enforce "a routed turn does not travel": the share-creation cut, the
    post-grant recount, and the transcript read. They are redundant on purpose, which means an
    end-to-end test of a freshly-minted share cannot tell you whether any ONE of them works -
    mutate any single site and the other two still produce the right answer. That is the trap
    a three-clause guard sets for its own author.

    The read-side rule earns its keep on a row that ALREADY EXISTS: a share minted before this
    change carries a cutoff computed without the rule AND an empty `shared_stores`, because
    its grantor was never asked which sources to pass on. No amount of correctness at create
    time reaches that row. So this fabricates exactly that - a stored row with no recorded
    consent, widened back to cover a routed turn - and asserts the read still refuses.
    `set_turn_cutoff` cannot be used for it, deliberately (it narrows only), so the row is
    edited directly, which is what "written before the rule existed" means."""
    _routes_on()
    _compose()
    conv = "c-689-oldrow"
    _seed_doc("oldrow-doc", "Parking policy", "Parking is free after 6pm.")
    _stream("when is parking free", conv)                       # turn 0: documents
    d1, _ = _stream("what is the total amount by region", conv)  # turn 1: routed
    assert any(f["kind"] == "sql" for f in d1.get("footnotes", [])), \
        f"PRECONDITION FAILED: turn 1 did not reach the store: {d1.get('footnotes')}"
    r = client.post(f"/conversations/{conv}/shares", headers=ALICE,
                    json={"grantee_oid": "bob", "exclude_stores": ["ledger-1"]})
    assert r.status_code == 200, r.text
    share_id = r.json()["share_id"]
    shares = _edition_of_app().conversation_shares
    row = shares.live_share_for(conv, "bob")
    assert row is not None and row.turn_cutoff == 1, \
        f"PRECONDITION FAILED: create time did not already cut at the routed turn: {row}"
    # A row written before #851: no recorded consent, and a boundary computed without the rule.
    widened = replace(row, turn_cutoff=3, shared_stores=[])
    shares._by_id[share_id] = widened
    reread = shares.live_share_for(conv, "bob")
    assert reread.turn_cutoff == 3, \
        f"PRECONDITION FAILED: the widened row did not take, so the read path is untested: {reread}"
    t = client.get(f"/conversations/{conv}/transcript", headers=BOB)
    assert t.status_code == 200, t.text
    theirs = [x for x in t.json()["turns"] if not x["own"]]
    assert len(theirs) == 1, \
        f"an existing share row carried a routed turn through: {[x['question'] for x in theirs]}"
    assert "ledger-1" not in json.dumps(t.json()), json.dumps(t.json())[:300]


def test_the_grantors_half_never_carries_a_rerun_token():
    """Signing for somebody else's turn would hand the reader a credential to execute a query
    against a store they cannot see - gate #1 defeated by a transcript.

    THIS GUARD BECAME REAL WITH #851, and the history is worth keeping. Under #689's first
    design a routed turn stopped the share outright, so the grantor's half could never hold a
    proof row, only proof rows are ever signed, and this assertion could not fail - which was
    written down here rather than dressed up. The owner's ruling replaced the stop with
    CONSENT, so a consented routed turn now travels with its proofs and the no-token rule is
    the whole of the difference between passing on evidence and passing on access. Pinned from
    both directions: this test covers the document-only thread, and
    test_a_routed_turn_TRAVELS_when_the_grantor_consented covers the routed one."""
    _routes_on(False)
    conv = "c-689-nograntortoken"
    _seed_doc("gr-doc", "Expenses policy", "Expenses are reimbursed within 30 days.")
    _stream("how long do expenses take to reimburse", conv)
    r = client.post(f"/conversations/{conv}/shares", headers=ALICE, json={"grantee_oid": "bob"})
    assert r.status_code == 200, r.text
    t = client.get(f"/conversations/{conv}/transcript", headers=BOB).json()
    theirs = [x for x in t["turns"] if not x["own"]]
    assert theirs, "PRECONDITION FAILED: nothing of the grantor's half travelled"
    for turn in theirs:
        for c in turn.get("citations", []):
            assert "rerun_token" not in c, f"the grantor's half carried a token: {c}"


def test_the_flag_is_an_allowlist():
    """#315's rule: a typo means OFF. DBSEARCH_ASK_ROUTES=flase must not enable a feature."""
    from dbsearch.server.app import ask_routes_enabled
    for bad in ("flase", "", "0", "off", "no", "maybe"):
        os.environ["DBSEARCH_ASK_ROUTES"] = bad
        assert not ask_routes_enabled(), f"{bad!r} enabled the feature"
    for good in ("1", "true", "YES", "on"):
        os.environ["DBSEARCH_ASK_ROUTES"] = good
        assert ask_routes_enabled(), f"{good!r} did not enable the feature"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}\n        {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
