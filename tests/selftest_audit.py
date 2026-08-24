"""#45 — query/access audit log.

Proves: AuditLog records newest-first with a bounded ring buffer; a served query writes an
audit entry; /admin/audit is admin-gated and returns metadata only (user, question, authorized
doc IDs + count) — NEVER document content (LAW 1).

    python3 tests/selftest_audit.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.audit import InMemoryAuditLog  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

c = TestClient(app)
FALCON_TEXT = "Confidential Project Falcon merger acquisition valuation, deal team only."


def test_auditlog_unit():
    log = InMemoryAuditLog(capacity=3)
    for i in range(5):
        log.record(f"u{i}", f"q{i}", "ask", [f"doc{i}"], ts=f"t{i}")
    assert len(log) == 3, "ring buffer should cap at 3"
    recent = log.recent(10)
    assert [e.user for e in recent] == ["u4", "u3", "u2"], "newest-first + capped"
    assert recent[0].n_authorized == 1 and recent[0].authorized_docs == ["doc4"]
    print("  PASS  AuditLog ring buffer + newest-first + n_authorized")


def test_query_writes_audit_and_admin_gated():
    c.post("/ingest", headers={"X-DBSearch-User": "alice"},
           json={"external_id": "deal-falcon", "title": "Falcon", "text": FALCON_TEXT,
                 "acl": ["deal-team"]})
    # alice (deal-team) asks -> served + audited
    c.post("/chat", headers={"X-DBSearch-User": "alice"},
           json={"conv_id": "a1", "question": "confidential falcon merger valuation"})
    # bob (no deal-team) asks the same -> served but trimmed to 0
    c.post("/chat", headers={"X-DBSearch-User": "bob"},
           json={"conv_id": "b1", "question": "confidential falcon merger valuation"})

    # /admin/audit is gated
    assert c.get("/admin/audit").status_code == 401, "audit must require identity"

    rows = c.get("/admin/audit", headers={"X-DBSearch-User": "admin"}).json()
    assert len(rows) >= 2, rows
    # newest-first: bob's query is last recorded
    bob_row = next(r for r in rows if r["user"] == "bob")
    alice_row = next(r for r in rows if r["user"] == "alice")
    assert "confidential falcon" in alice_row["question"], alice_row
    # access-reason transparency: alice saw the deal doc, bob saw nothing
    assert "deal-falcon" in alice_row["authorized_docs"] and alice_row["n_authorized"] == 1, alice_row
    assert bob_row["authorized_docs"] == [] and bob_row["n_authorized"] == 0, bob_row
    print("  PASS  served queries are audited; /admin/audit gated; access trim reflected")

    # LAW 1: NO document content anywhere in the audit payload (only ids/titles/counts)
    blob = c.get("/admin/audit", headers={"X-DBSearch-User": "admin"}).text
    assert FALCON_TEXT not in blob, "LAW 1 BREACH: document content leaked into the audit log"
    print("  PASS  audit payload carries metadata only (no document content — LAW 1)")


def main():
    print("Query/access audit-log self-test (#45):")
    test_auditlog_unit()
    test_query_writes_audit_and_admin_gated()
    print("\nALL AUDIT TESTS PASSED — per-query access trail, gated, metadata-only.")


if __name__ == "__main__":
    main()
