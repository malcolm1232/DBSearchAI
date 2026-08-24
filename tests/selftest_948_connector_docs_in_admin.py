"""#948 - documents ingested through a CONNECTOR appear in the Admin "Your documents" list.

The owner, 260824: "when ingested, either gdrive or sharepoint, data must be reflected inside
Admin tab." Today it cannot: `/admin/documents` reads `_edition.list_documents`, which scans
the UPLOAD index (`self.index`), and a connector store indexes into its OWN in-process index
(router/providers/connector.py). So a caller whose only source is a Drive/SharePoint folder
sees an empty Admin, even though the node shows a doc count and /ask answers from it. That is
#937's two-plane split reaching the Admin surface.

The fix merges the second plane in, and these tests pin the two things that must both hold:
  1. a composed connector store's documents show up in /admin/documents, tagged with the
     source they came from (so the surface can say WHERE, and suppress the upload-only
     actions - Download / Delete - that would 404 on a doc the upload index never held);
  2. the merge is ACL-TRIMMED per caller through each store's OWN authorize (LAW 2) - the
     whole risk, exactly as #939 framed it. A fixture where every document is visible to
     everyone cannot fail on the trim, so this one gives the two folder sub-audiences a
     document each and asserts what alice (both) and bob (one) are each TOLD.

WARM-ONLY, by design: the merge reads the caller's WARM workspace and never materializes a
cold one. Materializing here would fire every connector's crawl just to render a list, so a
caller who has not composed this session sees uploads only until they do - the honest bound
of #940 (connector content is not durable), surfaced rather than papered over with a crawl on
every Admin visit. The test composes first, so the workspace is warm.

    PYTHONPATH=src python3 tests/selftest_948_connector_docs_in_admin.py
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

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
ALICE = {"X-DBSearch-User": "alice"}      # all-staff + deal-team
BOB = {"X-DBSearch-User": "bob"}          # all-staff only

_ROOT: "Path | None" = None


def _compose_folder_store():
    """A folder connector with two sub-audiences: all-staff and deal-team. The folder
    connector reads the immediate subdirectory name as the ACL group, so policy.txt is
    all-staff (alice + bob) and osprey.txt is deal-team (alice only) - the two-identity
    fixture #939 established for exactly this trim."""
    global _ROOT
    _ROOT = Path(tempfile.mkdtemp(prefix="dbse948-"))
    (_ROOT / "all-staff").mkdir()
    (_ROOT / "deal-team").mkdir()
    (_ROOT / "all-staff" / "policy.txt").write_text("travel policy: economy under six hours")
    (_ROOT / "deal-team" / "osprey.txt").write_text("project osprey valuation is nine hundred million")
    demo = client.get("/router/demo", headers=ALICE).json()["manifest"]
    demo["stores"].append({
        "id": "legal-archive", "kind": "folder", "mode": "index",
        "business_unit": "legal", "acl": ["all-staff"], "title": "Legal archive",
        "description": "legal archive osprey travel policy",
        "config": {"path": str(_ROOT)}})
    r = client.post("/router/compose", headers=ALICE, json={"manifest": demo})
    assert r.status_code == 200, r.text
    _await_ingest(r.json())


def _await_ingest(resp_json, timeout: float = 60.0):
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
        else:
            raise AssertionError(f"ingest job {job_id} never finished")


