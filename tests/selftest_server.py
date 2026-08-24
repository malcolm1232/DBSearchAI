"""Self-host server self-test (in-memory backend, no Docker needed).

Proves the REST + GraphQL surface works AND that permission trimming (LAW 2) holds through
the HTTP layer: a user not in a document's group can't retrieve it via /search or GraphQL.

    python3 tests/selftest_server.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"  # must be set before importing the app
os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

DEAL = "deal-falcon"
client = TestClient(app)


def main():
    print("Self-host server self-test (REST + GraphQL, LAW 2 through HTTP):")

    h = client.get("/health").json()
    assert h["status"] == "ok"
    print(f"  PASS  /health -> {h}")

    # /ingest is gated behind a trusted identity (LAW 2): no identity -> 401, never indexes.
    r = client.post("/ingest", json={"external_id": "x", "title": "x", "text": "x", "acl": ["all-staff"]})
    assert r.status_code == 401, f"/ingest must require identity, got {r.status_code}"

    client.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={
        "external_id": "public-handbook", "title": "Staff Handbook",
        "text": "General staff handbook: holidays, expenses, onboarding for all staff.",
        "acl": ["all-staff"]})
    client.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={
        "external_id": DEAL, "title": "Project Falcon — Confidential",
        "text": "Confidential Project Falcon merger acquisition target valuation, deal team only.",
        "acl": ["deal-team"]})
    print("  PASS  /ingest gated (no-identity 401) + indexed 2 docs (one restricted to 'deal-team')")

    q = "confidential falcon merger acquisition valuation"
    # Identity is the trusted X-DBSearch-User header, never a body field (LAW 2).
    a = client.post("/search", headers={"X-DBSearch-User": "alice"}, json={"question": q}).json()
    b = client.post("/search", headers={"X-DBSearch-User": "bob"}, json={"question": q}).json()
    assert DEAL in a["authorized_docs"], "alice should retrieve the deal doc"
    assert DEAL not in b["authorized_docs"], "LAW 2 BREACH via REST: bob retrieved the deal doc"
    print(f"  PASS  /search REST trims  ->  alice={a['authorized_docs']}  bob={b['authorized_docs']}")

    # GraphQL through the mounted app — identity from the same trusted header
    gql = '{ search(question: "%s") { authorizedDocs citations { doc } } }'
    ga = client.post("/graphql", headers={"X-DBSearch-User": "alice"}, json={"query": gql % q}).json()["data"]["search"]
    gb = client.post("/graphql", headers={"X-DBSearch-User": "bob"}, json={"query": gql % q}).json()["data"]["search"]
    assert DEAL in ga["authorizedDocs"] and DEAL not in gb["authorizedDocs"], (ga, gb)
    print(f"  PASS  /graphql trims      ->  alice={ga['authorizedDocs']}  bob={gb['authorizedDocs']}")

    # /draft — happy path
    r = client.post("/draft", json={"brief": "Onboarding for a retail bank."},
                    headers={"X-DBSearch-User": "alice"})
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert body["plan"] and body["sections"], body
    assert "title" in body["sections"][0] and "prose" in body["sections"][0], body
    # /draft — empty brief -> 400
    r = client.post("/draft", json={"brief": "   "}, headers={"X-DBSearch-User": "alice"})
    assert r.status_code == 400, r.status_code
    # /draft — no identity -> 401
    r = client.post("/draft", json={"brief": "x"})
    assert r.status_code == 401, r.status_code
    print("  PASS  /draft (happy-path 200, empty-brief 400, no-identity 401)")

    print("\nALL SERVER SELF-TESTS PASSED — REST + GraphQL self-host surface is permission-faithful.")


if __name__ == "__main__":
    main()
