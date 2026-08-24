"""Self-test: DBSEARCH_DEMO_SEED=1 seeds a baseline corpus so the Ask examples work and the
alice/bob LAW-2 contrast is live out of the box; GET /admin/documents lists what's indexed.

    python3 tests/selftest_demo_seed.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
os.environ["DBSEARCH_DEMO_SEED"] = "1"   # MUST be set before importing the app (seed runs at build)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

c = TestClient(app)


def _docs(user, q):
    return c.post("/chat", headers={"X-DBSearch-User": user},
                  json={"conv_id": f"{user}-{q[:4]}", "question": q}).json()["authorized_docs"]


def main():
    # /admin/documents lists the seeded docs with their ACLs
    docs = c.get("/admin/documents", headers={"X-DBSearch-User": "alice"}).json()
    by_id = {d["doc_external_id"]: d for d in docs}
    assert "demo-handbook" in by_id and "demo-falcon" in by_id, f"seed missing: {list(by_id)}"
    assert by_id["demo-falcon"]["allowed_principals"] == ["deal-team"]
    assert "all-staff" in by_id["demo-handbook"]["allowed_principals"]

    # the Ask example prompts now retrieve
    assert "demo-handbook" in _docs("alice", "what is our holiday and expenses policy"), "handbook not found"
    assert "demo-falcon" in _docs("alice", "summarize the Project Falcon valuation"), "falcon not found for alice"

    # LAW 2 baked into the seed: bob (all-staff) sees the handbook but NOT Falcon
    assert "demo-handbook" in _docs("bob", "what is our holiday and expenses policy"), "bob should see handbook"
    assert "demo-falcon" not in _docs("bob", "summarize the Project Falcon valuation"), "LAW 2 BREACH: bob saw Falcon"

    print("PASS selftest_demo_seed (seed + /admin/documents + LAW-2 out of the box)")


if __name__ == "__main__":
    main()
