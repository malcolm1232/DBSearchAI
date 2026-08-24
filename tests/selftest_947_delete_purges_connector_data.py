"""#947 - deleting a CONNECTOR source removes its ingested data, and a re-add re-crawls.

The owner, 260824: "make sure 'delete button' removes from db." Today it does not, by design:
#731 made delete NON-DESTRUCTIVE so Undo restores a store wholesale, and #923 made only the
UPLOAD node destructive (its content lives only in DBSearch). A connector node delete left
every ingested chunk in the index - ACL'd to the deleter, "deleted" that isn't - and a re-add
REUSED the built store (#944), so a folder that changed while the source was gone served stale
content.

The recommendation the owner accepted: for a connector, DELETE PURGES. It is safe here for the
one reason an upload does not share - the source of truth is the external folder, so a re-add
re-crawls and nothing is lost. So Undo becomes re-add-and-re-crawl, not restore-from-a-corpse.

This file pins both halves:
  - the provider PURGE mechanic (unit): the chunks leave the index, the built store and its
    descriptor/cursor are forgotten, and a subsequent build_as REBUILDS rather than reuses;
  - the endpoint behaviour (integration): DELETE /router/stores/{id} on a connector store
    purges, so the store's documents are gone and a re-add fires a FRESH crawl.

    PYTHONPATH=src python3 tests/selftest_947_delete_purges_connector_data.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
os.environ["DBSEARCH_RATE_LIMIT"] = "0"
os.environ.pop("USERS_FILE", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.ports.base import as_read_scope  # noqa: E402
from dbsearch.router import ConnectorStoreProvider, folder_connector_factory  # noqa: E402

ROOT = None


def _folder(n=3, root=None):
    root = root or Path(tempfile.mkdtemp(prefix="dbse-947-"))
    (root / "all-staff").mkdir(exist_ok=True)
    for i in range(n):
        (root / "all-staff" / f"doc{i}.txt").write_text(f"document number {i} about ospreys")
    return root


def _cfg(path, sid="legal-archive", acl=("all-staff",)):
    return {"id": sid, "kind": "folder", "mode": "index", "business_unit": "legal",
            "acl": list(acl), "title": "Legal archive", "description": "archive",
            "config": {"path": str(path)}, "path": str(path)}


def _await(provider, sid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            d = provider.sources.get(sid)
        except KeyError:
            return None
        if d.status not in ("syncing",):
            return d
        time.sleep(0.05)
    return provider.sources.get(sid)


def _index_docs(provider, sid):
    """Every document the store's OWN index holds, untrimmed - the ground truth a purge must
    empty. Returns [] if the store is not built."""
    pipe = provider._pipes.get(sid)
    if pipe is None:
        return []
    _obj, _emb, index = pipe
    return [r.doc_external_id for r in index.list_doc_acls(as_read_scope(sid))]


# --------------------------------------------------------------------- the provider mechanic
def test_purge_empties_the_index_and_forgets_the_built_store():
    p = ConnectorStoreProvider("folder", folder_connector_factory)
    root = _folder()
    p.build_as(_cfg(root))
    _await(p, "legal-archive")
    assert _index_docs(p, "legal-archive"), "fixture never ingested - nothing to purge"
    assert p.owns("legal-archive")

    held = p.purge("legal-archive")
    assert held is True, "purge reported it held nothing, on a store it had just built"
    assert _index_docs(p, "legal-archive") == [], "chunks survived the purge - 'deleted' that isn't"
    assert not p.owns("legal-archive"), "the descriptor survived the purge"
    assert "legal-archive" not in p._stores, "the built store survived the purge"
    assert "legal-archive" not in p._pipes, "the index pipe survived the purge"
    assert "legal-archive" not in p._recipes, "the #944 recipe survived, so a re-add would reuse"
    print("  PASS  purge empties the index and forgets the built store, descriptor and recipe")


def test_purge_is_idempotent_on_an_unknown_store():
    p = ConnectorStoreProvider("folder", folder_connector_factory)
    assert p.purge("never-built") is False, "purging an unknown id must be a no-op, not an error"
    print("  PASS  purge of an unknown store is a harmless no-op")


def test_a_re_add_after_purge_rebuilds_and_recrawls_rather_than_reusing():
    """The #944 residual this closes: without the purge, build_as would hand back the stale
    store. After a purge, the same id is a FRESH build - a new store object and a new crawl -
    so a folder that changed while deleted is picked up."""
    p = ConnectorStoreProvider("folder", folder_connector_factory)
    root = _folder(n=3)
    first = p.build_as(_cfg(root))
    _await(p, "legal-archive")
    p.purge("legal-archive")

    # the folder GREW while the source was gone - the re-add must see all of it
    _folder(n=5, root=root)
    second = p.build_as(_cfg(root))
    _await(p, "legal-archive")
    assert second is not first, "build_as reused a store that was purged"
    docs = _index_docs(p, "legal-archive")
    assert len(docs) == 5, f"the re-add did not re-crawl the changed folder: {len(docs)} docs"
    print("  PASS  a re-add after purge rebuilds and re-crawls the (changed) source")


# --------------------------------------------------------------------- the endpoint behaviour
def _client():
    from fastapi.testclient import TestClient
    from dbsearch.server.app import app
    return TestClient(app)


ALICE = {"X-DBSearch-User": "alice"}


def _await_ingest(client, resp_json, timeout=60):
    jobs = [j["job_id"] for j in resp_json.get("ingesting", [])]
    if resp_json.get("job_id"):
        jobs.append(resp_json["job_id"])
    for job_id in jobs:
        deadline = time.time() + timeout
        while time.time() < deadline:
            b = client.get(f"/router/jobs/{job_id}", headers=ALICE).json()
            if b["status"] in ("succeeded", "failed"):
                assert b["status"] == "succeeded", b
                break
            time.sleep(0.05)


def _compose(client, root, sid="legal-archive"):
    demo = client.get("/router/demo", headers=ALICE).json()["manifest"]
    demo["stores"] = [s for s in demo["stores"] if s.get("id") != sid]
    demo["stores"].append({
        "id": sid, "kind": "folder", "mode": "index", "business_unit": "legal",
        "acl": ["all-staff"], "title": "Legal archive", "description": "osprey archive",
        "config": {"path": str(root)}})
    r = client.post("/router/compose", headers=ALICE, json={"manifest": demo})
    assert r.status_code == 200, r.text
    _await_ingest(client, r.json())
    return demo


def test_delete_endpoint_purges_and_a_re_add_starts_a_fresh_crawl():
    client = _client()
    root = _folder(n=3)
    _compose(client, root)
    # the store shows its documents before delete
    before = client.get("/router/stores/legal-archive/documents", headers=ALICE)
    assert before.status_code == 200 and before.json()["doc_count"] == 3, before.text

    d = client.delete("/router/stores/legal-archive", headers=ALICE)
    assert d.status_code == 200 and d.json()["deleted"] is True, d.text

    # the store is gone from the caller's catalog (404, never 403 - the existence-probe rule)
    after = client.get("/router/stores/legal-archive/documents", headers=ALICE)
    assert after.status_code == 404, after.text

    # a re-add re-crawls: the folder grew while it was gone, and the fresh crawl must see it
    _folder(n=5, root=root)
    demo = _compose(client, root)
    again = client.get("/router/stores/legal-archive/documents", headers=ALICE)
    assert again.status_code == 200, again.text
    assert again.json()["doc_count"] == 5, (
        f"the re-add reused stale content instead of re-crawling: {again.json()}")
    print("  PASS  DELETE purges the connector store; a re-add fires a fresh crawl (3 -> gone -> 5)")


if __name__ == "__main__":
    failures = []
    for name in ["test_purge_empties_the_index_and_forgets_the_built_store",
                 "test_purge_is_idempotent_on_an_unknown_store",
                 "test_a_re_add_after_purge_rebuilds_and_recrawls_rather_than_reusing",
                 "test_delete_endpoint_purges_and_a_re_add_starts_a_fresh_crawl"]:
        try:
            globals()[name]()
        except AssertionError as e:
            failures.append(name); print(f"FAIL  {name}\n      {e}")
        except Exception as e:
            failures.append(name); print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
