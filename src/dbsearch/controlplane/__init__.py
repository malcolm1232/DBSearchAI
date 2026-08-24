"""Control plane (runs in YOUR Azure) + the in-tenant uplink agent.

The control plane NEVER receives customer content — only boundary-validated telemetry
(LAW 1, ADR 0002). It exists to push releases/config down and aggregate metering/health
up. Each tenant is tracked in isolation (LAW 5). The DataPlaneAgent is the narrow,
validated bridge; setting `air_gapped=True` severs the uplink entirely (the air-gapped
edition is a flag, not a fork).
"""
from dbsearch.controlplane.agent import DataPlaneAgent
from dbsearch.controlplane.plane import ControlPlane

__all__ = ["ControlPlane", "DataPlaneAgent"]
