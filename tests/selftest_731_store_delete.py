"""#731 - DELETE /router/stores/{id}: a deletion is durable, cheap, isolated and honest.

THE DEFECT (owner hit it live on prod): deleting a canvas node only mutated the client
draft. The stored manifest row - written exclusively by _persisting_compose - still carried
the store, so navigating to /admin and back re-hydrated the canvas from the row, resurrected
every deleted node, and then boot's own composeUp() RE-COMMITTED the resurrected set. The
canvas was silently a draft/commit model and nothing said so.

THE FIX is a real server-side primitive, and this file pins its contract:

- DURABLE FIRST: the stored row is edited before the live workspace. A store outage means
  503 "NOT deleted" with NOTHING changed anywhere - fail closed, mirroring compose's own
  honesty. If the process dies between the row write and the live edit, the next rebuild
  converges on the row.
- CHEAP: the live workspace is edited only IF WARM (`get_if_warm`) - a delete must NEVER
  rebuild a cold workspace, because a rebuild re-fires connector crawls (the #536 pack is
  40MB/>1h). Catalog surgery + `service = None` (compose's own invalidation), no store
  rebuilds, broker untouched.
- NON-DESTRUCTIVE (the owner's revertibility condition): documents, ingest jobs, secret
  handles, vault entries and grants are untouched - re-adding the store restores it
  wholesale, and the response returns the removed manifest entry so the client's Undo can
  hold it.
- EMPTY IS A STATE: deleting the last store leaves `{tenant, stores: []}` - an object, not
  an absent row - because hydration must distinguish "authoritatively empty" from "never
  composed" (the latent half of #731: `stores: []` used to read as no-manifest and fall
  back to localStorage, so delete-ALL could never persist).

    PYTHONPATH=src python3 tests/selftest_731_store_delete.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
for _k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.edition import build_edition  # noqa: E402
from dbsearch.server.manifest_store import (  # noqa: E402
    InMemoryManifestStore, ManifestStoreUnavailable)
from dbsearch.server import router_api  # noqa: E402

_TABLE = {"sales": {"columns": ["region", "amount"], "rows": [["emea", 100]]}}
_TABLE2 = {"costs": {"columns": ["route", "cost"], "rows": [["sin-hkg", 8000]]}}


def _manifest(*stores):
    return {"tenant": "acme", "stores": list(stores)}


def _store_entry(sid, table, acl=("oid-a",)):
    return {"id": sid, "kind": "csv", "business_unit": "eng", "acl": list(acl),
            "config": {"tables": table}}


def _app(manifest_store=None):
    def current_user(request: Request) -> str:
        return request.headers["X-Test-User"]
    app = FastAPI()
    router = router_api.build_router_api(
        build_edition(), current_user, manifest_store=manifest_store,
        force_per_user_workspaces=True)
    app.include_router(router)
    return app, router


def _hdr(u="oid-a"):
    return {"X-Test-User": u}


def _compose_two(client, hdr=None):
    m = _manifest(_store_entry("csv-1", _TABLE), _store_entry("csv-2", _TABLE2))
    r = client.post("/router/compose", json={"manifest": m}, headers=hdr or _hdr())
    assert r.status_code == 200 and len(r.json()["stores"]) == 2, r.text
    return m


class _Recorder(InMemoryManifestStore):
    """Pins the non-destructive contract: a delete may only get/put - never delete a row."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def get(self, key):
        self.calls.append("get")
        return super().get(key)

    def put(self, key, manifest):
        self.calls.append("put")
        return super().put(key, manifest)

    def delete(self, key):
        self.calls.append("delete")
        raise AssertionError("a store DELETE must never delete the whole workspace row")


def test_delete_updates_row_and_catalog_without_recompose():
    store = _Recorder()
    client = TestClient(_app(store)[0])
    _compose_two(client)
    r = client.delete("/router/stores/csv-1", headers=_hdr())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] is True
    assert body["entry"] and body["entry"]["id"] == "csv-1", (
        f"the removed entry must ride back for the client's Undo: {body}")
    m = client.get("/router/manifest", headers=_hdr()).json()["manifest"]
    assert [s["id"] for s in m["stores"]] == ["csv-2"], m
    cat = client.get("/router/catalog", headers=_hdr()).text
    assert "csv-1" not in cat and "csv-2" in cat, (
        f"the live catalog does not reflect the delete without a recompose: {cat[:300]}")
    assert "delete" not in store.calls, "the whole row was deleted"
    print("  PASS  delete edits the row and the live catalog, no recompose")


