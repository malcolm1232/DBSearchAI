"""End-to-end: mint a key -> it lists -> it authenticates /search as the bound user ->
revoke -> it stops working. Plus the reference renders from the live contract.

    python3 tests/e2e_developer.py
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


def main():
    c.post("/ingest", headers={"X-DBSearch-User": "alice"},
           json={"external_id": "deal-falcon", "title": "Falcon",
                 "text": "confidential falcon merger valuation deal team",
                 "acl": ["deal-team"]})
    # alice mints a key
    token = c.post("/developer/keys", headers={"X-DBSearch-User": "alice"},
                   json={"label": "e2e"}).json()["token"]
    # key works AS alice
    r = c.post("/search", headers={"Authorization": f"Bearer {token}"}, json={"question": "falcon"})
    assert r.status_code == 200 and "deal-falcon" in r.json()["authorized_docs"], r.text
    # reference renders
    assert "type Query" in c.get("/developer/graphql-schema").json()["sdl"]
    assert "/search" in c.get("/openapi.json").json()["paths"]
    # revoke -> key dies
    kid = token.split(".", 1)[0]
    assert c.request("DELETE", f"/developer/keys/{kid}", headers={"X-DBSearch-User": "alice"}).status_code == 200
    assert c.post("/search", headers={"Authorization": f"Bearer {token}"},
                  json={"question": "falcon"}).status_code == 401
    print("PASS e2e_developer")


if __name__ == "__main__":
    main()
