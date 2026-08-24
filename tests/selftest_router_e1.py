"""Phase E E1 — end-to-end integration: a 2-BU catalog assembled from a manifest,
proving the visibility gate (#1) and per-store LAW-2 retrieval through one seam.
Run: python3 tests/selftest_router_e1.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch import router  # noqa: E402

SPEC = {
    "tenant": "acme",
    "stores": [
        {"id": "hr-wiki", "kind": "local", "business_unit": "hr", "acl": ["hr-staff"],
         "config": {"seed": [{"external_id": "hb", "title": "Handbook", "uri": "u1",
                              "acl": ["hr-staff"], "text": "parental leave policy"}],
                    "user_groups": {"alice": ["hr-staff"]}}},
        {"id": "fin-ledger", "kind": "local", "business_unit": "finance", "acl": ["fin-staff"],
         "config": {"seed": [{"external_id": "q3", "title": "Q3 Ledger", "uri": "u2",
                              "acl": ["fin-staff"], "text": "revenue emea numbers"}],
                    "user_groups": {"eve": ["fin-staff"]}}},
    ],
}


def _catalog():
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    return router.load_manifest(SPEC, registry=reg)


def test_exports_present():
    for name in ("Evidence", "StorePort", "StoreCatalog", "IndexedStore",
                 "StoreProviderPort", "load_manifest"):
        assert hasattr(router, name), name


def test_per_user_visibility():
    cat = _catalog()
    assert {n.id for n in cat.visible_stores(["alice", "hr-staff"])} == {"hr-wiki"}
    assert {n.id for n in cat.visible_stores(["eve", "fin-staff"])} == {"fin-ledger"}
    # a user in neither BU sees no store — cannot even learn they exist (gate #1)
    assert cat.visible_stores(["mallory", "all-staff"]) == []


def test_end_to_end_retrieve_only_visible():
    cat = _catalog()
    for node in cat.visible_stores(["alice", "hr-staff"]):
        ev = node.store.retrieve(node.store.authorize("alice"), "parental leave policy")
        assert any(e.provenance["doc"] == "hb" for e in ev), ev
        assert node.profile.business_unit == "hr", node.profile
        assert all(e.business_unit == "hr" for e in ev), ev


def main():
    print("Phase E E1 end-to-end self-test:")
    test_exports_present()
    test_per_user_visibility()
    test_end_to_end_retrieve_only_visible()
    print("  PASS  exports / per-user visibility / end-to-end visible retrieval")
    print("\nE1 END-TO-END SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
