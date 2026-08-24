"""Phase E E8 — scale & governance: route cache, coarse BU prune, dispatch budget.

10-BU / 30-store catalog: routing stays CORRECT under coarse→fine pruning, repeated
questions hit a per-catalog cache (invalidated by catalog revision), and a compound
fan-out can never dispatch more stores than the query budget — the cap is DISCLOSED,
never silent (no-silent-caps).

Run: python3 tests/selftest_router_e8.py
"""
import os
import sys
import time
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import ExtractiveLlm, HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch import router  # noqa: E402
from dbsearch.router.structured import MIN_SCHEMA_HASHING_DIM  # noqa: E402

# fix-round-1 (arc review, finding 1): this fixture built RouterQueryService with a bare
# HashingEmbedding() (128-dim default) - a path #450's edition-wiring fix never touched,
# since #450 only raised the dimension where build_edition() constructs the embedder for
# the memory/memory-ollama backends (server/edition.py), not test fixtures that construct
# RouterQueryService directly. Once #453 started folding a store's doc titles into its
# profile unconditionally, the extra tokens were enough to recreate the exact collision
# class #450 fixed elsewhere: at 128 dims, a "legal" query collided into "sales-store-0"
# (bisected: green through #450, red from #453). No real deployment is wired this narrow
# any more - both memory editions floor at MIN_SCHEMA_HASHING_DIM (4096) post-#450 - so this
# fixture now mirrors that real wiring instead of a dimension no product path still uses.
# Verified this doesn't blunt the test: a deliberately broken coarse_prune (inverted
# keep-direction) still misroutes all 10 BUs at this same dimension, so the test still
# fails on a genuine regression, it just no longer false-fails on a stale embedder default.


def _router_embedder() -> HashingEmbedding:
    return HashingEmbedding(dim=MIN_SCHEMA_HASHING_DIM)


# 10 BUs x 3 stores, disjoint vocabularies. Everyone in "staff" sees everything.
_BUS = ["hr", "finance", "sales", "legal", "eng", "marketing", "support", "ops",
        "research", "design"]
_WORDS = {
    "hr": "parental leave holidays onboarding benefits payroll",
    "finance": "revenue invoices tax ledger accounting audit",
    "sales": "pipeline deals quota crm prospects commission",
    "legal": "contracts compliance litigation counsel clauses",
    "eng": "deployment kubernetes incidents oncall repositories",
    "marketing": "campaigns branding webinars leads newsletter",
    "support": "tickets escalations sla csat helpdesk",
    "ops": "procurement vendors logistics facilities inventory",
    "research": "experiments papers datasets benchmarks models",
    "design": "figma mockups typography accessibility palettes",
}


def _spec():
    stores = []
    for bu in _BUS:
        words = _WORDS[bu]
        for i in range(3):
            stores.append({
                "id": f"{bu}-store-{i}", "kind": "local", "business_unit": bu,
                "acl": ["staff"], "title": f"{bu} store {i}",
                "description": f"{words} shard{i}",
                "config": {"seed": [{"external_id": f"{bu}-d{i}", "title": f"{bu} doc {i}",
                                     "uri": "u", "acl": ["staff"],
                                     "text": f"{words} content number {i}"}],
                           "user_groups": {"u1": ["staff"]}}})
    return {"tenant": "acme", "stores": stores}


def _catalog():
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    return router.load_manifest(_spec(), registry=reg)


IDENTITY = InMemoryIdentity({"u1": ["staff"], "u2": ["staff"]})


def test_catalog_revision_bumps():
    cat = router.StoreCatalog()
    r0 = cat.revision
    cat.register(router.CatalogNode("t", router.TENANT, None, acl=["g"]))
    assert cat.revision > r0, (cat.revision, r0)


def test_coarse_prune_keeps_routing_correct():
    cat = _catalog()
    svc = router.RouterQueryService(cat, IDENTITY, _router_embedder())
    for bu in _BUS:
        d = svc.route("u1", f"question about {_WORDS[bu].split()[0]} {_WORDS[bu].split()[1]}")
        assert d.stores and d.stores[0].store_id.startswith(bu + "-"), (bu, d.to_dict())
        # coarse prune engaged: candidates only from surviving BUs, not all 30 stores
        assert len(d.candidates) < 30, len(d.candidates)


