"""#689 / ADR 0025 - the Ask surface routes to every composed store.

THE DEFECT (found live 260812e, same account, same question, minutes apart):
  /canvas  "what is the total amount by region?" -> routed to azure_sql-1, real figures
  /ask     THE SAME QUESTION                     -> "I do not have that information"
Ask is the first item in the nav and the surface named after the product's core verb, and it
answered from the document index alone because `/chat/stream` -> ConversationService ->
QueryService is the document plane and nothing in that path has ever seen the router.

WHAT THIS FILE PINS, slice by slice (ADR 0025's own order):
  1. `decorate_ask_result` is ONE function, so the delegate and /router/ask cannot render
     the same answer two different ways -> test_decorate_*
  2. the caller's documents become a first-class store in the ASK scope only, visible to
     nobody else (gate #1) and never mutating the shared catalog -> test_overlay_*
  3. the router can stream, and a post-pass rewrite (the #493 condensed pass, the #474
     rescue) still wins over what was streamed -> test_ask_stream_*
  4. the delegate answers BOTH planes in one turn, and degrades to None - not to an error,
     and not to a half-answer - when nothing is composed -> test_delegate_*

  PYTHONPATH=src python3 tests/selftest_689_ask_routes.py
"""
import json
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
for _k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
    os.environ.pop(_k, None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from dbsearch.router.provenance import verify_rerun  # noqa: E402
from dbsearch.server.router_api import decorate_ask_result  # noqa: E402


class _Catalog:
    """The smallest thing `decorate_ask_result` reads: `.get(id).profile.origin`.
    An id it does not know RAISES, because that is what a real StoreCatalog does and the
    degradation-to-None has to be the function's own, not the fixture's."""

    def __init__(self, origins: dict) -> None:
        self._origins = origins

    def get(self, node_id: str):
        origin = self._origins[node_id]           # KeyError for an unknown id, deliberately
        return type("N", (), {"profile": type("P", (), {"origin": origin})})


AZURE = {"system": "Azure SQL", "location": "dbslivesql.database.windows.net"}


def test_decorate_signs_a_sql_citation_for_the_named_user():
    result = {"answer": "a", "evidence": [],
              "citations": [{"store_id": "azure_sql-1", "sql": "SELECT 1", "table": "Sales",
                             "proof": {"kind": "sql", "store_id": "azure_sql-1",
                                       "sql": "SELECT 1"}}]}
    decorate_ask_result(result, _Catalog({"azure_sql-1": AZURE}), "alice")
    token = result["citations"][0]["proof"]["rerun_token"]
    assert verify_rerun("azure_sql-1", "SELECT 1", "alice", token), \
        "the token the decorator issued does not verify for the user it was issued to"
    assert not verify_rerun("azure_sql-1", "SELECT 1", "bob", token), \
        "alice's proof token verified for bob - the token is not user-bound"


def test_decorate_builds_footnotes_from_evidence_in_merged_order():
    result = {"answer": "a", "citations": [],
              "evidence": [
                  {"store_id": "azure_sql-1", "business_unit": "sales", "content": "APAC=205000",
                   "provenance": {"sql": "SELECT region", "table": "Sales"}},
                  {"store_id": "documents", "business_unit": "documents", "content": "25 days",
                   "provenance": {"doc": "d1", "title": "Holiday Policy"}}]}
    decorate_ask_result(result, _Catalog({"azure_sql-1": AZURE,
                                          "documents": {"system": "Documents"}}), "alice")
    fns = result["footnotes"]
    assert [f["n"] for f in fns] == [1, 2], f"footnote numbering is not 1..n: {fns}"
    assert [f["kind"] for f in fns] == ["sql", "document"], \
        f"the two planes did not both survive into footnotes: {fns}"
    assert fns[0]["rerun_token"] and not fns[1]["rerun_token"], \
        "a document footnote was handed a re-run token, or a SQL one was not"
    assert "Azure SQL" in fns[0]["origin"] and "table Sales" in fns[0]["origin"], fns[0]


def test_decorate_degrades_an_unknown_store_to_a_readable_fallback():
    """A store id the catalog cannot resolve must not raise out of the ask path: the
    overlay, a recompose between ask and render, or a demo/live mismatch can all produce
    one, and an answer the user can read beats a 500 they cannot."""
    result = {"answer": "a", "citations": [{"store_id": "gone", "title": "Some doc"}],
              "evidence": [{"store_id": "gone", "business_unit": "bu", "content": "x",
                            "provenance": {}}]}
    decorate_ask_result(result, _Catalog({}), "alice")
    assert result["citations"][0]["origin"] == "Some doc", result["citations"][0]
    assert result["footnotes"][0]["origin"] == "bu", result["footnotes"][0]
    assert result["footnotes"][0]["system"] == "", result["footnotes"][0]


# --- slice 1b: the documents overlay (server/ask_router.py) ---------------------------------

class _Chunk:
    """A RetrievedChunk as `IndexedStore.retrieve` reads it, owner attribution included."""

    def __init__(self, owner_oid):
        self.owner_oid = owner_oid
        self.doc_external_id = f"doc-of-{owner_oid}"
        self.title = "Holiday and Annual Leave Policy"
        self.uri = ""
        self.text = "Staff receive 25 days of annual leave."
        self.locator = {}
        self.score = 0.9


class _StubQs:
    """The QueryService surface the overlay touches: two reads and one retrieval."""

    def __init__(self, has_content=True, owners=("acct-alice",)) -> None:
        self._has = has_content
        self._owners = owners
        self.calls = []

    def has_visible_content(self, user_oid, tenant_id=None):
        self.calls.append(("has", user_oid, tenant_id))
        return self._has

    def content_titles(self, limit=40, tenant_id=None):
        self.calls.append(("titles", tenant_id))
        return ["Holiday and Annual Leave Policy"]

    def retrieve(self, user_oid, question, **kw):
        self.calls.append(("retrieve", user_oid, kw.get("tenant_id")))
        return [_Chunk(o) for o in self._owners]


class _StubEdition:
    def __init__(self, qs) -> None:
        self.query_service = qs
        self.identity = type("I", (), {"expand_groups": staticmethod(lambda u: [u])})()
        self.embedder = type("E", (), {"embed": staticmethod(lambda ts: [[0.1] * 8
                                                                        for _ in ts])})()


class _BaseCatalog:
    """Stands in for a composed StoreCatalog: one store, visible to alice only."""

    def __init__(self, ids=("azure_sql-1",)) -> None:
        self.revision = 7
        self._nodes = {i: _node(i) for i in ids}

    def stores(self):
        return list(self._nodes.values())

    def get(self, node_id):
        return self._nodes[node_id]

    def children(self, node_id):
        return []

    def visible_stores(self, principals):
        return [n for n in self._nodes.values() if set(n.acl) & set(principals)]


def _node(nid):
    from dbsearch.router.catalog import STORE, CatalogNode
    return CatalogNode(id=nid, kind=STORE, parent_id=None, acl=["alice"])


def test_overlay_shows_the_documents_node_to_its_owner_alone():
    """Gate #1 on the node this seam invents. A store a user cannot see must not exist for
    them - and 'invisible == nonexistent' is the rule the whole router is built on."""
    from dbsearch.server.ask_router import DocsOverlayCatalog, documents_node
    ed = _StubEdition(_StubQs())
    node = documents_node(ed, "alice", None)
    cat = DocsOverlayCatalog(_BaseCatalog(), node, "alice")
    assert any(n.id == "documents" for n in cat.visible_stores(["alice"])), \
        "the owner cannot see her own documents store"
    assert not any(n.id == "documents" for n in cat.visible_stores(["bob"])), \
        "bob can see alice's documents store - gate #1 is broken by the overlay"


def test_overlay_consults_the_documents_node_for_its_owner_alone():
    """#856 opens a SECOND door into a routed ask: `always_consulted`. A visibility check on
    only the first one would let one caller's documents be read inside another caller's turn -
    the same leak this file already pins on `visible_stores`, through a door that did not
    exist when that test was written."""
    from dbsearch.server.ask_router import DocsOverlayCatalog, documents_node
    ed = _StubEdition(_StubQs())
    cat = DocsOverlayCatalog(_BaseCatalog(), documents_node(ed, "alice", None), "alice")
    assert [n.id for n in cat.always_consulted(["alice"])] == ["documents"], \
        "the owner's own documents are not consulted on her routed turn"
    assert cat.always_consulted(["bob"]) == [], \
        "bob's routed turn would read alice's documents - gate #1 broken by the second door"


def test_overlay_never_mutates_the_base_catalog():
    """The base is long-lived workspace state /router/ask and the canvas read concurrently.
    A per-request view that wrote to it would leak one caller's node into another's routing."""
    from dbsearch.server.ask_router import DocsOverlayCatalog, documents_node
    ed = _StubEdition(_StubQs())
    base = _BaseCatalog()
    before = (base.revision, {n.id for n in base.stores()})
    cat = DocsOverlayCatalog(base, documents_node(ed, "alice", None), "alice")
    cat.stores(), cat.visible_stores(["alice"]), cat.revision
    assert (base.revision, {n.id for n in base.stores()}) == before, \
        "the overlay mutated the shared catalog"
    assert len(cat.stores()) == len(base.stores()) + 1, "the overlay did not add its node"


def test_no_documents_means_no_node_at_all():
    """A store that exists and answers nothing can be routed to INSTEAD of a database that
    would have answered - #808's defect, invented fresh. No documents, no node."""
    from dbsearch.server.ask_router import DocsOverlayCatalog, documents_node
    ed = _StubEdition(_StubQs(has_content=False))
    assert documents_node(ed, "alice", None) is None
    base = _BaseCatalog()
    cat = DocsOverlayCatalog(base, None, "alice")
    assert [n.id for n in cat.stores()] == [n.id for n in base.stores()], \
        "a None node still changed what the catalog reports"
    assert cat.revision == base.revision, "a None node still perturbed the route-cache key"


def test_the_documents_store_reads_the_callers_own_partition():
    """#439: an ask must read the caller's ADR 0012 partition, not the deployment constant.
    The scope handed in has to reach QueryService verbatim, exactly as /search passes it."""
    from dbsearch.server.ask_router import documents_node
    qs = _StubQs()
    node = documents_node(_StubEdition(qs), "alice", "tenant-b")
    node.store.retrieve(node.store.authorize("alice"), "q")
    assert ("has", "alice", "tenant-b") in qs.calls, qs.calls
    assert ("retrieve", "alice", "tenant-b") in qs.calls, \
        f"the retrieval did not carry the caller's scope: {qs.calls}"


def test_the_node_stands_aside_for_a_store_the_caller_already_named():
    """Catalog ids share ONE namespace (#114). If someone composed a store called
    `documents`, the overlay must not shadow it in `get`."""
    from dbsearch.server.ask_router import DocsOverlayCatalog, documents_node
    ed = _StubEdition(_StubQs())
    base = _BaseCatalog(ids=("documents",))
    node = documents_node(ed, "alice", None, base_catalog=base)
    assert node.id == "documents-yours", node.id
    cat = DocsOverlayCatalog(base, node, "alice")
    assert cat.get("documents") is base.get("documents"), \
        "the overlay shadowed the caller's own store of the same name"


def test_owner_recorder_captures_accounts_without_putting_them_in_evidence():
    """#576's retention touch has to survive the routed path, and #549 says an owner oid
    must never ride out on the wire. So it is observed in passing and read off the recorder,
    never carried on Evidence."""
    from dbsearch.server.ask_router import documents_node
    node = documents_node(_StubEdition(_StubQs(owners=("acct-a", "acct-b"))), "alice", None)
    evidence = node.store.retrieve(node.store.authorize("alice"), "q")
    assert node.store._qs.owners == {"acct-a", "acct-b"}, node.store._qs.owners
    for ev in evidence:
        assert "owner" not in str(ev.to_dict()).lower(), \
            f"an account id reached the evidence dict: {ev.to_dict()}"


# --- slice 1c: the router can stream (RouterQueryService.ask_stream) -------------------------

def _service(evidence_text="APAC=205000"):
    """A RouterQueryService over a one-store catalog whose store always answers."""
    from dbsearch.router.catalog import STORE, StoreCatalog, CatalogNode, TENANT, SOURCE
    from dbsearch.router.evidence import CHUNK, Evidence
    from dbsearch.router.router_service import RouterQueryService
    from dbsearch.router.store import AccessContext, INDEXED, SEMANTIC, StoreProfile

    class _Store:
        def profile(self):
            return StoreProfile(store_id="s1", title="Sales", description="regional sales",
                                kind=INDEXED, capabilities={SEMANTIC}, business_unit="sales")

        def authorize(self, user_oid):
            return AccessContext(user_oid=user_oid, principals=[user_oid])

        def has_content(self, access):
            return True

        def retrieve(self, access, question, top_k=5):
            return [Evidence(store_id="s1", business_unit="sales", kind=CHUNK,
                             content=evidence_text, provenance={"doc": "d1"}, score=1.0)]

    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind=TENANT, parent_id=None, acl=["alice"]))
    cat.register(CatalogNode(id="bu", kind="business_unit", parent_id="t", acl=["alice"]))
    cat.register(CatalogNode(id="src", kind=SOURCE, parent_id="bu", acl=["alice"]))
    store = _Store()
    cat.register(CatalogNode(id="s1", kind=STORE, parent_id="src", acl=["alice"],
                             profile=store.profile(), store=store))
    identity = type("I", (), {"expand_groups": staticmethod(lambda u: [u])})()
    embedder = type("E", (), {"embed": staticmethod(lambda ts: [[0.5] * 8 for _ in ts])})()
    return RouterQueryService(cat, identity, embedder)


