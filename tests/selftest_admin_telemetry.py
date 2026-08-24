"""Telemetry emission: ingest + query feed the ControlPlane (boundary-validated), and the
Edition exposes an AdminService. Run: python3 tests/selftest_admin_telemetry.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_TENANT_ID"] = "selfhost"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.server.edition import build_edition  # noqa: E402


def main():
    print("Admin telemetry emission self-test:")
    ed = build_edition()
    assert ed.admin_service is not None, "edition must expose admin_service"

    ed.ingest_document("d1", "Doc One", "hello world payload", ["all-staff"], "https://x/d1")
    m = ed.control_plane.metering(ed.tenant_id)
    assert m.get("count:docs_indexed") == 1, m
    assert m.get("count:chunks_created") == 1, m
    h = ed.control_plane.health(ed.tenant_id) or {}
    assert h.get("index_ready") is True and h.get("last_index_ts"), h
    print(f"  PASS  ingest.completed metered -> {dict(m)}")

    ed.record_query_served(authorized_docs=3)
    m = ed.control_plane.metering(ed.tenant_id)
    assert m.get("count:queries_served") == 1, m
    assert m.get("count:authorized_docs") == 3, m
    print(f"  PASS  query.served metered -> {dict(m)}")

    # the AdminService telemetry view reflects it
    snap = ed.admin_service.telemetry()
    assert snap.counts.get("docs_indexed") == 1 and snap.counts.get("queries_served") == 1, snap
    print(f"  PASS  AdminService.telemetry() -> counts={snap.counts}")
    print("\nADMIN TELEMETRY SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
