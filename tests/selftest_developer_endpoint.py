"""Self-test: /developer/keys CRUD + guard codes, and a minted key drives /search as its
bound user (LAW 2) through the HTTP path.

    python3 tests/selftest_developer_endpoint.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

c = TestClient(app)
ALICE = {"X-DBSearch-User": "alice"}
BOB = {"X-DBSearch-User": "bob"}


def main():
    # seed a deal-team doc alice can see, bob cannot
    c.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={"external_id": "deal-falcon", "title": "Falcon",
                            "text": "confidential falcon merger valuation deal team",
                            "acl": ["deal-team"]})

    # mint a key as alice -> token shown once
    r = c.post("/developer/keys", headers=ALICE, json={"label": "ci"})
    assert r.status_code == 200, r.text
    body = r.json()
    token = body["token"]
    key_id = body["record"]["id"]
    assert token.startswith(key_id + ".")

    # list shows it, WITHOUT the secret
    r = c.get("/developer/keys", headers=ALICE)
    ids = [k["id"] for k in r.json()]
    assert key_id in ids
    assert "secret" not in r.text and token.split(".", 1)[1] not in r.text, "secret leaked in list"

    # the minted key authenticates /search AS alice (LAW 2 positive control)
    r = c.post("/search", headers={"Authorization": f"Bearer {token}"},
               json={"question": "falcon"})
    assert r.status_code == 200, r.text
    assert "deal-falcon" in r.json()["authorized_docs"], "alice's key should retrieve Falcon"

    # empty label -> 400; oversized -> 400
    assert c.post("/developer/keys", headers=ALICE, json={"label": ""}).status_code == 400
    assert c.post("/developer/keys", headers=ALICE, json={"label": "x" * 201}).status_code == 400

    # bob cannot revoke alice's key -> 404
    assert c.request("DELETE", f"/developer/keys/{key_id}", headers=BOB).status_code == 404

    # alice revokes -> 200; then the key no longer authenticates (-> 401), no wrong/revoked distinction
    assert c.request("DELETE", f"/developer/keys/{key_id}", headers=ALICE).status_code == 200
    assert c.post("/search", headers={"Authorization": f"Bearer {token}"},
                  json={"question": "falcon"}).status_code == 401

    # graphql schema renders
    r = c.get("/developer/graphql-schema")
    assert r.status_code == 200 and "type Query" in r.json()["sdl"]

    print("PASS selftest_developer_endpoint")


if __name__ == "__main__":
    main()
