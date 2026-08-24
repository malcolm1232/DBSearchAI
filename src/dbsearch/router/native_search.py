"""NativeSearchStore — `mode: native` (ADR 0008): query the source's OWN search API
(Phase E E3b, card #113).

Zero copy, zero sync, no DBSearch index: the query goes live to Microsoft Graph Search
(`POST /search/query`) as the delegated user, the SOURCE ranks and ACL-trims, and hits
come back as Evidence. Because trimming happens source-side, retrieval is permission-
faithful by construction (LAW 2) — there is nothing on our side to leak. Scores are NOT
returned (rank order is the merge signal — ADR 0008 cross-family comparability).

SPIKE auth boundary: `token_provider(user_oid) -> bearer` is injected. The dev spike uses
`env_token_provider` (ONE identity for all callers — catalog gate #1 still trims per user,
but source-side row security sees the token's identity, so this is demo-grade only).
Production replaces the callable with the E5 OBO token exchange — same seam, no store
change. Vertex AI Search / Kendra arrive as sibling adapters behind the same StorePort.
"""
from __future__ import annotations

import os
from typing import Callable

from dbsearch.router.evidence import Evidence, RECORD
from dbsearch.router.provider import StoreProviderPort
from dbsearch.router.store import (
    AccessContext, NATIVE_SEARCH, SEMANTIC, StorePort, StoreProfile,
)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# transport(path, payload, token) -> parsed JSON dict. Injected so tests (and future
# adapters) never touch the network; the default is a stdlib urllib POST.
Transport = Callable[[str, dict, str], dict]
TokenProvider = Callable[[str], str]


def http_transport(path: str, payload: dict, token: str) -> dict:
    import json
    from urllib import request

    req = request.Request(
        GRAPH_BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=15) as resp:  # noqa: S310 — https to a fixed host
        return json.loads(resp.read().decode())


def env_token_provider(var: str = "GRAPH_TOKEN") -> TokenProvider:
    """Dev-spike delegation: one bearer for every caller, read lazily so a composed store
    without credentials fails at authorize-time — the E3 executor then DROPS it with a
    disclosure instead of the whole catalog failing at build."""

    def provide(user_oid: str) -> str:
        token = os.environ.get(var, "")
        if not token:
            # #816: this reaches customer-facing verdicts, so it names whose problem it is
            # and what the user can do instead - internal vocabulary (spike/E5/OBO) stays
            # in comments. E5 OBO replaces this env seam.
            raise RuntimeError(
                f"native Microsoft search is not configured on this deployment ({var} is "
                "operator-set, not self-serve). Use a SharePoint source instead, or ask "
                "the operator to configure native search")
        return token

    return provide


class GraphSearchStore(StorePort):
    def __init__(self, store_id: str, business_unit: str, title: str, description: str,
                 *, transport: Transport | None = None,
                 token_provider: TokenProvider | None = None,
                 topics: list[str] | None = None, entity_types: list[str] | None = None) -> None:
        self._store_id = store_id
        self._bu = business_unit
        self._title = title
        self._description = description
        self._transport = transport or http_transport
        self._tokens = token_provider or env_token_provider()
        self._topics = topics or []
        self._entity_types = entity_types or ["driveItem", "listItem"]

    def profile(self) -> StoreProfile:
        return StoreProfile(store_id=self._store_id, title=self._title,
                            description=self._description, kind=NATIVE_SEARCH,
                            capabilities={SEMANTIC}, business_unit=self._bu,
                            topics=list(self._topics), freshness="live",
                            proof_kind="document")   # #165: native hits carry doc/uri → DocProof

    def authorize(self, user_oid: str) -> AccessContext:
        # No principals list: authorization material is the delegated bearer — the SOURCE
        # applies its own ACLs to whatever identity that token carries (gate #2, ADR 0006).
        return AccessContext(user_oid=user_oid, principals=[],
                             delegated_credential=self._tokens(user_oid))

    def retrieve(self, access: AccessContext, question: str, top_k: int = 5) -> list[Evidence]:
        payload = {"requests": [{
            "entityTypes": list(self._entity_types),
            "query": {"queryString": question},
            "size": top_k,
        }]}
        body = self._transport("/search/query", payload, access.delegated_credential)
        out: list[Evidence] = []
        for value in body.get("value", []):
            for container in value.get("hitsContainers", []):
                for hit in container.get("hits", []):
                    resource = hit.get("resource", {}) or {}
                    out.append(Evidence(
                        store_id=self._store_id,
                        business_unit=self._bu,
                        kind=RECORD,
                        content=hit.get("summary", "") or resource.get("name", ""),
                        provenance={"doc": hit.get("hitId", ""),
                                    "title": resource.get("name", ""),
                                    "uri": resource.get("webUrl", ""),
                                    "locator": {}},
                        score=None,   # rank order is the signal (ADR 0008)
                    ))
                    if len(out) >= top_k:
                        return out
        return out


class GraphSearchProvider(StoreProviderPort):
    """`kind: graph_search` in stores.yml — the first real `mode: native` provider.
    probe() is network-free: the routing signal is the declared description/topics
    (a live capability probe can come with E9's mode negotiation)."""

    kind = "graph_search"
    modes = ("native",)         # ADR 0008: the source's own search API, zero ingestion

    def __init__(self, *, transport: Transport | None = None,
                 token_provider: TokenProvider | None = None) -> None:
        self._transport = transport
        self._tokens = token_provider

    def _make(self, config: dict) -> GraphSearchStore:
        return GraphSearchStore(
            store_id=config["id"],
            business_unit=config.get("business_unit", ""),
            title=config.get("title", config["id"]),
            description=config.get("description", ""),
            transport=self._transport,
            token_provider=self._tokens,
            topics=config.get("topics") or [],
            entity_types=config.get("entity_types") or None,
        )

    def probe(self, config: dict) -> StoreProfile:
        return self._make(config).profile()

    def build(self, config: dict) -> GraphSearchStore:
        return self._make(config)
