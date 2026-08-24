"""S2 self-test for the in-app SharePoint connector (card #148): runtime connect + ingest.

Proves Edition.connect_sharepoint() ingests a (mock) SharePoint library into the running data
plane, registers it as a queryable source, and that retrieval through the SAME QueryService
stays permission-trimmed (LAW 2) — all without a live Graph tenant. Also exercises the
/connectors/sharepoint/finish endpoint (with the edition method stubbed) for HTTP wiring.
"""
import os
import sys
import time
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
os.environ["SP_CONNECTOR_CLIENT_ID"] = "cid-123"
os.environ["SP_CONNECTOR_CLIENT_SECRET"] = "secret-xyz"
os.environ["SP_CONNECTOR_REDIRECT_URI"] = "http://localhost:8080/connectors/sharepoint/callback"
# #423: the doc-plane tenant gate fail-closes on a real-login session with no tid, and a
# real Azure AD id token always carries one - AUTH_TENANT_ID is this rig's home tenant so
# the minted cookies below can carry a matching tid, same as production.
os.environ["AUTH_TENANT_ID"] = "home-tid"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.connectors.sharepoint import SharePointConnector  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import _edition, app  # noqa: E402

client = TestClient(app)
HDR = {"X-DBSearch-User": "alice"}
# #183: SP_CONNECTOR_* above also makes user_auth.is_enabled() True (it's the
# multi-tenant login app fallback), so current_user now requires a real session
# cookie rather than the dev header. Mint one per identity so LAW 2 trimming is
# still exercised through the real auth path. #423: both carry the home tid so
# the doc-plane tenant gate (real-login-only) admits them like production would.
ALICE_COOKIE = {user_auth.COOKIE: user_auth.sign_session(
    {"oid": "alice", "tid": "home-tid", "exp": int(time.time()) + 3600})}
BOB_COOKIE = {user_auth.COOKIE: user_auth.sign_session(
    {"oid": "bob", "tid": "home-tid", "exp": int(time.time()) + 3600})}
fails = 0


def check(name, cond):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails += 1


print("In-app SharePoint connector — S2 (runtime connect + ingest) self-test:")

# A mock 'live SharePoint' library: one all-staff doc + one deal-team-only confidential doc,
# ACL'd to the demo groups (alice ∈ deal-team, bob ∉) — same contrast as the real product.
seed = [
    {"external_id": "sp-handbook", "title": "Staff Handbook", "uri": "sp://handbook",
     "acl": ["all-staff"], "text": "General staff handbook: holidays and expenses for all staff."},
    {"external_id": "sp-falcon", "title": "Project Falcon — Confidential", "uri": "sp://falcon",
     "acl": ["deal-team"], "text": "Confidential Project Falcon merger acquisition valuation, deal team only."},
]
fake_connector = SharePointConnector(tenant_id=_edition.tenant_id, seed=seed)

# #569: connect_sharepoint SUBMITS the crawl and returns a job handle - it no longer runs
# the crawl inside the caller. Wait for the job, then assert on what was actually ingested.
# `docs_indexed` is deliberately gone from the return rather than reported as 0: a zero there
# would say "0 documents indexed" about a crawl that had merely not started yet.
result = _edition.connect_sharepoint("tenantB", "drive-9", connector=fake_connector)
job = _edition.await_ingest(result["job_id"], timeout=30)
check("connect_sharepoint ingests + registers the source",
      job.status == "succeeded" and job.docs_done == 2 and result["source_id"] == "sharepoint:tenantB")

sources = [s.source_id for s in _edition.admin_service.sources()]
check("connected library registered as a source", "sharepoint:tenantB" in sources)

# Query through the SAME QueryService — LAW 2 trim must hold on the freshly connected data.
q = "confidential falcon merger acquisition valuation"
a = client.post("/search", headers={"X-DBSearch-User": "alice"}, cookies=ALICE_COOKIE, json={"question": q}).json()
b = client.post("/search", headers={"X-DBSearch-User": "bob"}, cookies=BOB_COOKIE, json={"question": q}).json()
check("alice (deal-team) retrieves the connected confidential doc", "sp-falcon" in a["authorized_docs"])
check("bob (not deal-team) does NOT — LAW 2 holds on connected data", "sp-falcon" not in b["authorized_docs"])

# /finish endpoint wiring (stub the edition method so no network, verify HTTP contract)
_edition.connect_sharepoint = lambda tenant, drive_id, **k: {
    "source_id": f"sharepoint:{tenant}", "tenant": tenant, "drive_id": drive_id,
    "job_id": "job-stub-569", "job_status": "running"}
r = client.post("/connectors/sharepoint/finish", headers=HDR, cookies=ALICE_COOKIE, json={"tenant": "tenantC", "drive_id": "d2"})
# #569: 202 ACCEPTED with a handle, not 200 with a finished result. The crawl outcome arrives
# on the progress store / GET /ingest/jobs/{id}, published by the worker.
check("/finish -> 202 ingesting + a job handle to follow",
      r.status_code == 202 and r.json()["status"] == "ingesting" and r.json()["job_id"] == "job-stub-569")

print("\n" + ("ALL S2 SELF-TESTS PASSED — runtime connect+ingest works and stays permission-trimmed."
              if fails == 0 else f"{fails} S2 CHECK(S) FAILED"))
sys.exit(1 if fails else 0)