def _admin_docs(headers):
    r = client.get("/admin/documents", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_owner_sees_the_connector_documents_in_admin():
    """The card's whole point: an ingested folder's files are in the Admin listing."""
    _compose_folder_store()
    rows = _admin_docs(ALICE)
    titles = {d["title"] for d in rows}
    assert "policy.txt" in titles, f"the connector's all-staff doc is missing from Admin: {titles}"
    assert "osprey.txt" in titles, f"the connector's deal-team doc is missing from Admin: {titles}"
    print("  PASS  a composed connector store's documents appear in /admin/documents")


def test_each_connector_row_names_its_source_and_is_not_individually_deletable():
    """A connector document is removed by deleting its SOURCE node (#947), never one-by-one,
    and Download / Check-text read the UPLOAD index and would 404 on it. So the row must (a)
    say which store it came from, and (b) NOT claim owned_by_you, which is what draws the
    Delete/Share/Download controls in admin.js."""
    rows = _admin_docs(ALICE)
    conn = [d for d in rows if d["title"] in ("policy.txt", "osprey.txt")]
    assert conn, "no connector rows to check"
    for d in conn:
        assert d.get("source_store") == "legal-archive", f"row does not name its store: {d}"
        assert d.get("source_kind") == "folder", f"row does not name its kind: {d}"
        assert d.get("uri"), f"no uri to open the source with: {d}"
        assert d.get("owned_by_you") is not True, (
            f"a connector doc claims owned_by_you, which draws a Delete that 404s: {d}")
    print("  PASS  connector rows name their source and are not individually deletable")


def test_the_merge_is_acl_trimmed_per_caller():
    """The whole risk (#939's framing). bob is all-staff only, so he sees the all-staff
    folder doc and must NEVER see the deal-team one - even though both live in one store and
    the untrimmed index holds both."""
    brows = _admin_docs(BOB)
    btitles = {d["title"] for d in brows}
    assert "policy.txt" in btitles, f"bob cannot see the all-staff connector doc: {btitles}"
    assert "osprey.txt" not in btitles, (
        f"LAW 2 BREACH: bob sees the deal-team connector doc in Admin: {btitles}")
    print("  PASS  the Admin merge is ACL-trimmed per caller (bob is denied deal-team)")


def test_uploads_still_appear_alongside_connector_docs():
    """The merge must ADD connector docs, never REPLACE the upload plane. alice uploads one
    document; both it and the connector docs must be present in the same listing."""
    files = {"file": ("myupload.txt", b"my own uploaded note about badgers", "text/plain")}
    up = client.post("/admin/upload", headers=ALICE, files=files)
    assert up.status_code in (200, 201, 202), up.text
    # #569/LAW 4: the upload is async - poll its job to done before reading the listing, the
    # same discipline the connector crawl uses.
    body = up.json()
    poll = body.get("poll") or (f"/ingest/jobs/{body['job_id']}" if body.get("job_id") else None)
    if poll:
        deadline = time.time() + 30
        while time.time() < deadline:
            j = client.get(poll, headers=ALICE).json()
            if j.get("status") in ("succeeded", "failed") or j.get("job_status") in ("succeeded", "failed"):
                break
            time.sleep(0.05)
    rows = _admin_docs(ALICE)
    titles = {d["title"] for d in rows}
    assert "myupload.txt" in titles, f"the upload plane was dropped by the merge: {titles}"
    assert {"policy.txt", "osprey.txt"} <= titles, f"connector docs vanished when an upload joined: {titles}"
    # the upload IS individually owned/deletable; the connector docs are not - the two planes
    # keep their own action affordances in one list.
    up_row = next(d for d in rows if d["title"] == "myupload.txt")
    assert up_row.get("owned_by_you") is True, f"the upload lost its own-plane ownership: {up_row}"
    print("  PASS  uploads and connector docs coexist in one Admin listing, each with its own actions")


def test_admin_js_renders_a_connector_row_without_upload_only_actions():
    """A guard on the SURFACE, not just the payload: admin.js must branch on source_store so
    a connector doc gets a source badge + Open-source and NOT Download/Delete (which 404)."""
    admin_js = (Path(__file__).resolve().parents[1]
                / "src/dbsearch/server/static/js/surfaces/admin.js").read_text()
    assert "source_store" in admin_js, (
        "admin.js never reads source_store, so it draws upload-only actions on connector docs "
        "(Download/Check-text 404 on the upload index; Delete would 404 too)")
    print("  PASS  admin.js branches on source_store")


if __name__ == "__main__":
    failures = []
    for name in ["test_the_owner_sees_the_connector_documents_in_admin",
                 "test_each_connector_row_names_its_source_and_is_not_individually_deletable",
                 "test_the_merge_is_acl_trimmed_per_caller",
                 "test_uploads_still_appear_alongside_connector_docs",
                 "test_admin_js_renders_a_connector_row_without_upload_only_actions"]:
        try:
            globals()[name]()
        except AssertionError as e:
            failures.append(name); print(f"FAIL  {name}\n      {e}")
        except Exception as e:
            failures.append(name); print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
