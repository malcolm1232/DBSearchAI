"""The control plane: release/config registry + metering/health aggregation.

Every inbound payload is run through the BoundaryValidator BEFORE anything is recorded.
A payload that carries content is rejected, logged as a violation by reason only (never
stored), and never enters metering or the audit log (LAW 1). All state is keyed by
tenant_id with no cross-tenant sharing (LAW 5).
"""
from __future__ import annotations

from collections import defaultdict

from dbsearch.boundary.validator import BoundaryValidator, BoundaryViolation


class ControlPlane:
    def __init__(self) -> None:
        self._validator = BoundaryValidator()
        self._tenants: dict[str, dict] = {}                       # tenant -> {release, config}
        self._metering: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._health: dict[str, dict] = {}
        self.audit_log: list[dict] = []                           # accepted (metadata-only) payloads
        self.violations: list[dict] = []                          # rejected attempts (reason only)

    # --- release / config push (control plane -> data plane) ---
    def register_tenant(self, tenant_id: str, release: str, config: dict | None = None) -> None:
        self._tenants[tenant_id] = {"release": release, "config": config or {}}

    def set_release(self, tenant_id: str, release: str, config: dict | None = None) -> None:
        t = self._tenants[tenant_id]
        t["release"] = release
        if config is not None:
            t["config"] = config

    def get_release(self, tenant_id: str) -> dict:
        """What an in-tenant agent pulls down on poll."""
        return dict(self._tenants[tenant_id])

    # --- telemetry intake (data plane -> control plane) ---
    def receive_telemetry(self, payload: dict) -> bool:
        try:
            self._validator.validate(payload)
        except BoundaryViolation as e:
            # Record the REASON only. The offending payload never enters our systems (LAW 1).
            self.violations.append({"tenant_id": payload.get("tenant_id", "?"), "reason": str(e)})
            raise
        tid = payload["tenant_id"]
        for k, v in payload.get("counts", {}).items():
            self._metering[tid][f"count:{k}"] += v
        for k, v in payload.get("cost", {}).items():
            self._metering[tid][f"cost:{k}"] += v
        if "health" in payload:
            self._health[tid] = payload["health"]
        self.audit_log.append(payload)
        return True

    # --- read views (for billing dashboards, ops) ---
    def metering(self, tenant_id: str) -> dict:
        return dict(self._metering.get(tenant_id, {}))

    def health(self, tenant_id: str) -> dict | None:
        return self._health.get(tenant_id)

    def tenants(self) -> list[str]:
        return list(self._tenants)