def test_deleting_the_last_store_leaves_an_empty_manifest_not_none():
    client = TestClient(_app(InMemoryManifestStore())[0])
    _compose_two(client)
    client.delete("/router/stores/csv-1", headers=_hdr())
    client.delete("/router/stores/csv-2", headers=_hdr())
    m = client.get("/router/manifest", headers=_hdr()).json()["manifest"]
    assert m is not None and m.get("stores") == [], (
        f"delete-all must leave AUTHORITATIVE empty, not an absent row: {m!r}")
    cat = client.get("/router/catalog", headers=_hdr()).text
    assert "csv-1" not in cat and "csv-2" not in cat
    print("  PASS  delete-all leaves {tenant, stores: []} - empty is a state")


def test_idempotent_and_isolated():
    client = TestClient(_app(InMemoryManifestStore())[0])
    _compose_two(client)
    r = client.delete("/router/stores/nope", headers=_hdr())
    assert r.status_code == 200 and r.json()["deleted"] is False, r.text
    client.delete("/router/stores/csv-1", headers=_hdr())
    r2 = client.delete("/router/stores/csv-1", headers=_hdr())
    assert r2.status_code == 200 and r2.json()["deleted"] is False, (
        f"repeat delete must be an idempotent no-op: {r2.text}")
    # oid-b deleting oid-a's store id touches NOTHING of oid-a's
    r3 = client.delete("/router/stores/csv-2", headers=_hdr("oid-b"))
    assert r3.status_code == 200 and r3.json()["deleted"] is False, r3.text
    m = client.get("/router/manifest", headers=_hdr()).json()["manifest"]
    assert [s["id"] for s in m["stores"]] == ["csv-2"], (
        f"another user's delete reached oid-a's workspace: {m}")
    print("  PASS  idempotent, and another user's delete touches nothing")


def test_a_cold_key_is_never_rebuilt():
    """The cheapness clause: composing happened in app ONE; deleting through app TWO (cold
    pool) must edit the row WITHOUT materializing a workspace - a rebuild would re-fire
    connector crawls, which is exactly the cost the plan forbids on a delete."""
    store = InMemoryManifestStore()
    client1 = TestClient(_app(store)[0])
    _compose_two(client1)
    app2, router2 = _app(store)
    client2 = TestClient(app2)
    r = client2.delete("/router/stores/csv-1", headers=_hdr())
    assert r.status_code == 200 and r.json()["deleted"] is True, r.text
    m = store.get("oid-a")
    assert [s["id"] for s in m["stores"]] == ["csv-2"]
    # the introspection seam build_router_api exposes for exactly this assertion
    pool = router2._workspace_pool
    assert pool.warm_keys() == [], (
        f"a DELETE on a cold key materialized a workspace: {pool.warm_keys()}")
    print("  PASS  a cold-key delete edits the row and rebuilds nothing")


def test_store_outage_fails_closed():
    class _Broken(InMemoryManifestStore):
        def put(self, key, manifest):
            raise ManifestStoreUnavailable("down")

    broken = _Broken()
    client = TestClient(_app(broken)[0])
    _compose_two_direct(broken)
    r = client.delete("/router/stores/csv-1", headers=_hdr())
    assert r.status_code == 503, r.text
    assert "NOT deleted" in r.json()["detail"], r.text
    m = broken.get("oid-a")
    assert [s["id"] for s in m["stores"]] == ["csv-1", "csv-2"], (
        "the row changed despite the 503")
    print("  PASS  a store outage is a 503 with nothing changed (fail closed)")


def _compose_two_direct(store):
    """Seed the stored row without composing (the broken store refuses put via HTTP)."""
    InMemoryManifestStore.put(store, "oid-a",
                              _manifest(_store_entry("csv-1", _TABLE),
                                        _store_entry("csv-2", _TABLE2)))


def test_route_omits_the_deleted_store():
    """The service-invalidation clause, read where the user meets it: the route advisor
    (LLM-free) must stop offering a deleted store."""
    client = TestClient(_app(InMemoryManifestStore())[0])
    _compose_two(client)
    r0 = client.post("/router/route", json={"question": "freight cost per route"},
                     headers=_hdr())
    assert "csv-2" in r0.text, f"the control leg is vacuous - csv-2 never routed: {r0.text[:300]}"
    client.delete("/router/stores/csv-2", headers=_hdr())
    r1 = client.post("/router/route", json={"question": "freight cost per route"},
                     headers=_hdr())
    assert "csv-2" not in r1.text, (
        f"the route advisor still offers the deleted store: {r1.text[:300]}")
    print("  PASS  the route advisor forgets a deleted store immediately")


if __name__ == "__main__":
    test_delete_updates_row_and_catalog_without_recompose()
    test_deleting_the_last_store_leaves_an_empty_manifest_not_none()
    test_idempotent_and_isolated()
    test_a_cold_key_is_never_rebuilt()
    test_store_outage_fails_closed()
    test_route_omits_the_deleted_store()
    print("\nSTORE DELETE SELF-TEST PASSED.")
