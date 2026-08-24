"""Phase E E3b — NativeSearchStore spike (Microsoft Graph Search adapter, card #113).
Proves ADR 0008 `mode: native`: a zero-copy store behind the SAME StorePort — routing,
fan-out, synthesis and the leak gates all work unchanged over a live-search source.
Run: python3 tests/selftest_router_native.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import ExtractiveLlm, HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch import router  # noqa: E402
from dbsearch.router.native_search import (  # noqa: E402
    GraphSearchProvider, GraphSearchStore, env_token_provider,
)
from dbsearch.router.store import NATIVE_SEARCH, SEMANTIC  # noqa: E402

GRAPH_HITS = {
    "value": [{
        "hitsContainers": [{
            "hits": [
                {"hitId": "drv-1", "summary": "confidential revenue four point two million",
                 "resource": {"name": "Q3 Ledger.xlsx",
                              "webUrl": "https://acme.sharepoint.com/q3.xlsx"}},
                {"hitId": "drv-2", "summary": "invoice register fy26",
                 "resource": {"name": "Invoices.docx",
                              "webUrl": "https://acme.sharepoint.com/inv.docx"}},
            ]
        }]
    }]
}


class FakeTransport:
    def __init__(self, response=None, raise_exc=None):
        self.calls = []
        self.response = response or GRAPH_HITS
        self.raise_exc = raise_exc

    def __call__(self, path, payload, token):
        self.calls.append({"path": path, "payload": payload, "token": token})
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def _store(transport=None, token_provider=None):
    return GraphSearchStore(
        store_id="fin-sp", business_unit="finance", title="Finance SharePoint",
        description="revenue invoices tax numbers ledger accounting",
        transport=transport or FakeTransport(),
        token_provider=token_provider or (lambda user: "tok-" + user),
    )


def test_profile_is_native_search_live():
    p = _store().profile()
    assert p.kind == NATIVE_SEARCH and SEMANTIC in p.capabilities, p
    assert p.freshness == "live", p.freshness


def test_authorize_carries_delegated_token_not_principals():
    access = _store().authorize("alice")
    assert access.delegated_credential == "tok-alice", access
    assert access.principals == [], "native auth is source-side — no principals list"


def test_retrieve_calls_graph_and_maps_evidence():
    t = FakeTransport()
    s = _store(transport=t)
    access = s.authorize("alice")
    evs = s.retrieve(access, "revenue this quarter", top_k=2)
    call = t.calls[0]
    assert call["path"] == "/search/query", call
    req = call["payload"]["requests"][0]
    assert req["query"]["queryString"] == "revenue this quarter", req
    assert req["size"] == 2, req
    assert call["token"] == "tok-alice", call
    assert len(evs) == 2, evs
    e0 = evs[0]
    assert e0.kind == "record" and e0.store_id == "fin-sp" and e0.business_unit == "finance", e0
    assert "four point two million" in e0.content, e0.content
    assert e0.provenance["uri"] == "https://acme.sharepoint.com/q3.xlsx", e0.provenance
    assert e0.provenance["title"] == "Q3 Ledger.xlsx" and e0.provenance["doc"] == "drv-1", e0.provenance
    assert e0.score is None, "rank order, not raw score (ADR 0008)"


def test_missing_env_token_raises_at_authorize():
    os.environ.pop("E3B_TEST_TOKEN", None)
    s = _store(token_provider=env_token_provider("E3B_TEST_TOKEN"))
    try:
        s.authorize("alice")
        raise AssertionError("expected RuntimeError for unset token env")
    except RuntimeError as e:
        assert "E3B_TEST_TOKEN" in str(e), e
    os.environ["E3B_TEST_TOKEN"] = "t0k"
    assert s.authorize("alice").delegated_credential == "t0k"
    os.environ.pop("E3B_TEST_TOKEN", None)


def test_provider_probe_and_build():
    fake = FakeTransport()
    prov = GraphSearchProvider(transport=fake, token_provider=lambda u: "t")
    assert prov.kind == "graph_search"
    cfg = {"id": "hr-sp", "business_unit": "hr", "title": "HR SharePoint",
           "description": "people policies leave"}
    p = prov.probe(cfg)
    assert p.kind == NATIVE_SEARCH and p.store_id == "hr-sp", p
    store = prov.build(cfg)
    evs = store.retrieve(store.authorize("bob"), "leave", top_k=1)
    assert evs and evs[0].store_id == "hr-sp", evs


def _catalog_with_native(fake):
    spec = {
        "tenant": "acme",
        "stores": [
            {"id": "hr-wiki", "kind": "local", "business_unit": "hr", "acl": ["hr-staff"],
             "title": "HR Wiki",
             "description": "human resources parental leave holidays onboarding benefits",
             "config": {"seed": [{"external_id": "hb", "title": "Handbook", "uri": "u1",
                                  "acl": ["hr-staff"],
                                  "text": "parental leave is sixteen weeks"}],
                        "user_groups": {"carol": ["hr-staff", "fin-staff"]}}},
            {"id": "fin-sp", "kind": "graph_search", "business_unit": "finance",
             "acl": ["fin-staff"], "title": "Finance SharePoint",
             "description": "revenue invoices tax numbers ledger accounting",
             "config": {}},
        ],
    }
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    reg.register(GraphSearchProvider(transport=fake, token_provider=lambda u: "tok"))
    cat = router.load_manifest(spec, registry=reg)
    identity = InMemoryIdentity({"carol": ["hr-staff", "fin-staff"], "alice": ["hr-staff"]})
    return router.RouterQueryService(cat, identity, HashingEmbedding()), cat


def test_full_vertical_routes_and_cites_native_store():
    svc, _ = _catalog_with_native(FakeTransport())
    res = svc.ask("carol", "revenue invoices ledger accounting", ExtractiveLlm())
    assert res.routing["stores"][0]["store_id"] == "fin-sp", res.routing
    assert "four point two million" in res.answer, res.answer
    cite = res.citations[0]
    assert cite["uri"] == "https://acme.sharepoint.com/q3.xlsx", cite
    assert res.disclosure == "", res.disclosure


def test_native_store_existence_still_gated_for_hr_user():
    svc, _ = _catalog_with_native(FakeTransport())
    res = svc.ask("alice", "revenue invoices ledger accounting", ExtractiveLlm())
    assert "fin-sp" not in repr(res.to_dict()), "native store existence leaked (gate #1)"


def test_uncredentialed_native_store_drops_with_disclosure():
    boom = FakeTransport(raise_exc=RuntimeError("401 from Graph"))
    svc, cat = _catalog_with_native(boom)
    svc2 = router.RouterQueryService(cat, InMemoryIdentity({"carol": ["hr-staff", "fin-staff"]}),
                                     HashingEmbedding(), margin=1.0, floor_frac=0.0)
    res = svc2.ask("carol", "parental leave and revenue invoices ledger", ExtractiveLlm())
    assert "fin-sp" in res.disclosure, res.to_dict()
    assert "sixteen weeks" in res.answer, "indexed store must still answer"


def main():
    print("Phase E E3b native-search self-test:")
    test_profile_is_native_search_live()
    test_authorize_carries_delegated_token_not_principals()
    test_retrieve_calls_graph_and_maps_evidence()
    test_missing_env_token_raises_at_authorize()
    test_provider_probe_and_build()
    test_full_vertical_routes_and_cites_native_store()
    test_native_store_existence_still_gated_for_hr_user()
    test_uncredentialed_native_store_drops_with_disclosure()
    print("  PASS  profile / delegated auth / graph mapping / env token / provider / "
          "vertical cite / gate-1 / disclosed drop")
    print("\nE3B NATIVE-SEARCH SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
