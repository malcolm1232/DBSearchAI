"""StoreProviderPort — the 'compose for databases' provisioning seam (Phase E, card #98).

One provider per store KIND knows how to connect + introspect (probe -> StoreProfile) and
to build a ready StorePort (build -> StorePort). Providers live in a registry, mirroring the
extractor registry, so adding a cloud warehouse (E9) is a drop-in, not a core change (LAW 7).

Mode-aware since #111 (ADR 0008): every provider DECLARES which retrieval modes it serves
(`pushdown | native | index`), and one kind may be served by different providers per mode —
e.g. `kind: sharepoint, mode: native` -> Graph Search, `mode: index` -> the document-connector
rail. The registry resolves (kind, mode); an unsupported mode fails honestly, never silently.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from dbsearch.router.store import StorePort, StoreProfile


class StoreProviderPort(ABC):
    kind: str = ""                    # subclasses set e.g. "local", "bigquery", "azure_sql"
    modes: tuple[str, ...] = ()       # ADR 0008 modes this provider serves — MUST declare
    default_mode: str = ""            # used when the manifest omits `mode` (defaults to modes[0])

    @abstractmethod
    def probe(self, config: dict) -> StoreProfile:
        """Connect with config (env already resolved) + auto-introspect -> profile.
        Side-effect-free: the canvas Test-connection calls this, so it must never ingest."""

    @abstractmethod
    def build(self, config: dict) -> StorePort:
        """Return a ready StorePort adapter for this source."""


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, dict[str, StoreProviderPort]] = {}   # kind -> mode -> provider
        self._defaults: dict[str, str] = {}                             # kind -> default mode

    def register(self, provider: StoreProviderPort) -> None:
        if not provider.modes:
            raise ValueError(
                f"provider for kind {provider.kind!r} declares no modes — every provider "
                "must state which retrieval modes it serves (ADR 0008)")
        by_mode = self._providers.setdefault(provider.kind, {})
        for mode in provider.modes:
            by_mode[mode] = provider
        # the first-registered provider's default wins for the bare-kind lookup
        self._defaults.setdefault(provider.kind, provider.default_mode or provider.modes[0])

    def get(self, kind: str, mode: str | None = None) -> StoreProviderPort:
        if kind not in self._providers:
            raise KeyError(f"no store provider registered for kind {kind!r}")
        by_mode = self._providers[kind]
        if mode is None:
            return by_mode[self._defaults[kind]]
        if mode not in by_mode:
            raise KeyError(
                f"kind {kind!r} does not support mode {mode!r} "
                f"(supported: {sorted(by_mode)})")
        return by_mode[mode]

    def modes_for(self, kind: str) -> list[str]:
        return sorted(self._providers.get(kind, {}))


# module-level default registry (like the extractor registry)
_DEFAULT = ProviderRegistry()


def register_provider(provider: StoreProviderPort) -> None:
    _DEFAULT.register(provider)


def get_provider(kind: str, mode: str | None = None) -> StoreProviderPort:
    return _DEFAULT.get(kind, mode)