def test_coarse_prune_survives_a_heterogeneous_bu(*, _bug="#321"):
    """A BU whose stores have DIFFERENT vocabularies must not lose its one matching store
    to the coarse gate. Regression for #321: scoring a BU by the MEAN of its store vectors
    dilutes a single strong match with unrelated siblings, dropping the whole BU before the
    fine pass. The demo's `sales` BU (deals + storefront + warehouse) hit exactly this: a
    'revenue per product' question routed to a finance DOCUMENT holding a single total,
    because the SQL store with product+amount columns was pruned first.

    The coarse gate asks 'could ANY store in this BU answer?' - a max, not a mean."""
    from dbsearch.router.profiles import coarse_prune, ensure_profile_vector
    from dbsearch.adapters.local import HashingEmbedding
    from dbsearch import router as R

    emb = HashingEmbedding()
    # finance: ONE store that matches 'revenue' strongly.
    # sales:   THREE stores, only ONE of which matches 'revenue product'; the other two are
    #          unrelated, so their presence drags the sales CENTROID down.
    specs = [
        ("fin-ledger", "finance", "revenue invoices tax ledger accounting"),
        ("azure-deals", "sales", "closed deals revenue amount by region product"),
        ("storefront", "sales", "storefront orders shipping returns by region category"),
        ("warehouse", "sales", "warehouse units stock movements by region sku"),
    ]
    nodes = []
    for sid, bu, desc in specs:
        prof = R.StoreProfile(store_id=sid, title=sid, description=desc,
                              kind="local", capabilities={"semantic"}, business_unit=bu)
        n = R.CatalogNode(sid, R.STORE, f"{bu}--src", acl=["staff"], profile=prof)
        ensure_profile_vector(n.profile, emb)
        nodes.append(n)

    qv = emb.embed(["What is the total revenue for each product"])[0]
    kept = {n.id for n in coarse_prune(qv, nodes, emb, floor_frac=0.5)}
    assert "azure-deals" in kept, (
        "#321: the sales store that can answer was pruned by the coarse gate - its BU "
        f"centroid was diluted by unrelated siblings. kept={sorted(kept)}")


def test_route_cache_hits_and_respects_users():
    cat = _catalog()
    clock = [0.0]
    svc = router.RouterQueryService(cat, IDENTITY, _router_embedder(),
                                    cache_ttl_s=60.0, clock=lambda: clock[0])
    d1 = svc.route("u1", "parental leave holidays")
    assert svc.cache_stats()["hits"] == 0, svc.cache_stats()
    d2 = svc.route("u1", "parental leave holidays")
    assert svc.cache_stats()["hits"] == 1, svc.cache_stats()
    assert d2.to_dict() == d1.to_dict()
    svc.route("u2", "parental leave holidays")     # same principals -> may share; user
    # key is the PRINCIPAL SET, not the oid — u1/u2 both ["staff", oid]? expand_groups
    # includes the oid itself, so their principal sets differ -> no cross-user reuse.
    assert svc.cache_stats()["hits"] == 1, svc.cache_stats()
    clock[0] = 61.0                                # TTL expiry
    svc.route("u1", "parental leave holidays")
    assert svc.cache_stats()["hits"] == 1, svc.cache_stats()


def test_route_cache_invalidated_by_recompose():
    cat = _catalog()
    svc = router.RouterQueryService(cat, IDENTITY, _router_embedder())
    svc.route("u1", "contracts compliance")
    cat.register(router.CatalogNode("late-bu", router.BUSINESS_UNIT, "acme", acl=["staff"]))
    svc.route("u1", "contracts compliance")        # revision changed -> re-route, no hit
    assert svc.cache_stats()["hits"] == 0, svc.cache_stats()


def test_dispatch_budget_caps_and_discloses():
    cat = _catalog()
    # wide fan-out: no dominant winner + no floor -> every store of the BU ties
    svc = router.RouterQueryService(cat, IDENTITY, _router_embedder(),
                                    margin=1.0, floor_frac=0.0, fanout_cap=3,
                                    max_dispatches=2)
    res = svc.ask("u1", "parental leave holidays versus revenue invoices tax",
                  ExtractiveLlm())
    dispatched = [o for o in res.outcomes if o["status"] != "budget"]
    capped = [o for o in res.outcomes if o["status"] == "budget"]
    assert len(dispatched) <= 2, res.outcomes
    assert capped, "over-budget stores must appear as outcomes, not vanish"
    assert "budget" in res.disclosure, res.disclosure


def test_profile_vectors_warm_at_service_init():
    cat = _catalog()
    router.RouterQueryService(cat, IDENTITY, _router_embedder())
    missing = [n.id for n in cat.stores() if n.profile.profile_vector is None]
    assert missing == [], missing


def bench_10bu():
    cat = _catalog()
    svc = router.RouterQueryService(cat, IDENTITY, _router_embedder())
    qs = [f"question about {_WORDS[bu].split()[0]}" for bu in _BUS] * 5
    t0 = time.perf_counter()
    for q in qs:
        svc.route("u1", q)
    cold_warm = time.perf_counter() - t0
    stats = svc.cache_stats()
    print(f"  BENCH 10-BU/30-store: {len(qs)} routes in {cold_warm*1000:.1f}ms "
          f"(cache hits {stats['hits']}/{len(qs)})")
    assert stats["hits"] >= len(qs) - len(set(qs)), stats   # every repeat = a hit


def main():
    print("Phase E E8 scale & governance self-test:")
    test_catalog_revision_bumps()
    test_coarse_prune_keeps_routing_correct()
    print("  PASS  revision counter + coarse BU prune stays correct at 10 BUs")
    test_coarse_prune_survives_a_heterogeneous_bu()
    print("  PASS  coarse gate keeps a BU's one matching store despite unrelated siblings (#321)")
    test_route_cache_hits_and_respects_users()
    test_route_cache_invalidated_by_recompose()
    print("  PASS  route cache: hit / per-principal isolation / TTL / revision invalidation")
    test_dispatch_budget_caps_and_discloses()
    print("  PASS  dispatch budget caps compound fan-out + disclosed (no silent caps)")
    test_profile_vectors_warm_at_service_init()
    print("  PASS  profile vectors warm at init")
    bench_10bu()
    print("\nE8 SCALE & GOVERNANCE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
