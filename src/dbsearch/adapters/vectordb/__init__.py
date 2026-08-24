"""Vector-database adapters for the IndexPort.

pgvector is the default because it runs inside the customer's OWN Postgres (in-tenant) —
LAW-1-friendly, unlike SaaS vector DBs (Pinecone et al.) where data would leave the tenant.
Self-hosted Qdrant/Milvus/Weaviate would be additional adapters here, same port.
"""
from dbsearch.adapters.vectordb.pgvector import PgVectorIndex

__all__ = ["PgVectorIndex"]