class _StreamLlm:
    """A model that CAN stream. `pieces` is what it drips."""

    def __init__(self, pieces=("The ", "APAC ", "total ", "is 205000.")) -> None:
        self._pieces = pieces

    def answer_stream(self, question, context):
        for p in self._pieces:
            yield p

    def answer(self, question, context):
        return {"answer": "".join(self._pieces)}


class _OneShotLlm:
    """A model with no streaming capability at all - the capability-gated fallback."""

    def answer(self, question, context):
        return {"answer": "The APAC total is 205000."}


def test_ask_stream_yields_tokens_then_one_done():
    evs = list(_service().ask_stream("alice", "what is the apac total", _StreamLlm()))
    kinds = [e["type"] for e in evs]
    assert kinds.count("done") == 1 and kinds[-1] == "done", kinds
    assert kinds.count("token") >= 2, f"nothing actually streamed: {kinds}"
    done = evs[-1]
    for key in ("answer", "citations", "evidence", "routing", "outcomes", "disclosure"):
        assert key in done, f"done is missing {key}: {sorted(done)}"
    assert done["answer"], "done carried no answer"


def test_ask_stream_done_answer_is_what_the_client_must_render():
    """The streamed tokens are a DRAFT: the marker strip, the echo strip and the #493
    condensed pass all rewrite after the last token. A client rendering its accumulator
    instead of `done.answer` shows text the product already decided was wrong (#257)."""
    llm = _StreamLlm(pieces=("The answer ", "is 205000. ", "[coverage]"))
    evs = list(_service().ask_stream("alice", "q", llm))
    streamed = "".join(e["text"] for e in evs if e["type"] == "token")
    assert "[coverage]" in streamed, "the fixture did not reach the rewrite path"
    assert "[coverage]" not in evs[-1]["answer"], \
        "an instruction marker survived into the final answer"
    assert evs[-1]["answer"] != streamed, "done.answer is merely the streamed text"


