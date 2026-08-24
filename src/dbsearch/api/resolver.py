"""Transport-agnostic resolver core. REST and GraphQL both call this.

SECURITY (LAW 2): `user_oid` MUST be the identity from a verified auth token. Never accept
a client-supplied identity in production — that would let a caller impersonate anyone. The
transport layer is responsible for authenticating the user and passing the trusted oid here.
"""
from __future__ import annotations

from dbsearch.query import QueryService


def search_resolver(query_service: QueryService, user_oid: str, question: str,
                    tenant_id: "str | None" = None) -> dict:
    # ADR 0012: `tenant_id` is the transport-derived partition (resolve_tenant), same trust
    # rule as user_oid — server-supplied, never a client argument.
    result = query_service.answer(user_oid, question, tenant_id=tenant_id)
    return {
        "answer": result.answer,
        "citations": result.citations,
        "retrieved_docs": result.retrieved_docs,
        "authorized_docs": result.retrieved_docs,   # deprecated alias (#393)
    }
