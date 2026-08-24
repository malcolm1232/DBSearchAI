"""AISearchIndex — IndexPort backed by Azure AI Search, with MANDATORY security
trimming (LAW 2). This is the most security-critical adapter in the system.

Requires: pip install azure-search-documents azure-identity

The trim is an OData filter applied INSIDE search() — the query service passes the
user's principal set and the filter keeps only chunks whose `allowed_principals`
intersect it. There is no code path that returns an untrimmed result.

    allowed_principals/any(p: search.in(p, '<oid1>,<oid2>,...', ','))

`ensure_index()` creates the index schema (idempotent). Embeddings + content are read
from the in-tenant object store by reference at upsert and uploaded to the index doc.
"""
from __future__ import annotations

import base64
import json

from dbsearch.core.models import Chunk
from dbsearch.ports.base import IndexPort, ObjectStorePort


def _safe_key(chunk_id: str) -> str:
    """AI Search document keys allow only [A-Za-z0-9_-=]. Real chunk/SharePoint ids can
    contain other characters (e.g. '#'), so encode deterministically to a safe key."""
    return base64.urlsafe_b64encode(chunk_id.encode()).decode()


def _doc_key(tenant_id: str, chunk_id: str) -> str:
    """#916: the AI Search document key, derived from (tenant_id, chunk_id) - the same
    identity contract as InMemoryIndex and the pgvector composite PK. chunk_id alone is
    NOT an identity: upload chunk ids are content-addressed, so two tenants uploading the
    same bytes mint identical chunk_ids, and upload_documents replaces the whole document
    by key - tenant field included - handing one tenant's row to the other.

    U+001F (unit separator) joins the parts: it can appear in neither a tenant id nor a
    chunk id, so the encoding cannot collide two distinct pairs.

    NOTE for established AI Search deployments (none live today - prod is pgvector):
    documents indexed under the old chunk-only keys keep those keys, so a re-ingest after
    this change would sit BESIDE them, not replace them. Reindex (delete each doc via
    delete(), then re-ingest) when upgrading a live AI Search index."""
    return _safe_key(f"{tenant_id}\x1f{chunk_id}")


class AISearchIndex(IndexPort):
    def __init__(self, endpoint: str, index_name: str, credential, store: ObjectStorePort, embedding_dim: int = 1536) -> None:
        from azure.search.documents import SearchClient

        self._endpoint = endpoint
        self._index_name = index_name
        self._credential = credential
        self._store = store
        self._dim = embedding_dim
        self._client = SearchClient(endpoint=endpoint, index_name=index_name, credential=credential)

    def ensure_index(self) -> None:
        """Create the index if it doesn't exist. Run once at provisioning time."""
        from azure.search.documents.indexes import SearchIndexClient
        from azure.search.documents.indexes.models import (
            HnswAlgorithmConfiguration,
            SearchableField,
            SearchField,
            SearchFieldDataType,
            SearchIndex,
            SimpleField,
            VectorSearch,
            VectorSearchProfile,
        )

        fields = [
            SimpleField(name="chunk_id", type=SearchFieldDataType.String, key=True, filterable=True),
            SimpleField(name="tenant_id", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="doc_external_id", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="text_ref", type=SearchFieldDataType.String),
            SimpleField(name="title", type=SearchFieldDataType.String),
            SimpleField(name="uri", type=SearchFieldDataType.String),
            SearchableField(name="content", type=SearchFieldDataType.String),  # hybrid keyword
            SearchField(
                name="allowed_principals",
                type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                filterable=True,  # the security-trim filter targets this
            ),
            SearchField(
                name="embedding",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=self._dim,
                vector_search_profile_name="default",
            ),
        ]
        vector_search = VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name="hnsw")],
            profiles=[VectorSearchProfile(name="default", algorithm_configuration_name="hnsw")],
        )
        index = SearchIndex(name=self._index_name, fields=fields, vector_search=vector_search)
        SearchIndexClient(self._endpoint, self._credential).create_or_update_index(index)

    def upsert(self, chunks: list[Chunk]) -> None:
        docs = []
        for c in chunks:
            content = self._store.get(c.text_ref).decode()
            embedding = c.vector(self._store)
            docs.append({
                "chunk_id": _doc_key(c.tenant_id, c.chunk_id),  # #916: tenant-scoped key
                "tenant_id": c.tenant_id,
                "doc_external_id": c.doc_external_id,
                "text_ref": c.text_ref,
                "title": c.title,
                "uri": c.uri,
                "content": content,
                "allowed_principals": c.allowed_principals,
                "embedding": embedding,
            })
        if docs:
            self._client.upload_documents(documents=docs)

    def search(self, query_embedding: list[float], principals: list[str], top_k: int,
               scope) -> list[dict]:
        from azure.search.documents.models import VectorizedQuery

        if getattr(scope, "doorway", frozenset()):
            # LAW 7: an unimplemented capability SAYS SO. Quietly dropping the doorway and
            # returning owner-only results would be a share that appears to work and returns
            # nothing - the precise bug #582 exists to kill. It must not come back as an
            # adapter gap. Implement the filter here before this backend serves sharing.
            raise NotImplementedError(
                "AzureAISearchIndex does not implement the #582 sharing doorway")
        # TWO mandatory trims: tenant partition (ADR 0012) AND the LAW-2 ACL overlap.
        # Escape single quotes defensively; the partition is server-supplied, escaped anyway.
        safe = ",".join(p.replace("'", "") for p in principals)
        safe_tenant = (getattr(scope, "partition", scope) or "").replace("'", "")
        trim_filter = (f"tenant_id eq '{safe_tenant}' and "
                       f"allowed_principals/any(p: search.in(p, '{safe}', ','))")
        vector_query = VectorizedQuery(vector=query_embedding, k_nearest_neighbors=top_k, fields="embedding")
        results = self._client.search(
            search_text=None,
            vector_queries=[vector_query],
            filter=trim_filter,
            top=top_k,
            select=["doc_external_id", "chunk_id", "text_ref", "title", "uri", "allowed_principals"],
        )
        return [
            {
                "doc_external_id": r["doc_external_id"],
                "chunk_id": r["chunk_id"],
                "text_ref": r["text_ref"],
                "title": r.get("title", ""),
                "uri": r.get("uri", ""),
                "score": r.get("@search.score", 0.0),
                "allowed_principals": r.get("allowed_principals", []),
            }
            for r in results
        ]

    def delete(self, tenant_id: str, doc_external_id: str) -> None:
        # Find the doc's chunks and delete them (tombstone on revocation/removal, LAW 2/3).
        hits = self._client.search(
            search_text="*",
            filter=f"tenant_id eq '{tenant_id}' and doc_external_id eq '{doc_external_id}'",
            select=["chunk_id"],
        )
        keys = [{"chunk_id": h["chunk_id"]} for h in hits]
        if keys:
            self._client.delete_documents(documents=keys)