def test_ask_stream_without_a_streaming_model_still_answers():
    """Capability-gated, like the decomposer and the planner. A deployment whose chat model
    cannot stream must get an answer, not an empty one."""
    evs = list(_service().ask_stream("alice", "q", _OneShotLlm()))
    assert [e["type"] for e in evs] == ["token", "done"], [e["type"] for e in evs]
    assert evs[-1]["answer"] == "The APAC total is 205000."


def test_ask_stream_matches_ask_on_the_same_question():
    """The two surfaces must not answer the same question differently - that IS #689."""
    svc, llm = _service(), _StreamLlm()
    streamed = list(svc.ask_stream("alice", "what is the apac total", llm))[-1]
    one_shot = svc.ask("alice", "what is the apac total", llm).to_dict()
    assert streamed["answer"] == one_shot["answer"], (streamed["answer"], one_shot["answer"])
    assert streamed["citations"] == one_shot["citations"]
    assert streamed["routing"]["method"] == one_shot["routing"]["method"]


def test_ask_stream_raises_the_workers_failure_on_the_callers_thread():
    """A generation that blows up must not come back as a silent, empty, successful answer -
    an empty success hides an outage."""
    class _Boom:
        def answer_stream(self, question, context):
            raise RuntimeError("model exploded")
            yield  # pragma: no cover - generator marker

        def answer(self, question, context):
            raise RuntimeError("model exploded")

    try:
        list(_service().ask_stream("alice", "q", _Boom()))
    except RuntimeError as exc:
        assert "model exploded" in str(exc), exc
    else:
        raise AssertionError("a failing generation streamed a successful empty answer")


