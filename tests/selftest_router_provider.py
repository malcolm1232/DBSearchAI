"""Phase E E1 — StoreProviderPort + registry + LocalIndexProvider self-test.
Run: python3 tests/selftest_router_provider.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.provider import (  # noqa: E402
    ProviderRegistry, StoreProviderPort, get_provider, register_provider,
)
from dbsearch.router.providers.local import LocalIndexProvider  # noqa: E402
from dbsearch.router.store import INDEXED, SEMANTIC  # noqa: E402

CONFIG = {
    "id": "hr-wiki", "business_unit": "hr", "title": "HR Wiki",
    "description": "hr policies and handbook",
    "seed": [{"external_id": "hb", "title": "Handbook", "uri": "u",
              "acl": ["hr-staff"], "text": "parental leave holidays"}],
    "user_groups": {"alice": ["hr-staff"]},
}


def test_registry_roundtrip():
    reg = ProviderRegistry()
    reg.register(LocalIndexProvider())
    assert isinstance(reg.get("local"), StoreProviderPort), reg.get("local")
    try:
        reg.get("nope")
        assert False, "unknown kind must raise"
    except KeyError:
        pass


def test_default_registry_has_local():
    register_provider(LocalIndexProvider())   # idempotent-friendly
    assert get_provider("local").kind == "local"


def test_probe_returns_profile():
    p = LocalIndexProvider().probe(CONFIG)
    assert p.store_id == "hr-wiki" and p.kind == INDEXED, p
    assert SEMANTIC in p.capabilities and p.business_unit == "hr", p


def test_build_returns_working_store():
    store = LocalIndexProvider().build(CONFIG)
    ev = store.retrieve(store.authorize("alice"), "parental leave")
    assert any(e.provenance["doc"] == "hb" for e in ev), ev


def main():
    print("Phase E E1 provider self-test:")
    test_registry_roundtrip()
    test_default_registry_has_local()
    test_probe_returns_profile()
    test_build_returns_working_store()
    print("  PASS  registry / default-registry / probe / build")
    print("\nE1 PROVIDER SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
