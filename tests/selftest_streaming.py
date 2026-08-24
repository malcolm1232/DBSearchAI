"""Self-test: POST /chat/stream emits SSE token events then a done event, and the streaming
path is permission-faithful (LAW 2) — same trim as the non-streaming path.

    python3 tests/selftest_streaming.py
"""
import json
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

c = TestClient(app)
DEAL = "deal-falcon"


def _stream(user, question):
    events = []
    with c.stream("POST", "/chat/stream",
                  headers={"X-DBSearch-User": user},
                  json={"conv_id": f"cv-{user}", "question": question}) as r:
        assert r.status_code == 200, r.status_code
        for line in r.iter_lines():
            line = line.strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


def main():
    # /ingest is auth-gated (#38) — seed with an identity header.
    seed_h = {"X-DBSearch-User": "alice"}
    c.post("/ingest", headers=seed_h, json={"external_id": "public-handbook", "title": "Handbook",
                                            "text": "Holidays and expenses for all staff.", "acl": ["all-staff"]})
    c.post("/ingest", headers=seed_h, json={"external_id": DEAL, "title": "Project Falcon (Confidential)",
                                            "text": "Confidential Falcon merger valuation, deal team only.",
                                            "acl": ["deal-team"]})
    q = "falcon merger valuation"

    ev_alice = _stream("alice", q)
    tokens = [e for e in ev_alice if e.get("type") == "token"]
    done = [e for e in ev_alice if e.get("type") == "done"]
    assert tokens, "expected at least one token event"
    assert len(done) == 1, f"expected exactly one done event, got {len(done)}"
    assert DEAL in done[0]["authorized_docs"], "alice (deal-team) should see Falcon (positive control)"
    # the streamed tokens reconstruct the final answer
    assert "".join(t["text"] for t in tokens) == done[0]["answer"], "tokens must rebuild the answer"

    # streaming uses the SAME trim as non-streaming /chat (the security invariant for #50)
    ns = c.post("/chat", headers={"X-DBSearch-User": "alice"},
                json={"conv_id": "ns", "question": q}).json()
    assert set(done[0]["authorized_docs"]) == set(ns["authorized_docs"]), \
        "streaming path must trim identically to non-streaming"

    # LAW 2 through the streaming path: bob (all-staff) must NOT get Falcon
    ev_bob = _stream("bob", q)
    done_bob = [e for e in ev_bob if e.get("type") == "done"][0]
    assert DEAL not in done_bob["authorized_docs"], "LAW 2 BREACH: bob streamed Falcon"

    print("PASS selftest_streaming (SSE tokens + done; stream==non-stream trim; LAW-2 holds)")


if __name__ == "__main__":
    main()
