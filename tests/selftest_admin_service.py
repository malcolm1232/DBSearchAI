"""Unit tests for the Phase 2 admin read surface: new port methods (Task 1) and the
AdminService (Task 2). In-process, no HTTP. Run: python3 tests/selftest_admin_service.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.ports.base import ReadScope  # noqa: E402

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIdentity, InMemoryIndex, InMemoryObjectStore, PlainTextExtractor,
)
from dbsearch.connectors.sharepoint import SharePointConnector  # noqa: E402
from dbsearch.core.models import DocACL, IndexStats, PrincipalDirectory  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.adapters.local import InMemoryQueue  # noqa: E402

TENANT = "selfhost"


def _fresh_index():
    """A populated in-memory index: handbook (all-staff) + falcon (deal-team)."""
    store = InMemoryObjectStore()
    index = InMemoryIndex(store)
    embedder = HashingEmbedding()
    seed = [
        {"external_id": "public-handbook", "title": "Staff Handbook",
         "uri": "https://example/handbook", "acl": ["all-staff"], "text": "holidays expenses onboarding"},
        {"external_id": "deal-falcon", "title": "Project Falcon — Confidential",
         "uri": "https://example/falcon", "acl": ["deal-team"], "text": "confidential falcon valuation"},
    ]
    conn = SharePointConnector(tenant_id=TENANT, seed=seed)
    run_ingestion(conn, InMemoryQueue(), store, PlainTextExtractor(), embedder, index)
    return index


def test_ports():
    index = _fresh_index()
    identity = InMemoryIdentity({"alice": ["all-staff", "deal-team"], "bob": ["all-staff"]})

    s = index.stats(TENANT)
    assert isinstance(s, IndexStats), s
    assert s.doc_count == 2, s
    assert s.chunk_count == 2, s          # phase-1 chunking = 1 chunk/doc
    assert s.embedding_dim == 128, s      # HashingEmbedding default dim

    acls = index.list_doc_acls(ReadScope(TENANT))
    assert {a.doc_external_id for a in acls} == {"public-handbook", "deal-falcon"}, acls
    falcon = next(a for a in acls if a.doc_external_id == "deal-falcon")
    assert isinstance(falcon, DocACL) and falcon.allowed_principals == ["deal-team"], falcon
    assert falcon.title == "Project Falcon — Confidential", falcon

    d = identity.list_principals()
    assert isinstance(d, PrincipalDirectory), d
    assert set(d.users) == {"alice", "bob"}, d
    assert set(d.groups) == {"all-staff", "deal-team"}, d
    print("  PASS  ports: stats / list_doc_acls / list_principals")


def test_service():
    from dbsearch.admin.service import AdminService
    from dbsearch.controlplane.plane import ControlPlane

    index = _fresh_index()
    identity = InMemoryIdentity({"alice": ["all-staff", "deal-team"], "bob": ["all-staff"]})
    svc = AdminService(index, identity, ControlPlane(), TENANT, "memory", "hashing-embedding (dev)")

    h = svc.index_health()
    assert h.doc_count == 2 and h.chunk_count == 2, h
    assert h.backend == "memory" and h.embedding_dim == 128, h
    assert h.embedding_model == "hashing-embedding (dev)", h
    assert h.last_index_ts is None, h     # nothing emitted yet

    ids = svc.identities()
    alice = next(u for u in ids.users if u["principal_oid"] == "alice")
    assert set(alice["group_oids"]) == {"all-staff", "deal-team"}, alice
    deal = next(g for g in ids.groups if g["group_oid"] == "deal-team")
    assert deal["member_count"] == 1 and deal["doc_count"] == 1, deal

    # LAW 2 through the admin tool: alice sees falcon, bob is denied with empty intersection
    pa = svc.permission_test("alice", "falcon")
    fa = next(r for r in pa.results if r.doc_external_id == "deal-falcon")
    assert fa.returned is True and fa.matched_principals == ["deal-team"], fa
    assert pa.authorized_count == 2 and pa.denied_count == 0, pa

    pb = svc.permission_test("bob", "falcon")
    fb = next(r for r in pb.results if r.doc_external_id == "deal-falcon")
    assert fb.returned is False and fb.matched_principals == [], fb
    assert pb.authorized_count == 1 and pb.denied_count == 1, pb
    print("  PASS  service: index_health / identities / permission_test (LAW 2)")


def main():
    print("Admin service unit self-test:")
    test_ports()
    test_service()
    print("\nADMIN SERVICE UNIT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
