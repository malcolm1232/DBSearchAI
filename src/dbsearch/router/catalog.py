"""Semantic Catalog — the hierarchical registry of stores (Phase E, card #98).

Tenant -> BusinessUnit -> Source -> Store. Each node carries an ACL of principal oids
that may KNOW IT EXISTS (gate #1). visible_stores() is that gate: it returns only the
store leaves whose ENTIRE ancestor path is visible to the caller, so a router can never
even consider — nor name in an explanation — a store the user isn't cleared to see
(design §8, scenario G).

Visibility is HEREDITARY: a store under a business unit you can't see is invisible, even
if the store's own ACL would admit you. That closes the 'child leaks parent' existence
probe.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dbsearch.router.store import StorePort, StoreProfile

TENANT = "tenant"
BUSINESS_UNIT = "business_unit"
SOURCE = "source"
STORE = "store"


@dataclass
class CatalogNode:
    id: str
    kind: str                     # TENANT | BUSINESS_UNIT | SOURCE | STORE
    parent_id: str | None
    acl: list[str] = field(default_factory=list)   # principal oids that may see this node
    profile: StoreProfile | None = None            # STORE leaves only
    store: StorePort | None = None                 # STORE leaves only — the dispatch handle


class StoreCatalog:
    def __init__(self) -> None:
        self._nodes: dict[str, CatalogNode] = {}
        self.revision = 0        # E8: bumped on every register — route-cache invalidation key

    def register(self, node: CatalogNode) -> None:
        if node.parent_id is not None and node.parent_id not in self._nodes:
            raise ValueError(f"parent {node.parent_id!r} not registered before {node.id!r}")
        if node.id in self._nodes:
            # #114: ids share ONE namespace. Silent replacement let a store reusing its
            # BU's id create a parent CYCLE — the visibility gate then spun forever.
            raise ValueError(
                f"catalog id {node.id!r} already registered "
                f"(as {self._nodes[node.id].kind}) — tenant/BU/source/store ids must be unique")
        self._nodes[node.id] = node
        self.revision += 1

    def get(self, node_id: str) -> CatalogNode:
        return self._nodes[node_id]

    def remove(self, store_id: str) -> bool:
        """#731: unregister ONE store node and its dedicated source parent (provisioning
        registers them as `{id}` under `{id}--src`). The BU/tenant scaffold stays - the
        visible tree tolerates a childless BU, and the next compose rebuilds the scaffold
        anyway. Returns whether anything was removed, so the delete endpoint can answer
        idempotently."""
        node = self._nodes.get(store_id)
        if node is None or node.kind != STORE:
            return False
        del self._nodes[store_id]
        src = self._nodes.get(node.parent_id) if node.parent_id else None
        if src is not None and src.kind == SOURCE and not self.children(src.id):
            del self._nodes[src.id]
        return True

    def children(self, node_id: str) -> list[CatalogNode]:
        return [n for n in self._nodes.values() if n.parent_id == node_id]

    def stores(self) -> list[CatalogNode]:
        return [n for n in self._nodes.values() if n.kind == STORE]

    # --- gate #1: hereditary catalog visibility trim ---
    def _path_visible(self, node: CatalogNode, principals: set[str]) -> bool:
        cur: CatalogNode | None = node
        seen: set[str] = set()
        while cur is not None:
            if cur.id in seen:
                return False   # #114 defense in depth: a cyclic path DENIES, never spins
            seen.add(cur.id)
            if not (set(cur.acl) & principals):
                return False
            cur = self._nodes.get(cur.parent_id) if cur.parent_id else None
        return True

    def visible_stores(self, principals: list[str]) -> list[CatalogNode]:
        pset = set(principals)
        return [n for n in self.stores() if self._path_visible(n, pset)]

    def visible_tree(self, principals: list[str]) -> dict:
        """E7 admin/advisor read surface: the caller's VISIBLE catalog as a nested
        tenant → business-unit → source → store tree (gate #1 — built purely from
        visible_stores(), so it can never name a node the caller isn't cleared to see).
        Store entries carry the profile summary incl. freshness."""
        tenant = next((n for n in self._nodes.values() if n.kind == TENANT), None)
        tree: dict = {"tenant": tenant.id if tenant else "", "business_units": []}
        bus: dict[str, dict] = {}
        for store in self.visible_stores(principals):
            src = self._nodes.get(store.parent_id)
            bu = self._nodes.get(src.parent_id) if src else None
            bu_id = bu.id if bu else ""
            src_id = src.id if src else ""
            bu_entry = bus.setdefault(bu_id, {"id": bu_id, "sources": {}})
            src_entry = bu_entry["sources"].setdefault(src_id, {"id": src_id, "stores": []})
            p = store.profile
            src_entry["stores"].append({
                "store_id": store.id,
                "title": p.title if p else store.id,
                "kind": p.kind if p else "",
                "capabilities": sorted(p.capabilities) if p else [],
                "business_unit": bu_id,
                "freshness": p.freshness if p else "",
            })
        for bu_entry in bus.values():
            bu_entry["sources"] = list(bu_entry["sources"].values())
            tree["business_units"].append(bu_entry)
        return tree
