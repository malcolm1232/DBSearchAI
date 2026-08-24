"""Phase E E1 — manifest loader self-test ('compose for databases').
Run: python3 tests/selftest_router_manifest.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.provider import ProviderRegistry  # noqa: E402
from dbsearch.router.providers.local import LocalIndexProvider  # noqa: E402
from dbsearch.router.provisioning import load_manifest, resolve_env  # noqa: E402

SPEC = {
    "tenant": "acme",
    "stores": [
        {"id": "hr-wiki", "kind": "local", "business_unit": "hr", "acl": ["hr-staff"],
         "config": {"seed": [{"external_id": "hb", "title": "Handbook", "uri": "u",
                              "acl": ["hr-staff"], "text": "parental leave"}],
                    "user_groups": {"alice": ["hr-staff"]}}},
        {"id": "sales-deck", "kind": "local", "business_unit": "sales", "acl": ["sales-staff"],
         "config": {"seed": [{"external_id": "d1", "title": "Deck", "uri": "u",
                              "acl": ["sales-staff"], "text": "pipeline revenue"}],
                    "user_groups": {"sam": ["sales-staff"]}}},
    ],
}


def _registry():
    r = ProviderRegistry()
    r.register(LocalIndexProvider())
    return r


def test_resolve_env():
    got = resolve_env({"k": "${MYVAR}", "n": 1}, env={"MYVAR": "secret"})
    assert got == {"k": "secret", "n": 1}, got
    try:
        resolve_env("${MISSING}", env={})
        assert False, "missing env must raise"
    except KeyError:
        pass


def test_loads_stores():
    cat = load_manifest(SPEC, registry=_registry())
    assert {n.id for n in cat.stores()} == {"hr-wiki", "sales-deck"}, cat.stores()
    hr = cat.get("hr-wiki")
    assert hr.store is not None and hr.profile is not None, hr


def test_visibility_after_load():
    cat = load_manifest(SPEC, registry=_registry())
    alice = ["alice", "hr-staff"]
    assert {n.id for n in cat.visible_stores(alice)} == {"hr-wiki"}, cat.visible_stores(alice)


def test_retrieve_through_loaded_catalog():
    cat = load_manifest(SPEC, registry=_registry())
    node = cat.get("hr-wiki")
    ev = node.store.retrieve(node.store.authorize("alice"), "parental leave")
    assert any(e.provenance["doc"] == "hb" for e in ev), ev


def test_id_collision_rejected_before_any_build():
    # #114: a store id colliding with its BU id used to create a catalog parent cycle
    # (infinite loop in the visibility gate). The loader now rejects the manifest
    # UP FRONT — no provider may have built (= ingested) anything by then.
    class NeverBuilt(LocalIndexProvider):
        def build(self, config):  # pragma: no cover - must not be reached
            raise AssertionError("build ran despite an id collision")

    r = ProviderRegistry()
    r.register(NeverBuilt())
    spec = {"tenant": "acme",
            "stores": [{"id": "hr", "kind": "local", "business_unit": "hr",
                        "acl": ["hr-staff"], "config": {"seed": [], "user_groups": {}}}]}
    try:
        load_manifest(spec, registry=r)
        assert False, "id collision must raise"
    except ValueError as exc:
        assert "hr" in str(exc), exc


def test_store_build_failure_skips_when_asked_raises_by_default():
    """#107: one miscredentialed cloud store must never take down the catalog — with a
    `skipped` list the failing store is skipped with an honest reason and the REST of
    the manifest composes; without one (strict default) the old raise stands."""
    class Exploding(LocalIndexProvider):
        kind = "boom"

        def build(self, config):
            raise RuntimeError("credentials rejected by cloud")

    r = _registry()
    r.register(Exploding())
    spec = dict(SPEC, stores=SPEC["stores"] + [
        {"id": "bad-cloud", "kind": "boom", "business_unit": "sales", "acl": ["s"],
         "config": {}}])
    skipped: list = []
    cat = load_manifest(spec, registry=r, skipped=skipped)
    assert [s["id"] for s in skipped] == ["bad-cloud"], skipped
    assert "credentials rejected" in skipped[0]["reason"], skipped
    assert {n.id for n in cat.stores()} == {"hr-wiki", "sales-deck"}, cat.stores()
    # strict default: same spec without a skip list still raises
    try:
        load_manifest(spec, registry=r)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def main():
    print("Phase E E1 manifest self-test:")
    test_resolve_env()
    test_loads_stores()
    test_visibility_after_load()
    test_retrieve_through_loaded_catalog()
    test_id_collision_rejected_before_any_build()
    print("  PASS  resolve_env / load stores / visibility / retrieve through catalog / "
          "id-collision guard (#114)")
    test_store_build_failure_skips_when_asked_raises_by_default()
    print("  PASS  per-store build failure -> honest skip (strict raise stays default)")
    print("\nE1 MANIFEST SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
