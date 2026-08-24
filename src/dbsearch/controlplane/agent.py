"""DataPlaneAgent — the narrow uplink that runs INSIDE the customer tenant.

It is the only thing allowed to talk to the control plane. Defense in depth: it
validates every payload against the boundary contract BEFORE it leaves the tenant, so
even a buggy caller cannot exfiltrate content (LAW 1). `air_gapped=True` turns the
uplink off entirely — the data plane keeps working, nothing crosses (air-gapped edition).
"""
from __future__ import annotations

from dbsearch.boundary.validator import BoundaryValidator
from dbsearch.controlplane.plane import ControlPlane


class DataPlaneAgent:
    def __init__(self, tenant_id: str, control_plane: ControlPlane, air_gapped: bool = False) -> None:
        self.tenant_id = tenant_id
        self.air_gapped = air_gapped
        self._cp = control_plane
        self._validator = BoundaryValidator()
        self.installed_release: str | None = None

    def emit(
        self,
        event: str,
        counts: dict | None = None,
        cost: dict | None = None,
        health: dict | None = None,
        ts: str = "1970-01-01T00:00:00Z",
    ) -> bool:
        """Send telemetry up. Returns False if air-gapped (nothing sent). Raises
        BoundaryViolation if the payload would leak content — before it leaves the tenant."""
        payload: dict = {"tenant_id": self.tenant_id, "event": event, "ts": ts}
        if counts:
            payload["counts"] = counts
        if cost:
            payload["cost"] = cost
        if health:
            payload["health"] = health

        self._validator.validate(payload)  # local guard — raises before anything crosses
        if self.air_gapped:
            return False                    # uplink severed; data plane still fully functional
        self._cp.receive_telemetry(payload)
        return True

    def poll_release(self) -> dict:
        """Pull desired version/config from the control plane and 'install' it."""
        if self.air_gapped:
            raise RuntimeError("air-gapped tenant: releases are applied manually, not pulled")
        desired = self._cp.get_release(self.tenant_id)
        self.installed_release = desired["release"]
        return desired
