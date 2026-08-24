"""#302: live indexing progress — the ingest pipeline reports discover→fetch→extract→embed→done
so the canvas can show what it's doing instead of a silent 'Ingesting…'.

Proves (a) run_ingestion drives the progress callback through every stage with sane counts, and
(b) the per-tenant progress store + GET /connectors/sharepoint/ingest-progress round-trip.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIndex, InMemoryObjectStore, InMemoryQueue, PlainTextExtractor,
)
from dbsearch.connectors.sharepoint import SharePointConnector  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.server import sp_connect, user_auth  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
HDR = {"X-DBSearch-User": "alice"}
COOKIES = {user_auth.COOKIE: user_auth.sign_session({"oid": "alice", "exp": int(time.time()) + 3600})}
fails = 0


def check(name, cond):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails += 1


print("Ingest progress — #302 self-test:")

# --- run_ingestion drives the progress callback through the pipeline ------------------------
seed = [{"external_id": f"doc-{i}", "title": f"Doc {i}", "uri": f"https://ex/{i}",
         "acl": ["all-staff"], "text": "some content " * 40} for i in range(3)]
events = []
obj = InMemoryObjectStore()
res = run_ingestion(
    SharePointConnector(tenant_id="t", seed=seed), InMemoryQueue(), obj,
    PlainTextExtractor(), HashingEmbedding(), InMemoryIndex(obj),
    progress=lambda phase, done, total: events.append((phase, done, total)),
)
phases = [e[0] for e in events]
check("progress reports every stage in order",
      phases and phases[0] == "discovering"
      and {"fetching", "extracting", "embedding", "done"} <= set(phases)
      and phases[-1] == "done")
fetch = [e for e in events if e[0] == "fetching"]
check("fetch phase counts up to the total document count",
      any(d == 3 and t == 3 for _, d, t in fetch))
emb = [e for e in events if e[0] == "embedding"]
check("embed phase advances per document (the slow stage the UI watches)",
      [d for _, d, _ in emb] == [1, 2, 3])
obj2 = InMemoryObjectStore()
check("no progress reporting does not change the result (default no-op)",
      run_ingestion(SharePointConnector(tenant_id="t2", seed=seed), InMemoryQueue(),
                    obj2, PlainTextExtractor(), HashingEmbedding(),
                    InMemoryIndex(obj2)).doc_count == 3)

# --- the per-tenant progress store + polling endpoint round-trip ----------------------------
sp_connect.set_progress("tenantX", "embedding", 120, 600)
r = client.get("/connectors/sharepoint/ingest-progress?tenant=tenantX", headers=HDR, cookies=COOKIES)
check("/ingest-progress returns the live phase/done/total",
      r.status_code == 200 and r.json() == {"phase": "embedding", "done": 120, "total": 600,
                                            **{k: r.json().get(k) for k in ("at",)}})
sp_connect.clear_progress("tenantX")
r = client.get("/connectors/sharepoint/ingest-progress?tenant=tenantX", headers=HDR, cookies=COOKIES)
check("/ingest-progress reports idle once nothing is running", r.json()["phase"] == "idle")

# --- #365: terminal states survive a proxy-dropped /finish response -------------------------
sp_connect.set_progress_complete("tenantX", {"status": "connected", "docs_indexed": 3,
                                             "drive_id": "d1", "source_id": "s1"})
r = client.get("/connectors/sharepoint/ingest-progress?tenant=tenantX", headers=HDR, cookies=COOKIES)
check("complete terminal state is served with its result payload",
      r.json()["phase"] == "complete" and r.json()["result"]["docs_indexed"] == 3)
r = client.get("/connectors/sharepoint/ingest-progress?tenant=tenantX&ack=1",
               headers=HDR, cookies=COOKIES)
check("ack=1 clears the terminal state", r.json()["phase"] == "idle")
sp_connect.set_progress_error("tenantX", "boom")
r = client.get("/connectors/sharepoint/ingest-progress?tenant=tenantX", headers=HDR, cookies=COOKIES)
check("error terminal state is served with its detail",
      r.json()["phase"] == "error" and r.json()["detail"] == "boom")
sp_connect.set_progress("tenantX", "embedding", 1, 3)
r = client.get("/connectors/sharepoint/ingest-progress?tenant=tenantX&ack=1",
               headers=HDR, cookies=COOKIES)
check("ack=1 never clears a LIVE phase (only terminal states)",
      r.json()["phase"] == "embedding")
sp_connect.clear_progress("tenantX")

print("\n" + ("ALL #302 SELF-TESTS PASSED — ingest reports live progress; poll endpoint round-trips."
              if fails == 0 else f"{fails} #302 CHECK(S) FAILED"))
sys.exit(1 if fails else 0)
