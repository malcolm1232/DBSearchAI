"""AdminService — assembles operator-facing METADATA from the data-plane ports.

LAW 1: returns counts + identifiers (title/uri/oid) only, never chunk content. The
boundary validator is NOT applied here (it guards the control-plane uplink); the admin
HTTP tests assert no content-body keys instead. LAW 5: everything is scoped to tenant_id.
LAW 2: permission_test reuses the same expand_groups ∩ allowed_principals trim as queries.
"""
from __future__ import annotations

from dataclasses import dataclass

from dbsearch.connectors.registry import SourceRegistry, SourceSummary
from dbsearch.controlplane.plane import ControlPlane
from dbsearch.ports.base import IdentityPort, IndexPort, as_read_scope


@dataclass
class IndexHealth:
    backend: str
    chunk_count: int
    doc_count: int
    embedding_model: str
    embedding_dim: int
    last_index_ts: str | None


@dataclass
class IdentitySummary:
    users: list      # [{principal_oid, kind, group_oids:[...]}]
    groups: list     # [{group_oid, member_count, doc_count}]


@dataclass
class PermissionTestRow:
    doc_external_id: str
    title: str
    uri: str
    returned: bool
    matched_principals: list[str]


@dataclass
class PermissionTestResult:
    user_oid: str
    expanded_principals: list[str]
    results: list[PermissionTestRow]
    authorized_count: int
    denied_count: int


@dataclass
class TelemetrySnapshot:
    tenant: str
    counts: dict
    cost: dict
    health: dict


class AdminService:
    def __init__(self, index: IndexPort, identity: IdentityPort, control_plane: ControlPlane,
                 tenant_id: str, backend: str, embedding_model: str,
                 source_registry: SourceRegistry | None = None) -> None:
        self._index = index
        self._identity = identity
        self._cp = control_plane
        self._tenant_id = tenant_id
        self._backend = backend
        self._embedding_model = embedding_model
        self._registry = source_registry

    def sources(self) -> list[SourceSummary]:
        """Metadata-only list of connected sources (LAW 1). Empty if no registry wired."""
        return self._registry.list_sources() if self._registry is not None else []

    def index_health(self) -> IndexHealth:
        s = self._index.stats(self._tenant_id)
        health = self._cp.health(self._tenant_id) or {}
        return IndexHealth(
            backend=self._backend, chunk_count=s.chunk_count, doc_count=s.doc_count,
            embedding_model=self._embedding_model, embedding_dim=s.embedding_dim,
            last_index_ts=health.get("last_index_ts"),
        )

    def identities(self) -> IdentitySummary:
        d = self._identity.list_principals()
        doc_count: dict[str, int] = {}
        # The admin metadata plane is not a sharing surface: own partition, no doorway.
        for da in self._index.list_doc_acls(as_read_scope(self._tenant_id)):
            for p in da.allowed_principals:
                doc_count[p] = doc_count.get(p, 0) + 1
        users = [{"principal_oid": u, "kind": "user", "group_oids": list(gs)}
                 for u, gs in sorted(d.users.items())]
        groups = [{"group_oid": g,
                   "member_count": sum(1 for gs in d.users.values() if g in gs),
                   "doc_count": doc_count.get(g, 0)}
                  for g in d.groups]
        return IdentitySummary(users=users, groups=groups)

    def permission_test(self, user_oid: str, question: str = "") -> PermissionTestResult:
        # Permission is ACL-only (independent of query text): a doc is returned iff the
        # user's expanded principals intersect its allowed_principals (LAW 2).
        pset = set(self._identity.expand_groups(user_oid))
        rows: list[PermissionTestRow] = []
        # The admin metadata plane is not a sharing surface: own partition, no doorway.
        for da in self._index.list_doc_acls(as_read_scope(self._tenant_id)):
            matched = sorted(pset & set(da.allowed_principals))
            rows.append(PermissionTestRow(
                doc_external_id=da.doc_external_id, title=da.title, uri=da.uri,
                returned=bool(matched), matched_principals=matched,
            ))
        authorized = sum(1 for r in rows if r.returned)
        return PermissionTestResult(
            user_oid=user_oid, expanded_principals=sorted(pset), results=rows,
            authorized_count=authorized, denied_count=len(rows) - authorized,
        )

    def telemetry(self) -> TelemetrySnapshot:
        m = self._cp.metering(self._tenant_id)
        counts = {k[len("count:"):]: v for k, v in m.items() if k.startswith("count:")}
        cost = {k[len("cost:"):]: v for k, v in m.items() if k.startswith("cost:")}
        return TelemetrySnapshot(tenant=self._tenant_id, counts=counts, cost=cost,
                                 health=self._cp.health(self._tenant_id) or {})