# --- slice 1d: the delegate over a real edition + composed workspace -------------------------

_SALES = {"sales": {"columns": ["region", "amount"],
                    "rows": [["apac", 205000], ["emea", 125000]]}}


def _live_app(manifest_store=None):
    """A real edition + router api, per-user workspaces forced on (the live-login shape)."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from dbsearch.server import router_api
    from dbsearch.server.edition import build_edition

    def current_user(request: Request) -> str:
        return request.headers["X-Test-User"]

    edition = build_edition()
    app = FastAPI()
    api = router_api.build_router_api(edition, current_user,
                                      manifest_store=manifest_store,
                                      force_per_user_workspaces=True)
    app.include_router(api)
    return TestClient(app), api, edition


def _compose(client, user, store_id="csv-sales", acl=None):
    manifest = {"tenant": "acme", "stores": [
        {"id": store_id, "kind": "csv", "business_unit": "sales",
         "title": "Regional sales", "description": "sales amounts by region",
         "acl": list(acl or [user]), "config": {"tables": _SALES}}]}
    r = client.post("/router/compose", json={"manifest": manifest},
                    headers={"X-Test-User": user})
    assert r.status_code == 200, r.text
    return manifest


def _ingest(edition, external_id, title, text, acl, owner):
    edition.ingest_document(external_id, title, text, list(acl), "", owner_oid=owner)


def _done(producer, question):
    return [e for e in producer(question)][-1]


def test_delegate_is_none_when_nothing_is_composed():
    """The ADR's degrade clause. A fresh user has no catalog, so the routed path becomes the
    document path - which is today's behaviour, and why the empty-state copy stays true."""
    client, api, _ = _live_app()
    assert api.ask_delegate("alice", None, None) is None


