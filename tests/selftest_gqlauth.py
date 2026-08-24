"""GQLAUTH — prove the API derives identity from the trusted transport, not a client value.

The security spec (LAW 2): a caller MUST NOT be able to choose whose results they get.
Identity comes from the request's auth context (a header in dev, a verified bearer token in
prod), never from a client-supplied argument/body field. No identity -> 401.

    DBSEARCH_DEV_AUTH=1 python3 tests/selftest_gqlauth.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.api.graphql_app import build_schema  # noqa: E402
from dbsearch.server.app import _edition, app  # noqa: E402

DEAL = "deal-falcon"
client = TestClient(app)


def main():
    print("GQLAUTH self-test (identity from trusted transport, no impersonation):")

    client.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={"external_id": "public-handbook", "title": "Handbook",
                                 "text": "Holidays and expenses for all staff.", "acl": ["all-staff"]})
    client.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={"external_id": DEAL, "title": "Project Falcon (Confidential)",
                                 "text": "Confidential Falcon merger acquisition valuation, deal team only.",
                                 "acl": ["deal-team"]})
    q = "confidential falcon merger valuation"

    # 1. IMPERSONATION PREVENTED: authenticated as bob, but body claims user=alice.
    #    The body identity must be ignored — bob is not on deal-team, so no deal doc.
    r = client.post("/search", headers={"X-DBSearch-User": "bob"}, json={"user": "alice", "question": q})
    assert r.status_code == 200, r.status_code
    assert DEAL not in r.json()["authorized_docs"], "IMPERSONATION: bob got alice's doc via body field"
    print("  PASS  body-supplied identity is ignored — bob cannot impersonate alice")

    # 2. Legitimate identity via the trusted header works.
    ra = client.post("/search", headers={"X-DBSearch-User": "alice"}, json={"question": q})
    assert DEAL in ra.json()["authorized_docs"], "alice (deal-team, via header) should see the deal doc"
    print("  PASS  trusted-header identity works — alice sees her authorized doc")

    # 3. No identity at all -> 401 (no anonymous access).
    rn = client.post("/search", json={"question": q})
    assert rn.status_code == 401, f"expected 401 without identity, got {rn.status_code}"
    print("  PASS  no identity -> 401 (no anonymous access)")

    # 4. GraphQL: identity comes from context; there is NO spoofable userOid argument.
    schema = build_schema(_edition.query_service)
    gb = schema.execute_sync('{ search(question: "%s") { authorizedDocs } }' % q,
                             context_value={"user_oid": "bob"})
    assert gb.errors is None, gb.errors
    assert DEAL not in gb.data["search"]["authorizedDocs"], "GraphQL: bob got the deal doc"
    print("  PASS  GraphQL identity from context, no client userOid arg to spoof")

    # 5. dbk_ KEY is permission-faithful: alice's key sees Falcon; bob's key does NOT (cross-user).
    alice_key = client.post("/developer/keys", headers={"X-DBSearch-User": "alice"},
                            json={"label": "k"}).json()["token"]
    bob_key = client.post("/developer/keys", headers={"X-DBSearch-User": "bob"},
                          json={"label": "k"}).json()["token"]
    ra2 = client.post("/search", headers={"Authorization": f"Bearer {alice_key}"}, json={"question": q})
    rb2 = client.post("/search", headers={"Authorization": f"Bearer {bob_key}"}, json={"question": q})
    assert DEAL in ra2.json()["authorized_docs"], "alice's key should see Falcon (positive control)"
    assert DEAL not in rb2.json()["authorized_docs"], "LAW 2 BREACH: bob's key saw Falcon"
    print("  PASS  dbk_ keys are permission-faithful (alice sees Falcon, bob does not)")

    print("\nALL GQLAUTH TESTS PASSED — identity is trusted-transport-derived; impersonation impossible.")


if __name__ == "__main__":
    main()
