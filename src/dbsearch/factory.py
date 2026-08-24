"""Factory — assemble the data-plane adapters for the selected backend.

This is the ONLY place that knows whether we're running local or on Azure. The
pipeline (runner.py) and query service (query/service.py) are identical for both —
that's LAW 7 / ADR 0004 paying off: going live is a wiring change, not a rewrite.

Azure SDKs are imported lazily inside the azure branch, so `backend=local` (tests,
this repo's CI) never needs them installed.
"""
from __future__ import annotations

from dataclasses import dataclass

from dbsearch.config import Settings
from dbsearch.ports.base import (
    ConnectorPort,
    EmbeddingPort,
    ExtractorPort,
    IdentityPort,
    IndexPort,
    LlmPort,
    ObjectStorePort,
    QueuePort,
)
from dbsearch.query import QueryService


@dataclass
class DataPlane:
    store: ObjectStorePort
    queue: QueuePort
    extractor: ExtractorPort
    embedder: EmbeddingPort
    index: IndexPort
    identity: IdentityPort
    llm: LlmPort
    connector: ConnectorPort
    tenant_id: str

    def query_service(self, top_k: int = 5) -> QueryService:
        return QueryService(self.index, self.identity, self.embedder, self.llm, self.store,
                            top_k=top_k, tenant_id=self.tenant_id)


def build_data_plane(settings: Settings) -> DataPlane:
    if settings.backend == "local":
        return _build_local(settings)
    if settings.backend == "azure":
        return _build_azure(settings)
    raise ValueError(f"unknown backend: {settings.backend!r}")


def _build_local(settings: Settings) -> DataPlane:
    from dbsearch.adapters.local import (
        ExtractiveLlm,
        HashingEmbedding,
        InMemoryIdentity,
        InMemoryIndex,
        InMemoryObjectStore,
        InMemoryQueue,
        PlainTextExtractor,
    )
    from dbsearch.connectors.sharepoint import SharePointConnector

    store = InMemoryObjectStore()
    return DataPlane(
        store=store,
        queue=InMemoryQueue(),
        extractor=PlainTextExtractor(),
        embedder=HashingEmbedding(),
        index=InMemoryIndex(store),
        identity=InMemoryIdentity({}),
        llm=ExtractiveLlm(),
        connector=SharePointConnector(tenant_id=settings.tenant_id),
        tenant_id=settings.tenant_id,
    )


def _build_azure(settings: Settings) -> DataPlane:
    from azure.identity import DefaultAzureCredential

    from dbsearch.adapters.azure.blob import AzureBlobStore
    from dbsearch.adapters.azure.docintel import DocIntelligenceExtractor
    from dbsearch.adapters.azure.entra import EntraIdentity
    from dbsearch.adapters.azure.servicebus import ServiceBusQueue
    from dbsearch.connectors.sharepoint_graph import GraphSharePointConnector

    cred = DefaultAzureCredential()
    # Blob may use a shared account key (settings.blob_account_key) to avoid the multi-minute
    # data-plane RBAC propagation; falls back to AAD (managed identity in AKS) when unset.
    blob_cred = settings.blob_account_key or cred
    store = AzureBlobStore(settings.blob_account_url, settings.blob_container, blob_cred)
    embedder, llm = _build_models(settings)
    return DataPlane(
        store=store,
        queue=ServiceBusQueue(settings.servicebus_namespace, cred),
        extractor=DocIntelligenceExtractor(settings.docintel_endpoint, cred),
        embedder=embedder,
        index=_build_index(settings, store, cred),
        identity=EntraIdentity(settings.az_tenant_id, settings.client_id, settings.client_secret),
        llm=llm,
        connector=GraphSharePointConnector(
            settings.tenant_id, settings.sharepoint_drive_id,
            settings.az_tenant_id, settings.client_id, settings.client_secret,
        ),
        tenant_id=settings.tenant_id,
    )


def _build_index(settings: Settings, store: ObjectStorePort, cred) -> IndexPort:
    """Swap the index without touching the pipeline/query code (LAW 7)."""
    if settings.index_backend == "pgvector":
        from dbsearch.adapters.vectordb import PgVectorIndex

        return PgVectorIndex(settings.pgvector_dsn, store, dim=settings.embedding_dim)
    from azure.core.credentials import AzureKeyCredential

    from dbsearch.adapters.azure.aisearch import AISearchIndex

    return AISearchIndex(
        settings.search_endpoint, settings.search_index,
        AzureKeyCredential(settings.search_admin_key), store, embedding_dim=settings.embedding_dim,
    )


def _build_models(settings: Settings) -> tuple[EmbeddingPort, LlmPort]:
    """Swap the model provider (Azure OpenAI <-> in-tenant LLaMA) behind the ports (LAW 9)."""
    if settings.model_backend == "llama":
        from dbsearch.adapters.llama import LlamaEmbedding, LlamaLlm

        return (
            LlamaEmbedding(settings.llama_base_url, settings.llama_embed_model),
            LlamaLlm(settings.llama_base_url, settings.llama_chat_model),
        )
    from dbsearch.adapters.azure.aoai import AzureOpenAIEmbedding, AzureOpenAILlm

    return (
        AzureOpenAIEmbedding(settings.aoai_endpoint, settings.aoai_api_key, settings.aoai_embedding_deployment),
        AzureOpenAILlm(settings.aoai_endpoint, settings.aoai_api_key, settings.aoai_chat_deployment),
    )