def test_delegate_answers_from_a_composed_store():
    client, api, edition = _live_app()
    _compose(client, "alice")
    produce = api.ask_delegate("alice", None,
                               edition.chat_models[edition.chat_model_default])
    done = _done(produce, "what is the total amount by region")
    assert done["type"] == "done", done
    assert done["citations"], f"a composed SQL store produced no citations: {done}"
    assert any(f["store_id"] == "csv-sales" for f in done["footnotes"]), done["footnotes"]
    assert done["footnotes"][0]["rerun_token"], "the routed footnote carries no proof token"


def test_delegate_answers_from_documents_too():
    """THE POINT OF THE OVERLAY. /router/ask cannot see the edition's uploaded documents,
    so a delegate that merely called it would answer every database and lose the one plane
    /ask can answer from today - #689's own defect, pointing the other way."""
    client, api, edition = _live_app()
    _compose(client, "alice")
    _ingest(edition, "hol-1", "Holiday and Annual Leave Policy",
            "Staff receive 25 days of paid annual leave each year.", ["alice"], "alice")
    produce = api.ask_delegate("alice", None,
                               edition.chat_models[edition.chat_model_default])
    done = _done(produce, "how many days of annual leave do staff receive")
    kinds = {f["kind"] for f in done["footnotes"]}
    assert "document" in kinds, f"the document plane never reached the answer: {done}"
    assert done["retrieved_docs"] == ["hol-1"], done["retrieved_docs"]
    assert done["retrieved_owners"] == ["alice"], done["retrieved_owners"]


