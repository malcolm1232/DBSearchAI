"""Phase E E2 — RouterQueryService end-to-end over an E1 catalog.
Run: python3 tests/selftest_router_route.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch import router  # noqa: E402

# Two BUs, distinct vocabularies so the lexical embedder routes deterministically.
SPEC = {
    "tenant": "acme",
    "stores": [
        {"id": "hr-wiki", "kind": "local", "business_unit": "hr", "acl": ["hr-staff"],
         "title": "HR Wiki", "description": "human resources parental leave holidays onboarding benefits",
         "config": {"seed": [{"external_id": "hb", "title": "Handbook", "uri": "u1",
                              "acl": ["hr-staff"], "text": "parental leave holidays onboarding"}],
                    "user_groups": {"alice": ["hr-staff"], "carol": ["hr-staff", "fin-staff"]}}},
        {"id": "fin-ledger", "kind": "local", "business_unit": "finance", "acl": ["fin-staff"],
         "title": "Finance Ledger", "description": "revenue invoices tax numbers ledger accounting",
         "config": {"seed": [{"external_id": "q3", "title": "Q3 Ledger", "uri": "u2",
                              "acl": ["fin-staff"], "text": "revenue invoices tax"}],
                    "user_groups": {"eve": ["fin-staff"], "carol": ["hr-staff", "fin-staff"]}}},
    ],
}


def _svc():
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    cat = router.load_manifest(SPEC, registry=reg)
    # A single identity that knows every demo user's groups (the router's IdentityPort).
    identity = InMemoryIdentity({"alice": ["hr-staff"], "eve": ["fin-staff"],
                                 "carol": ["hr-staff", "fin-staff"], "mallory": []})
    return router.RouterQueryService(cat, identity, HashingEmbedding()), cat


def test_topical_query_routes_to_one_store():
    svc, _ = _svc()
    d = svc.route("carol", "what is our parental leave policy")
    assert d.query_type == "semantic", d
    assert [s.store_id for s in d.stores] == ["hr-wiki"], d.to_dict()
    assert "hr-wiki" in d.reason or "HR Wiki" in d.reason, d.reason


def test_finance_query_routes_to_finance():
    svc, _ = _svc()
    d = svc.route("carol", "show me revenue and invoices ledger")
    assert d.stores[0].store_id == "fin-ledger", d.to_dict()


def test_visibility_limits_candidates():
    """alice (hr only) must never see fin-ledger as a candidate — gate #1 in routing."""
    svc, _ = _svc()
    d = svc.route("alice", "revenue invoices tax")   # finance-flavoured query
    cand_ids = {c.store_id for c in d.candidates}
    assert "fin-ledger" not in cand_ids, d.to_dict()
    assert cand_ids == {"hr-wiki"}, d.to_dict()


def test_no_access_is_fallback():
    svc, _ = _svc()
    d = svc.route("mallory", "anything")
    assert d.stores == [] and d.method == "fallback", d.to_dict()
    assert "no accessible store" in d.reason, d.reason


def test_llm_tiebreak_only_over_visible():
    """When a tiebreak is injected, it may only choose among the visible candidates."""
    reg = router.ProviderRegistry(); reg.register(router.LocalIndexProvider())
    cat = router.load_manifest(SPEC, registry=reg)
    identity = InMemoryIdentity({"carol": ["hr-staff", "fin-staff"]})
    seen = {}
    def tb(ids):
        seen["ids"] = ids
        return ids[:1]
    # margin=1 forces ambiguity; floor_frac=0 keeps BOTH visible stores in the pool so
    # the tiebreak actually has a choice to make.
    svc = router.RouterQueryService(cat, identity, HashingEmbedding(),
                                    tiebreak=tb, margin=1.0, floor_frac=0.0)
    d = svc.route("carol", "revenue invoices")
    assert set(seen["ids"]) <= {"hr-wiki", "fin-ledger"}, seen
    assert d.method == "llm", d.to_dict()
    assert len(d.stores) == 1, d.to_dict()   # tiebreak narrowed to its top pick


def main():
    print("Phase E E2 route self-test:")
    test_topical_query_routes_to_one_store()
    test_finance_query_routes_to_finance()
    test_visibility_limits_candidates()
    test_no_access_is_fallback()
    test_llm_tiebreak_only_over_visible()
    print("  PASS  topical single / finance route / visibility-limited candidates / "
          "no-access fallback / tiebreak over visible")
    print("\nE2 ROUTE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
