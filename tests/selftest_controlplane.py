"""Phase 2 exit test (ROADMAP): a minimal control plane over multiple tenants.

Proves:
  - Two tenants run fully ISOLATED data planes (LAW 5) — separate indexes, no leakage.
  - The control plane aggregates per-tenant metering + health (LAW 8) ...
  - ... with ZERO customer content crossing the wire — the boundary rejects any leak (LAW 1).
  - Release/config is pushed centrally and pulled by the in-tenant agent.
  - The air-gapped edition is a flag: data plane works, uplink sends nothing.

Dependency-free:  python3 tests/selftest_controlplane.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import (  # noqa: E402
    ExtractiveLlm,
    HashingEmbedding,
    InMemoryIdentity,
    InMemoryIndex,
    InMemoryObjectStore,
    InMemoryQueue,
    PlainTextExtractor,
)
from dbsearch.boundary.validator import BoundaryValidator, BoundaryViolation  # noqa: E402
from dbsearch.connectors.sharepoint import SharePointConnector  # noqa: E402
from dbsearch.controlplane import ControlPlane, DataPlaneAgent  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.query import QueryService  # noqa: E402

GLOBEX_SEED = [{
    "external_id": "globex-cloud-migration",
    "title": "Globex Cloud Migration Plan",
    "uri": "https://globex.sharepoint.com/plans/cloud-migration",
    "acl": ["grp-all-consultants"],
    "text": "Globex cloud migration plan: lift-and-shift then re-platform, AWS to Azure.",
}]


def build_data_plane(tenant_id, seed=None):
    """Stand up an isolated in-tenant data plane and return (index, query_service)."""
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    index = InMemoryIndex(store)  # index reads embeddings from this tenant's own store
    connector = SharePointConnector(tenant_id=tenant_id, seed=seed)
    run_ingestion(connector, queue, store, PlainTextExtractor(), HashingEmbedding(), index)
    identity = InMemoryIdentity({"u": ["grp-all-consultants"]})
    qs = QueryService(index, identity, HashingEmbedding(), ExtractiveLlm(), store, tenant_id=tenant_id)
    return index, qs


def main():
    print("Control-plane self-test (LAW 5 isolation · LAW 1 boundary · LAW 8 metering):")
    cp = ControlPlane()

    # --- Onboard two tenants, each its own isolated data plane (LAW 5) ---
    acme_index, acme_qs = build_data_plane("acme", seed=None)        # default 3-doc seed
    globex_index, globex_qs = build_data_plane("globex", seed=GLOBEX_SEED)
    cp.register_tenant("acme", release="2.3.1")
    cp.register_tenant("globex", release="2.3.0")

    assert acme_index is not globex_index
    acme_docs = {c.doc_external_id for c, _ in acme_index._items.values()}
    globex_docs = {c.doc_external_id for c, _ in globex_index._items.values()}
    assert acme_docs.isdisjoint(globex_docs), "tenants must not share documents"
    # A query in globex can never surface an acme doc — different data plane entirely.
    assert globex_qs.answer("u", "retail bank proposal").retrieved_docs == [] or \
        "prop-retail-bank-2025" not in globex_qs.answer("u", "retail bank proposal").retrieved_docs
    print(f"  PASS  two isolated tenants  ->  acme={sorted(acme_docs)}  globex={sorted(globex_docs)}")

    # --- Each tenant's in-tenant agent emits telemetry up (metadata only) ---
    acme_agent = DataPlaneAgent("acme", cp)
    globex_agent = DataPlaneAgent("globex", cp)
    acme_agent.emit("ingest.completed", counts={"docs_indexed": 3}, cost={"embed_tokens": 1500},
                    health={"index_pct": 1.0, "version": "2.3.1"}, ts="2026-06-25T10:00:00Z")
    globex_agent.emit("ingest.completed", counts={"docs_indexed": 1}, cost={"embed_tokens": 400},
                      health={"index_pct": 1.0, "version": "2.3.0"}, ts="2026-06-25T10:00:00Z")

    assert cp.metering("acme")["count:docs_indexed"] == 3
    assert cp.metering("globex")["count:docs_indexed"] == 1
    assert cp.health("acme")["version"] == "2.3.1" and cp.health("globex")["version"] == "2.3.0"
    print(f"  PASS  per-tenant metering+health  ->  acme cost={cp.metering('acme')}  globex health={cp.health('globex')}")

    # --- Central release push, pulled by the in-tenant agent ---
    cp.set_release("acme", "2.4.0")
    pulled = acme_agent.poll_release()
    assert pulled["release"] == "2.4.0" and acme_agent.installed_release == "2.4.0"
    print(f"  PASS  release pushed centrally + pulled in-tenant  ->  acme now {acme_agent.installed_release}")

    # --- A content leak attempt is REJECTED on the wire; nothing recorded (LAW 1) ---
    before = len(cp.audit_log)
    try:
        cp.receive_telemetry({"tenant_id": "acme", "event": "query.served",
                              "snippet": "Project Falcon merger terms...", "ts": "2026-06-25T10:01:00Z"})
        raise AssertionError("LAW 1 BREACH: control plane accepted content")
    except BoundaryViolation:
        pass
    assert len(cp.audit_log) == before and len(cp.violations) == 1
    # The agent itself also refuses to send content before it leaves the tenant:
    try:
        acme_agent.emit("ingest.completed", counts={"docs_indexed": 1, "title": "secret.pptx"})
        raise AssertionError("LAW 1 BREACH: agent sent content")
    except BoundaryViolation:
        pass
    print(f"  PASS  content rejected both sides  ->  violations logged (reason only)={cp.violations}")

    # --- Audit: re-validate everything that DID cross — all metadata, zero content ---
    v = BoundaryValidator()
    assert all(v.validate(p) for p in cp.audit_log)
    print(f"  PASS  audit clean: {len(cp.audit_log)} payloads crossed, all boundary-valid (no content)")

    # --- Air-gapped 3rd tenant: data plane works, uplink sends nothing ---
    initech_index, initech_qs = build_data_plane("initech", seed=None)
    cp.register_tenant("initech", release="2.3.1")
    initech_agent = DataPlaneAgent("initech", cp, air_gapped=True)
    sent = initech_agent.emit("ingest.completed", counts={"docs_indexed": 3}, ts="2026-06-25T10:02:00Z")
    served = initech_qs.answer("u", "healthcare sap migration").retrieved_docs
    assert sent is False and cp.metering("initech") == {} and "healthcare-sap-case" in served
    print(f"  PASS  air-gapped tenant serves locally, uplink off  ->  sent={sent}, served={served}")

    print("\nALL CONTROL-PLANE SELF-TESTS PASSED — isolated tenants, central ops, zero content crossed.")


if __name__ == "__main__":
    main()