def test_delegate_never_names_another_users_documents():
    """LAW 2, DOCUMENT plane. The edition's index is SHARED across accounts - alice's and
    bob's uploads are rows in one index - so the trim is the only thing between them, and
    the overlay must run it as the CALLER. Swept over the whole payload rather than over the
    fields I happened to think of: citations, footnotes, routing, outcomes and disclosure
    all carry text, and a leak in any one of them is a leak."""
    client, api, edition = _live_app()
    _compose(client, "bob", store_id="bob-only")
    _ingest(edition, "alice-doc", "Krakow headcount plan",
            "The Krakow office headcount was cut in half.", ["alice"], "alice")
    _ingest(edition, "bob-doc", "Bob onboarding", "Bob's own notes.", ["bob"], "bob")
    produce = api.ask_delegate("bob", None,
                               edition.chat_models[edition.chat_model_default])
    blob = json.dumps(_done(produce, "what happened to the krakow headcount"))
    for secret in ("alice-doc", "Krakow headcount plan", "cut in half"):
        assert secret not in blob, \
            f"bob's routed answer leaked {secret!r} - the document trim is not the caller's"


def test_delegate_never_names_a_store_the_caller_cannot_see():
    """LAW 2, STORE plane - gate #1 THROUGH the overlay.

    The fixture is a SHARED workspace holding both stores with different ACLs, because that
    is the only arrangement in which this can fail. Per-user workspaces (the live-login
    shape) give bob a catalog that never contained alice's store at all, so the same
    assertion there passes whether or not the overlay honours visibility - a rig that cannot
    show the bug. Here the store IS in the catalog the delegate routes over, and only the
    trim keeps it out of the answer."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from dbsearch.server import router_api
    from dbsearch.server.edition import build_edition

    def current_user(request: Request) -> str:
        return request.headers["X-Test-User"]

    edition = build_edition()
    app = FastAPI()
    api = router_api.build_router_api(edition, current_user,
                                      force_per_user_workspaces=False)   # ONE catalog
    app.include_router(app_router := api)
    client = TestClient(app)
    manifest = {"tenant": "acme", "stores": [
        {"id": "alice-secret-ledger", "kind": "csv", "business_unit": "finance",
         "title": "Krakow headcount ledger", "description": "krakow office headcount",
         "acl": ["alice"], "config": {"tables": {"headcount": {
             "columns": ["office", "heads"], "rows": [["krakow", 12]]}}}},
        {"id": "bob-only", "kind": "csv", "business_unit": "sales",
         "title": "Bob sales", "description": "sales amounts by region",
         "acl": ["bob"], "config": {"tables": _SALES}}]}
    r = client.post("/router/compose", json={"manifest": manifest},
                    headers={"X-Test-User": "alice"})
    assert r.status_code == 200, r.text
    produce = app_router.ask_delegate("bob", None,
                                      edition.chat_models[edition.chat_model_default])
    blob = json.dumps(_done(produce, "what is the krakow office headcount"))
    for secret in ("alice-secret-ledger", "Krakow headcount ledger", "krakow office"):
        assert secret not in blob, \
            f"bob's routed answer named {secret!r} - gate #1 is broken through the overlay"


def test_delegate_refuses_a_demo_identity():
    """The chat routes depend on the LIVE-only current_user, which 403s demo:*. Asserted
    rather than assumed: #340 is the card about a demo/live mispairing nobody could see."""
    client, api, _ = _live_app()
    try:
        api.ask_delegate("demo:alice", None, None)
    except AssertionError:
        return
    raise AssertionError("the ask delegate accepted a demo identity")


def test_delegate_survives_a_workspace_store_outage():
    """/router/compose must fail closed on an unreadable manifest table (#200). An ASK must
    not: the caller still has documents to answer from, and 503-ing the Ask box because the
    manifest table is down takes a working surface offline."""
    from dbsearch.server.manifest_store import InMemoryManifestStore, ManifestStoreUnavailable

    class _Down(InMemoryManifestStore):
        def get(self, key):
            raise ManifestStoreUnavailable("table gone")

    client, api, _ = _live_app(manifest_store=_Down())
    assert api.ask_delegate("alice", None, None) is None


# --- slice 2a: what a turn PERSISTS (_slim_citations) ----------------------------------------

