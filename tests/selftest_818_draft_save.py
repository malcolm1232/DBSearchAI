"""#818 - PUT /router/manifest: an added node survives a reload WITHOUT a compose.

THE DEFECT (owner hit it live on prod, 260818): add a node on the canvas, hard refresh,
gone. addNode -> renderAll -> saveCanvas wrote localStorage only; the server row - the
system of record since #368 - was written ONLY by _persisting_compose, and loadLiveUser
rebuilds the canvas exclusively from that row. An added-but-never-composed draft was
durably lost on every reload (the remount's own saveCanvas then destroyed the local copy
too). Proven twice: jsdom remount probe, and prod as the operator (postgres-1 vanished
from canvas, localStorage AND server).

THE FIX is a lightweight guarded row write: PUT /router/manifest validates exactly like
compose (one shared prelude - the #368 invariant is a single guarded write path, so the
two writers cannot drift on the guard) and stores the manifest verbatim, WITHOUT
composing. Drafts ride in the row; boot's composeUp reconciles status as it always has.

The contract, in order of what matters:
- SAME GUARDS AS COMPOSE, SHARED NOT COPIED: plaintext credential -> 400 naming
  store.field, row untouched (LAW 1); the caller-powers guard runs (#423).
- NO COMPOSE IN-REQUEST: a warm catalog is NOT rebuilt or extended by a PUT - drafts are
  not queryable until a real compose. (LAW 4: nothing heavy on this request path.)
- CALLER-SCOPED: user A's PUT can never touch user B's row (LAW 2/5).
- FAIL CLOSED, HONESTLY: store outage -> 503 "NOT saved"; no store configured -> 503,
  never a lying 200 (an empty success hides an outage).

    PYTHONPATH=src python3 tests/selftest_818_draft_save.py
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


def _entry(sid, acl=("oid-a",), **cfg):
    base = {"id": sid, "kind": "csv", "business_unit": "eng", "acl": list(acl),
            "config": cfg or {"tables": _TABLE}}
    return base


def _draft(sid, kind="postgres", acl=("oid-a",)):
    # What addNode actually produces: every field seeded empty, nothing connectable yet.
    return {"id": sid, "kind": kind, "business_unit": "", "acl": list(acl),
            "config": {"description": "", "host": "", "database": ""}}


def _app(manifest_store=None):
    def current_user(request: Request) -> str:
        return request.headers["X-Test-User"]
    app = FastAPI()
    router = router_api.build_router_api(
        build_edition(), current_user, manifest_store=manifest_store,
        force_per_user_workspaces=True)
    app.include_router(router)
    return app


def _hdr(u="oid-a"):
    return {"X-Test-User": u}


def test_put_stores_drafts_and_get_returns_them():
    client = TestClient(_app(InMemoryManifestStore()))
    m = {"tenant": "acme", "stores": [_entry("csv-1"), _draft("postgres-1")]}
    r = client.put("/router/manifest", json={"manifest": m}, headers=_hdr())
    assert r.status_code == 200, r.text
    got = client.get("/router/manifest", headers=_hdr()).json()["manifest"]
    assert [s["id"] for s in got["stores"]] == ["csv-1", "postgres-1"], (
        f"the DRAFT must ride in the stored row - that IS the #818 fix: {got}")
    draft = got["stores"][1]
    assert draft["kind"] == "postgres" and draft["config"].get("host") == "", (
        f"the draft must come back verbatim, empty fields and all: {draft}")


def test_secret_literal_refused_and_row_untouched():
    store = InMemoryManifestStore()
    client = TestClient(_app(store))
    ok = {"tenant": "acme", "stores": [_entry("csv-1")]}
    assert client.put("/router/manifest", json={"manifest": ok},
                      headers=_hdr()).status_code == 200
    bad = {"tenant": "acme",
           "stores": [{"id": "pg-1", "kind": "postgres", "business_unit": "",
                       "acl": ["oid-a"],
                       "config": {"host": "h", "password": "hunter2-plaintext"}}]}
    r = client.put("/router/manifest", json={"manifest": bad}, headers=_hdr())
    assert r.status_code == 400, (
        f"a plaintext credential must be refused exactly as compose refuses it: {r.text}")
    assert "pg-1.password" in r.text, f"the refusal must name store.field: {r.text}"
    assert "hunter2" not in r.text, f"LAW 1: the refusal must never carry the value: {r.text}"
    got = client.get("/router/manifest", headers=_hdr()).json()["manifest"]
    assert [s["id"] for s in got["stores"]] == ["csv-1"], (
        f"a refused PUT must leave the row untouched: {got}")


def test_two_users_rows_are_isolated():
    client = TestClient(_app(InMemoryManifestStore()))
    ma = {"tenant": "acme", "stores": [_entry("csv-a", acl=("oid-a",))]}
    mb = {"tenant": "acme", "stores": [_entry("csv-b", acl=("oid-b",))]}
    assert client.put("/router/manifest", json={"manifest": ma},
                      headers=_hdr("oid-a")).status_code == 200
    assert client.put("/router/manifest", json={"manifest": mb},
                      headers=_hdr("oid-b")).status_code == 200
    ga = client.get("/router/manifest", headers=_hdr("oid-a")).json()["manifest"]
    gb = client.get("/router/manifest", headers=_hdr("oid-b")).json()["manifest"]
    assert [s["id"] for s in ga["stores"]] == ["csv-a"], ga
    assert [s["id"] for s in gb["stores"]] == ["csv-b"], gb


def test_put_never_composes_a_warm_catalog():
    client = TestClient(_app(InMemoryManifestStore()))
    m1 = {"tenant": "acme", "stores": [_entry("csv-1")]}
    r = client.post("/router/compose", json={"manifest": m1}, headers=_hdr())
    assert r.status_code == 200 and len(r.json()["stores"]) == 1, r.text
    # csv-2 is fully composable - if the PUT composed, the warm catalog would gain it.
    m2 = {"tenant": "acme", "stores": [_entry("csv-1"), _entry("csv-2")]}
    assert client.put("/router/manifest", json={"manifest": m2},
                      headers=_hdr()).status_code == 200
    cat = client.get("/router/catalog", headers=_hdr()).text
    assert "csv-1" in cat and "csv-2" not in cat, (
        "PUT /router/manifest composed the manifest - it must be a row write only, the "
        f"canvas composes when the user composes: {cat[:300]}")
    got = client.get("/router/manifest", headers=_hdr()).json()["manifest"]
    assert [s["id"] for s in got["stores"]] == ["csv-1", "csv-2"], got


def test_layout_rides_the_row_verbatim():
    client = TestClient(_app(InMemoryManifestStore()))
    m = {"tenant": "acme", "stores": [_entry("csv-1")], "layout": {"csv-1": [1234, 777]}}
    assert client.put("/router/manifest", json={"manifest": m},
                      headers=_hdr()).status_code == 200
    got = client.get("/router/manifest", headers=_hdr()).json()["manifest"]
    assert got.get("layout") == {"csv-1": [1234, 777]}, (
        f"a MOVE is a mutation too - the layout must ride the stored row: {got}")


def test_compose_keeps_the_layout_it_was_posted():
    client = TestClient(_app(InMemoryManifestStore()))
    m = {"tenant": "acme", "stores": [_entry("csv-1")], "layout": {"csv-1": [50, 60]}}
    r = client.post("/router/compose", json={"manifest": m}, headers=_hdr())
    assert r.status_code == 200, r.text
    got = client.get("/router/manifest", headers=_hdr()).json()["manifest"]
    assert got.get("layout") == {"csv-1": [50, 60]}, (
        "compose overwrote the row and DROPPED the layout - the two row writers must "
        f"carry the same shape: {got}")


def test_store_outage_is_a_503_not_a_lying_200():
    class _Down(InMemoryManifestStore):
        def put(self, key, manifest):
            raise ManifestStoreUnavailable("down")
    client = TestClient(_app(_Down()))
    m = {"tenant": "acme", "stores": [_entry("csv-1")]}
    r = client.put("/router/manifest", json={"manifest": m}, headers=_hdr())
    assert r.status_code == 503, (
        f"an outage must say NOT saved - an empty success hides an outage: {r.status_code}")
    assert "NOT saved" in r.text, r.text


def test_no_store_configured_is_a_503_not_a_lying_200():
    client = TestClient(_app(None))
    m = {"tenant": "acme", "stores": [_entry("csv-1")]}
    r = client.put("/router/manifest", json={"manifest": m}, headers=_hdr())
    assert r.status_code == 503, (
        f"with no manifest store there is nothing durable to save into: {r.status_code}")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
