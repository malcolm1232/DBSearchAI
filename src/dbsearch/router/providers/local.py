"""LocalIndexProvider — an in-tenant provider that ingests seed docs into a fresh
in-memory index and returns an IndexedStore (Phase E, card #98).

This is the E1 proof of the provider seam: probe/build work end-to-end with zero external
dependencies. Cloud providers (bigquery/redshift/azure_sql, E9) implement the SAME
StoreProviderPort against real connection config instead of an in-memory seed.

config = {
    id, business_unit, title, description,
    seed: [{external_id, title, uri, acl:[...], text}],
    user_groups: {user_oid: [group_oid, ...]},
}
"""
from __future__ import annotations

from dbsearch.adapters.local import (
    ExtractiveLlm, HashingEmbedding, InMemoryIdentity, InMemoryIndex,
    InMemoryObjectStore, InMemoryQueue, PlainTextExtractor,
)
from dbsearch.connectors.sharepoint import SharePointConnector
from dbsearch.pipeline.runner import run_ingestion
from dbsearch.query import QueryService
from dbsearch.router.indexed_store import IndexedStore
from dbsearch.router.provider import StoreProviderPort
from dbsearch.router.store import INDEXED, SEMANTIC, StoreProfile


class LocalIndexProvider(StoreProviderPort):
    kind = "local"
    modes = ("index",)          # ADR 0008: derived in-tenant index

    def __init__(self, embedder=None, llm=None, floor_vector_rescue: float = 0.0) -> None:
        # #143: accept the edition's embedder/LLM instead of hardcoding lexical HashingEmbedding
        # + ExtractiveLlm. Defaults preserve the zero-dependency behaviour (memory backend).
        # A real semantic embedder passes floor_vector_rescue>0 so pure-semantic hits (no shared
        # keywords) survive the lexical relevance floor.
        self._embedder = embedder
        self._llm = llm
        self._floor_vector_rescue = floor_vector_rescue

    def probe(self, config: dict) -> StoreProfile:
        return StoreProfile(
            store_id=config["id"],
            title=config.get("title", config["id"]),
            description=config.get("description", ""),
            kind=INDEXED,
            capabilities={SEMANTIC},
            business_unit=config.get("business_unit", ""),
        )

    def build(self, config: dict) -> IndexedStore:
        obj = InMemoryObjectStore()
        index = InMemoryIndex(obj)
        embedder = self._embedder or HashingEmbedding()
        run_ingestion(
            SharePointConnector(tenant_id=config["id"], seed=config.get("seed", [])),
            InMemoryQueue(), obj, PlainTextExtractor(), embedder, index,
        )
        identity = InMemoryIdentity(config.get("user_groups", {}))
        qs = QueryService(index, identity, embedder, self._llm or ExtractiveLlm(), obj,
                          floor_vector_rescue=self._floor_vector_rescue,
                          tenant_id=config["id"])  # matches the connector's ingest stamp
        return IndexedStore(config["id"], config.get("business_unit", ""),
                            config.get("title", config["id"]),
                            config.get("description", ""), qs, identity)