def test_a_turn_keeps_both_citation_shapes():
    """One answer's evidence, one list. Splitting document rows and proof rows into separate
    columns would let a reopened turn show half of what it was built from."""
    from dbsearch.query.conversation import _slim_citations
    rows = _slim_citations([
        {"doc": "d1", "quote": "25 days", "quote_kind": "pointed", "title": "DROP-ME",
         "uri": "DROP-ME"},
        {"store_id": "azure_sql-1", "kind": "row", "origin": "Azure SQL · host · db",
         "snippet": "region=APAC amount=205000",
         "proof": {"kind": "sql", "store_id": "azure_sql-1", "sql": "SELECT region"},
         "rerun_token": "DROP-ME"},
        {"title": "neither shape"},
        {"doc": "d2", "quote": "after the gap"},
    ])
    assert rows[0] == {"doc": "d1", "quote": "25 days", "quote_kind": "pointed"}, rows[0]
    assert rows[1] == {"kind": "sql", "store_id": "azure_sql-1", "sql": "SELECT region",
                       "origin": "Azure SQL · host · db",
                       "snippet": "region=APAC amount=205000"}, rows[1]
    # #855: three in, three out. A row that is neither shape says NOTHING and still HOLDS
    # ITS SLOT - dropping it would renumber every marker after it, which is the lie
    # `_citation_rows` names in its own docstring for the document plane.
    assert len(rows) == 4, f"an unresolvable citation shifted the rows after it: {rows}"
    assert rows[2] == {}, f"a row nothing can resolve stored something anyway: {rows[2]}"
    assert rows[3] == {"doc": "d2", "quote": "after the gap"}, \
        f"[4] no longer resolves to the document the answer pointed at: {rows}"


def test_one_query_persists_as_one_proof_PER_CITATION():
    """REVERSED BY #855, deliberately, and this docstring is the record of why.

    This test used to assert that a SELECT returning three rows persisted as ONE proof,
    deduped by the whole slimmed row, so a reopened transcript stopped rendering three
    identical Verify data buttons. That was measured on the transcript and it was true - and
    it broke something the fixture never looked at. The answer's `[n]` markers index this
    list POSITIONALLY, so collapsing three rows into one leaves [2] and [3] pointing at
    nothing; on prod, a reopened turn read "AMER - 195,000.00[2]" with a single-entry rail.

    The rows were only identical because `pair_proof_snippets` was joining every result row
    onto every citation. Paired by position they are three DIFFERENT rows of evidence, which
    is what they always were, and the reader gets one entry per marker.

    The original complaint is answered where it belongs: if a rail should show one entry for
    two identical proofs, it can group them at RENDER time - on both surfaces, since the live
    one shows them too. What it must not do is persist a list the answer is not numbered
    against. See tests/selftest_855_reopened_markers.py."""
    from dbsearch.query.conversation import _slim_citations
    one = {"store_id": "s1", "kind": "row", "sql": "SELECT region", "origin": "o",
           "proof": {"kind": "sql", "sql": "SELECT region"}}
    rows = _slim_citations([dict(one, snippet="apac"), dict(one, snippet="emea"),
                            dict(one, snippet="amer")])
    assert len(rows) == 3, f"three cited rows persisted as {len(rows)}: {rows}"
    assert [r["snippet"] for r in rows] == ["apac", "emea", "amer"], rows


def test_two_different_queries_against_one_store_stay_two_proofs():
    """Two genuinely different queries are two proofs and both belong on screen. Kept from
    the pre-#855 dedup as the control it always was: `store_id` would have been the obvious
    key and collapsing on it would lose a whole query, not merely a position."""
    from dbsearch.query.conversation import _slim_citations
    rows = _slim_citations([
        {"store_id": "s1", "proof": {"kind": "sql", "sql": "SELECT region"}},
        {"store_id": "s1", "proof": {"kind": "sql", "sql": "SELECT product"}},
    ])
    assert len(rows) == 2, f"two different queries collapsed into {len(rows)}: {rows}"


def test_a_stored_proof_never_carries_a_rerun_token():
    """A token is signed per (store, sql, USER). Stored, it is either minted for somebody
    else or outliving the identity it was bound to. The transcript re-signs for its reader."""
    from dbsearch.query.conversation import _slim_citations
    rows = _slim_citations([{"store_id": "s1", "kind": "sql", "sql": "SELECT 1",
                             "rerun_token": "tok", "proof": {"rerun_token": "tok"}}])
    assert "rerun_token" not in rows[0], rows[0]
    assert "rerun_token" not in str(rows[0]), rows[0]


