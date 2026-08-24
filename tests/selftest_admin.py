"""HTTP self-test for the Admin Console slice (REST, LAW 1 + LAW 2 through the API).
Run: python3 tests/selftest_admin.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import _edition, app  # noqa: E402

client = TestClient(app)
ADMIN = {"X-DBSearch-User": "operator"}        # any authenticated user in dev
_CONTENT_KEYS = {"text", "body", "snippet", "content", "chunk"}


def _assert_no_content(obj, where):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k.lower() not in _CONTENT_KEYS, f"content key {k!r} leaked in {where}"
            _assert_no_content(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _assert_no_content(v, where)


def main():
    print("Admin Console HTTP self-test:")
    client.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={"external_id": "public-handbook", "title": "Staff Handbook",
        "text": "holidays expenses onboarding all staff", "acl": ["all-staff"], "uri": "https://x/h"})
    client.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={"external_id": "deal-falcon", "title": "Project Falcon — Confidential",
        "text": "confidential falcon valuation deal team", "acl": ["deal-team"], "uri": "https://x/f"})

    h = client.get("/admin/index", headers=ADMIN)
    assert h.status_code == 200, h.text
    hj = h.json()
    assert hj["doc_count"] == 2 and hj["backend"] == "memory", hj
    _assert_no_content(hj, "/admin/index")
    print(f"  PASS  /admin/index -> {hj}")

    ids = client.get("/admin/identities", headers=ADMIN).json()
    deal = next(g for g in ids["groups"] if g["group_oid"] == "deal-team")
    assert deal["member_count"] == 1 and deal["doc_count"] == 1, deal
    _assert_no_content(ids, "/admin/identities")
    print(f"  PASS  /admin/identities -> {len(ids['users'])} users, {len(ids['groups'])} groups")

    pa = client.post("/admin/permission-test", headers=ADMIN,
                     json={"user_oid": "alice", "question": "falcon"}).json()
    fa = next(r for r in pa["results"] if r["doc_external_id"] == "deal-falcon")
    assert fa["returned"] is True and fa["matched_principals"] == ["deal-team"], fa
    pb = client.post("/admin/permission-test", headers=ADMIN,
                     json={"user_oid": "bob", "question": "falcon"}).json()
    fb = next(r for r in pb["results"] if r["doc_external_id"] == "deal-falcon")
    assert fb["returned"] is False and fb["matched_principals"] == [], fb
    print("  PASS  /admin/permission-test -> alice sees Falcon, bob denied (LAW 2)")
    _assert_no_content(pa, "/admin/permission-test")
    _assert_no_content(pb, "/admin/permission-test")

    # a query is metered, then telemetry reflects it
    client.post("/search", headers={"X-DBSearch-User": "alice"}, json={"question": "holidays"})
    t = client.get("/admin/telemetry", headers=ADMIN).json()
    assert t["counts"].get("docs_indexed") == 2, t
    assert t["counts"].get("queries_served", 0) >= 1, t
    _assert_no_content(t, "/admin/telemetry")
    print(f"  PASS  /admin/telemetry -> counts={t['counts']}")

    # --- Phase 2b: Sources + resync ---
    srcs = client.get("/admin/sources", headers=ADMIN)
    assert srcs.status_code == 200, srcs.text
    sj = srcs.json()
    assert isinstance(sj, list) and len(sj) == 1, sj
    sp = sj[0]
    assert sp["source_id"] == "sharepoint" and sp["kind"] == "sharepoint", sp
    assert sp["last_sync_at"] is None and sp["doc_count"] == 0 and sp["status"] == "idle", sp
    _assert_no_content(sj, "/admin/sources")
    print(f"  PASS  /admin/sources -> {sj}")

    # #569: /admin/resync SUBMITS the re-crawl and returns 202 with a handle. It used to run
    # the whole thing inside this request - the LAW 4 violation #454 removed from the router
    # rail, still live on a route any operator can point at a real library.
    rs = client.post("/admin/resync", headers=ADMIN, json={"source_id": "sharepoint"})
    assert rs.status_code == 202, rs.text
    rj = rs.json()
    assert rj["job_id"] and rj["poll"] == f"/ingest/jobs/{rj['job_id']}", rj
    _assert_no_content(rj, "/admin/resync")

    job = _edition.await_ingest(rj["job_id"], timeout=30)
    assert job.status == "succeeded", f"{job.status} {job.error}"
    src = next(s for s in _edition.admin_service.sources() if s.source_id == "sharepoint")
    assert src.doc_count == 3 and src.status == "idle" and src.last_sync_at, src
    print(f"  PASS  /admin/resync -> 202 job {rj['job_id']}, then doc_count={src.doc_count}")

    nf = client.post("/admin/resync", headers=ADMIN, json={"source_id": "nope"})
    assert nf.status_code == 404, nf.text
    print(f"  PASS  /admin/resync unknown source -> 404")

    # admin requires identity (LAW 2): no header -> 401
    assert client.get("/admin/index").status_code == 401
    print("  PASS  /admin/index requires identity (401 without header)")

    print("\nADMIN CONSOLE HTTP SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
