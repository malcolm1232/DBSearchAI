"""StorePort — the uniform face over every queryable store (Phase E, card #98).

A store is INDEXED (DBSearch owns a vector index; IndexedStore adapter) or FEDERATED
(queried in place; E4/E5). The router only ever talks to this interface, so adding a
cloud warehouse later is a new adapter, not a core change (LAW 7).

authorize() -> retrieve() is the permission seam: authorize() resolves the caller into
an AccessContext (principals for indexed; delegated creds / row-policy for federated in
E5); retrieve() uses it. No store may retrieve un-authorized (LAW 2).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from dbsearch.router.evidence import Evidence

# capabilities — what kinds of question a store can answer
SEMANTIC = "semantic"        # fuzzy / natural-language lookup (vector)
ANALYTICAL = "analytical"    # aggregate / filter / join (NL2SQL, E4)
EXACT = "exact"              # point / record lookup by key

# store kinds
INDEXED = "indexed"
FEDERATED_SQL = "federated_sql"
FEDERATED_DOC = "federated_doc"
NATIVE_SEARCH = "native_search"   # ADR 0008 `mode: native` — the source's own search API


@dataclass
class StoreProfile:
    """What a store IS — metadata the router (E2) and NL2SQL (E4) consume. profile_vector
    (E2) and schema (E4) are filled by later slices; E1 needs only the descriptive fields."""
    store_id: str
    title: str
    description: str
    kind: str                                  # INDEXED | FEDERATED_SQL | FEDERATED_DOC
    capabilities: set[str] = field(default_factory=set)
    business_unit: str = ""
    topics: list[str] = field(default_factory=list)   # E2 routing signal (keywords)
    profile_vector: list[float] | None = None  # E2 routing
    schema: list[dict] | None = None           # E4 structured
    freshness: str = "live"
    proof_kind: str = ""                        # #165: sql | document | native — how this store proves answers
    origin: dict | None = None                  # #176: {system, location} — human source origin
    #: #808: things TRUE of a store that composed successfully and are worth saying out loud.
    #: Distinct from `skipped`, which means the store was refused and never built — a warned
    #: store IS live and queryable, it just has something wrong with it that only its owner
    #: can fix. Empty for every healthy store, so the canvas shows nothing by default.
    warnings: list[str] = field(default_factory=list)


@dataclass
class AccessContext:
    """Output of StorePort.authorize(user). Carries the permission material a store trims
    by. Indexed -> principals; federated (E5) -> delegated_credential or row_policy. Never
    bypassable: retrieve() takes this, never a raw un-trimmed query."""
    user_oid: str
    principals: list[str] = field(default_factory=list)
    delegated_credential: object | None = None   # E5 federated
    row_policy: str | None = None                # E5 federated fallback


class StorePort(ABC):
    """Uniform interface over indexed + federated stores (LAW 7)."""

    @abstractmethod
    def profile(self) -> StoreProfile: ...

    def capabilities(self) -> set[str]:
        return self.profile().capabilities

    @abstractmethod
    def authorize(self, user_oid: str) -> AccessContext:
        """Resolve the caller into the permission material this store trims by."""

    @abstractmethod
    def retrieve(self, access: AccessContext, question: str, top_k: int = 5) -> list[Evidence]:
        """Return ONLY evidence this access context is entitled to (LAW 2)."""
