"""Settings — read the data-plane configuration from environment.

`DBSEARCH_BACKEND` selects "local" (in-memory, for tests/dev) or "azure" (real).
Everything else is only needed when backend=azure. See .env.example + docs/DEPLOY_AZURE.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    backend: str
    tenant_id: str  # OUR opaque tenant id (data-residency key, not a customer name)

    # Azure resource endpoints
    blob_account_url: str = ""
    blob_container: str = "dbsearch"
    blob_account_key: str = ""   # optional: shared-key auth (avoids waiting on data-plane RBAC)
    servicebus_namespace: str = ""          # <ns>.servicebus.windows.net
    search_endpoint: str = ""
    search_index: str = "chunks"
    search_admin_key: str = ""
    aoai_endpoint: str = ""
    aoai_api_key: str = ""
    aoai_embedding_deployment: str = "text-embedding-3-small"
    aoai_chat_deployment: str = "gpt-4o-mini"
    embedding_dim: int = 1536
    docintel_endpoint: str = ""
    keyvault_url: str = ""

    # Entra app registration (for Graph: identity + SharePoint connector)
    az_tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""

    # SharePoint source
    sharepoint_drive_id: str = ""

    # Swappable backends (all behind the same ports — LAW 7)
    index_backend: str = "aisearch"     # aisearch | pgvector
    pgvector_dsn: str = ""              # e.g. postgresql://user:pass@host:5432/db
    model_backend: str = "azure"        # azure | llama
    llama_base_url: str = ""           # OpenAI-compatible endpoint inside the tenant
    llama_chat_model: str = ""
    llama_embed_model: str = ""

    @staticmethod
    def from_env() -> "Settings":
        e = os.environ.get
        return Settings(
            backend=e("DBSEARCH_BACKEND", "local"),
            tenant_id=e("DBSEARCH_TENANT_ID", "dev-tenant"),
            blob_account_url=e("AZURE_BLOB_ACCOUNT_URL", ""),
            blob_container=e("AZURE_BLOB_CONTAINER", "dbsearch"),
            blob_account_key=e("AZURE_BLOB_ACCOUNT_KEY", ""),
            servicebus_namespace=e("AZURE_SERVICEBUS_NAMESPACE", ""),
            search_endpoint=e("AZURE_SEARCH_ENDPOINT", ""),
            search_index=e("AZURE_SEARCH_INDEX", "chunks"),
            search_admin_key=e("AZURE_SEARCH_ADMIN_KEY", ""),
            aoai_endpoint=e("AZURE_OPENAI_ENDPOINT", ""),
            aoai_api_key=e("AZURE_OPENAI_API_KEY", ""),
            aoai_embedding_deployment=e("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"),
            aoai_chat_deployment=e("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o-mini"),
            embedding_dim=int(e("DBSEARCH_EMBEDDING_DIM", "1536")),
            docintel_endpoint=e("AZURE_DOCINTEL_ENDPOINT", ""),
            keyvault_url=e("AZURE_KEYVAULT_URL", ""),
            az_tenant_id=e("AZURE_TENANT_ID", ""),
            client_id=e("AZURE_CLIENT_ID", ""),
            client_secret=e("AZURE_CLIENT_SECRET", ""),
            sharepoint_drive_id=e("SHAREPOINT_DRIVE_ID", ""),
            index_backend=e("DBSEARCH_INDEX_BACKEND", "aisearch"),
            pgvector_dsn=e("PGVECTOR_DSN", ""),
            model_backend=e("DBSEARCH_MODEL_BACKEND", "azure"),
            llama_base_url=e("LLAMA_BASE_URL", ""),
            llama_chat_model=e("LLAMA_CHAT_MODEL", ""),
            llama_embed_model=e("LLAMA_EMBED_MODEL", ""),
        )
