"""Phase E E7 — advisor UX seams: per-store why, manual override, visible catalog tree.

The advisor is transparency over the SAME gated routing: every candidate carries a
human 'why this store' explanation; a user may manually pin a store — but only a
VISIBLE one (an override naming an invisible/nonexistent store gets one generic
fallback, indistinguishable either way — scenario G); and the catalog read surface
returns only the caller-visible tree with freshness.

Run: python3 tests/selftest_router_e7.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import ExtractiveLlm, HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch import router  # noqa: E402

SPEC = {
    "tenant": "acme",
    "stores": [
        {"id": "hr-wiki", "kind": "local", "business_unit": "hr", "acl": ["hr-staff"],
         "title": "HR Wiki", "description": "human resources parental leave holidays onboarding benefits",
         "config": {"seed": [{"external_id": "hb", "title": "Handbook", "uri": "u1",
                              "acl": ["hr-staff"], "text": "parental leave lasts sixteen weeks"}],
                    "user_groups": {"alice": ["hr-staff"], "carol": ["hr-staff", "fin-staff"]}}},
        {"id": "fin-ledger", "kind": "local", "business_unit": "finance", "acl": ["fin-staff"],
         "title": "Finance Ledger", "description": "revenue invoices tax numbers ledger accounting",
         "config": {"seed": [{"external_id": "q3", "title": "Q3 Ledger", "uri": "u2",
                              "acl": ["fin-staff"], "text": "quarterly revenue was nine million"}],
                    "user_groups": {"eve": ["fin-staff"], "carol": ["hr-staff", "fin-staff"]}}},
    ],
}


def _svc():
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    cat = router.load_manifest(SPEC, registry=reg)
    identity = InMemoryIdentity({"alice": ["hr-staff"], "eve": ["fin-staff"],
                                 "carol": ["hr-staff", "fin-staff"]})
    return router.RouterQueryService(cat, identity, HashingEmbedding()), cat, identity


def test_candidates_carry_why():
    svc, _, _ = _svc()
    d = svc.route("carol", "what is our parental leave policy")
    assert d.candidates, d.to_dict()
    top = d.candidates[0]
    assert top.why and "match" in top.why, top.why
    assert "leave" in top.why or "parental" in top.why, top.why   # shared-term transparency
    assert "why" in top.to_dict(), top.to_dict()


def test_manual_override_visible_store():
    svc, _, _ = _svc()
    d = svc.route("carol", "what is our parental leave policy",
                  store_override="fin-ledger")
    assert d.method == "manual", d.to_dict()
    assert [s.store_id for s in d.stores] == ["fin-ledger"], d.to_dict()
    assert "pinned" in d.reason, d.reason
    # candidates still listed for transparency (the advisor shows what WOULD have won)
    assert {c.store_id for c in d.candidates} == {"hr-wiki", "fin-ledger"}, d.to_dict()


def test_manual_override_never_confirms_invisible_store():
    svc, _, _ = _svc()
    invisible = svc.route("alice", "revenue", store_override="fin-ledger")
    nonexistent = svc.route("alice", "revenue", store_override="no-such-store")
    for d in (invisible, nonexistent):
        assert d.method == "fallback", d.to_dict()
        assert d.stores == [], d.to_dict()
    # scenario G: the two must be INDISTINGUISHABLE, and never name the store
    assert invisible.reason == nonexistent.reason, (invisible.reason, nonexistent.reason)
    assert "fin-ledger" not in repr(invisible.to_dict()), "override leaked store existence"


def test_ask_with_override_executes_only_that_store():
    svc, _, _ = _svc()
    res = svc.ask("carol", "what changed this quarter", ExtractiveLlm(),
                  store_override="fin-ledger")
    assert {o["store_id"] for o in res.outcomes} == {"fin-ledger"}, res.outcomes
    assert res.routing["method"] == "manual", res.routing


def test_override_beats_compound_decomposition():
    svc, _, _ = _svc()
    d = svc.route("carol", "parental leave versus revenue invoices",
                  store_override="hr-wiki")
    assert d.method == "manual" and d.sub_queries == [], d.to_dict()


def test_visible_tree_is_caller_trimmed():
    _, cat, identity = _svc()
    carol_tree = cat.visible_tree(identity.expand_groups("carol"))
    assert carol_tree["tenant"] == "acme", carol_tree
    bus = {b["id"] for b in carol_tree["business_units"]}
    assert bus == {"hr", "finance"}, carol_tree
    fin = next(b for b in carol_tree["business_units"] if b["id"] == "finance")
    store = fin["sources"][0]["stores"][0]
    assert store["store_id"] == "fin-ledger" and "freshness" in store, store

    alice_tree = cat.visible_tree(identity.expand_groups("alice"))
    assert "fin-ledger" not in repr(alice_tree), "catalog tree leaked an invisible store"
    assert {b["id"] for b in alice_tree["business_units"]} == {"hr"}, alice_tree


def main():
    print("Phase E E7 advisor self-test:")
    test_candidates_carry_why()
    print("  PASS  per-store why (match + shared terms)")
    test_manual_override_visible_store()
    test_manual_override_never_confirms_invisible_store()
    test_ask_with_override_executes_only_that_store()
    test_override_beats_compound_decomposition()
    print("  PASS  manual override: pin visible / scenario-G fallback / single-store ask / beats compound")
    test_visible_tree_is_caller_trimmed()
    print("  PASS  caller-trimmed catalog tree with freshness")
    print("\nE7 ADVISOR SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