def test_a_stored_proof_keeps_its_query_from_either_producer_shape():
    """The router nests `sql` under `proof`; the flattened footnote shape puts it at the top
    level. A proof row that lost its query is a claim with nothing behind it."""
    from dbsearch.query.conversation import _slim_citations
    nested = _slim_citations([{"store_id": "s1", "proof": {"kind": "sql", "sql": "SELECT 1"}}])
    flat = _slim_citations([{"store_id": "s1", "kind": "sql", "sql": "SELECT 1"}])
    assert nested[0]["sql"] == "SELECT 1" and nested[0]["kind"] == "sql", nested
    assert flat[0]["sql"] == "SELECT 1", flat


def test_a_stored_kind_speaks_the_proof_vocabulary_not_the_evidence_one():
    """A citation carries the EVIDENCE kind (chunk | row | record); footnotes and every
    renderer speak the PROOF kind (sql | document | record). They overlap on `record` and
    disagree everywhere else, so a row stored as "row" reads downstream as "not a SQL proof"
    and silently loses its Verify data action. One field, one vocabulary."""
    from dbsearch.query.conversation import _slim_citations
    rows = _slim_citations([
        {"store_id": "s1", "kind": "row", "proof": {"kind": "sql", "sql": "SELECT 1"}},
        {"store_id": "s2", "kind": "chunk"},          # unclassifiable: no proof to speak for it
    ])
    assert rows[0]["kind"] == "sql", rows[0]
    assert "kind" not in rows[1], \
        f"an evidence-vocabulary kind was persisted as if it were a proof kind: {rows[1]}"


# --- slice 1e: the ConversationService seam --------------------------------------------------

def _conv_service(history=()):
    """A ConversationService whose document plane is a stub, so a test can tell which
    producer actually answered."""
    from dbsearch.query.conversation import ConversationService, Turn
    from dbsearch.server.conversation_store import InMemoryConversationStore

    class _Qs:
        def __init__(self):
            self.asked = []

        def answer_stream(self, user_oid, standalone, llm=None, tenant_id=None):
            self.asked.append(standalone)
            yield {"type": "done", "answer": "DOCUMENT PLANE", "citations": [],
                   "retrieved_docs": [], "retrieved_owners": []}

    class _Llm:
        def condense_question(self, question, history):
            return f"CONDENSED({question})"

    store = InMemoryConversationStore()
    for t in history:
        store.append("c1", "alice", Turn(question=t, standalone=t, answer="prior"))
    qs = _Qs()
    return ConversationService(qs, _Llm(), store=store), qs, store


def test_the_producer_replaces_the_document_plane_and_nothing_else():
    svc, qs, store = _conv_service()

    def produce(standalone):
        yield {"type": "token", "text": "routed "}
        yield {"type": "done", "answer": "ROUTED ANSWER", "citations": [],
               "retrieved_docs": [], "retrieved_owners": []}

    evs = list(svc.ask_stream("alice", "c1", "hello", answer_producer=produce))
    assert evs[-1]["answer"] == "ROUTED ANSWER", evs[-1]
    assert qs.asked == [], "the document plane was consulted as well - two answers per turn"
    recorded = store.history("c1", "alice")
    assert len(recorded) == 1 and recorded[0].answer == "ROUTED ANSWER", recorded
    assert recorded[0].question == "hello", "the turn recorded the standalone, not what was typed"


def test_the_producer_receives_the_condensed_question():
    """A router handed 'and by region?' routes on a fragment whose subject is in the previous
    turn. Condense is the conversational surface's contribution to a routed answer."""
    svc, _, _ = _conv_service(history=["what is the total amount"])
    seen = {}

    def produce(standalone):
        seen["q"] = standalone
        yield {"type": "done", "answer": "a", "citations": [], "retrieved_docs": []}

    list(svc.ask_stream("alice", "c1", "and by region?", answer_producer=produce))
    assert seen["q"] == "CONDENSED(and by region?)", seen


def test_without_a_producer_the_document_plane_is_untouched():
    """The flag-off / nothing-composed path has to be byte-identical to before #689."""
    svc, qs, _ = _conv_service()
    evs = list(svc.ask_stream("alice", "c1", "hello"))
    assert evs[-1]["answer"] == "DOCUMENT PLANE", evs[-1]
    assert qs.asked == ["hello"], qs.asked


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
