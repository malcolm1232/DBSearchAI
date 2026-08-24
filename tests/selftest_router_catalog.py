"""Phase E E1 — catalog + hereditary visibility gate self-test.
Run: python3 tests/selftest_router_catalog.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.catalog import (  # noqa: E402
    CatalogNode, StoreCatalog, TENANT, BUSINESS_UNIT, SOURCE, STORE,
)


def _tree():
    """acme
         hr        (acl hr-staff)      -> hr-sp -> hr-wiki   (acl hr-staff)
         finance   (acl finance-staff) -> fin-sp -> exec-comp (acl exec-only)
       tenant acl = all-staff (everyone in the company).
    """
    c = StoreCatalog()
    c.register(CatalogNode("acme", TENANT, None, acl=["all-staff"]))
    c.register(CatalogNode("hr", BUSINESS_UNIT, "acme", acl=["hr-staff"]))
    c.register(CatalogNode("hr-sp", SOURCE, "hr", acl=["hr-staff"]))
    c.register(CatalogNode("hr-wiki", STORE, "hr-sp", acl=["hr-staff"]))
    c.register(CatalogNode("finance", BUSINESS_UNIT, "acme", acl=["finance-staff"]))
    c.register(CatalogNode("fin-sp", SOURCE, "finance", acl=["finance-staff"]))
    c.register(CatalogNode("exec-comp", STORE, "fin-sp", acl=["exec-only"]))
    return c


def test_register_requires_parent():
    c = StoreCatalog()
    try:
        c.register(CatalogNode("orphan", STORE, "missing", acl=["x"]))
        assert False, "registering under a missing parent must raise"
    except ValueError:
        pass


def test_register_rejects_duplicate_id():
    # #114: ids share ONE namespace. A store reusing its BU's id used to silently
    # REPLACE the BU node, creating a parent cycle -> visible_stores spun forever.
    c = StoreCatalog()
    c.register(CatalogNode("acme", TENANT, None, acl=["all-staff"]))
    c.register(CatalogNode("legal", BUSINESS_UNIT, "acme", acl=["all-staff"]))
    c.register(CatalogNode("legal--src", SOURCE, "legal", acl=["all-staff"]))
    try:
        c.register(CatalogNode("legal", STORE, "legal--src", acl=["all-staff"]))
        assert False, "duplicate id must raise, never silently replace"
    except ValueError as exc:
        assert "legal" in str(exc), exc


def test_path_visible_survives_a_cycle():
    # defense in depth for #114: even if a cycle sneaks in, the gate DENIES, never spins
    c = StoreCatalog()
    c.register(CatalogNode("a", TENANT, None, acl=["g"]))
    c.register(CatalogNode("b", SOURCE, "a", acl=["g"]))
    c.register(CatalogNode("s", STORE, "b", acl=["g"]))
    c._nodes["a"].parent_id = "s"          # force a cycle behind the API
    assert c.visible_stores(["g"]) == [], "a cyclic path must be invisible, not a hang"


def test_structure():
    c = _tree()
    assert {n.id for n in c.stores()} == {"hr-wiki", "exec-comp"}, c.stores()
    assert {n.id for n in c.children("acme")} == {"hr", "finance"}, c.children("acme")


def test_visible_hereditary():
    c = _tree()
    # alice: all-staff + hr-staff -> sees hr-wiki, not exec-comp (no finance/exec)
    alice = ["alice", "all-staff", "hr-staff"]
    assert {n.id for n in c.visible_stores(alice)} == {"hr-wiki"}, c.visible_stores(alice)
    # eve: all-staff + finance-staff + exec-only -> sees exec-comp only
    eve = ["eve", "all-staff", "finance-staff", "exec-only"]
    assert {n.id for n in c.visible_stores(eve)} == {"exec-comp"}, c.visible_stores(eve)


def test_parent_restricts_child():
    """A user cleared for the STORE's own acl but NOT an ancestor's sees nothing —
    the store's existence must not leak through a business unit the user can't see
    (design §8, scenario G)."""
    c = _tree()
    # bob has hr-staff (would satisfy hr-wiki's own acl) but NOT all-staff (tenant acl).
    bob = ["bob", "hr-staff"]
    assert c.visible_stores(bob) == [], c.visible_stores(bob)


def main():
    print("Phase E E1 catalog self-test:")
    test_register_requires_parent()
    test_register_rejects_duplicate_id()
    test_path_visible_survives_a_cycle()
    test_structure()
    test_visible_hereditary()
    test_parent_restricts_child()
    print("  PASS  register / duplicate-id + cycle guard (#114) / structure / "
          "hereditary visibility / parent-restricts-child")
    print("\nE1 CATALOG SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
