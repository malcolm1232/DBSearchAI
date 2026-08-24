"""GraphQL API over the query service, using Strawberry.

Optional dep: pip install '.[graphql]'   (strawberry-graphql + uvicorn)

Schema:
    type Citation { doc: String!, title: String!, uri: String!, locator: JSON! }
    type SearchResult { answer: String!, citations: [Citation!]!, authorizedDocs: [String!]! }
    type Query { search(question: String!): SearchResult! }

SECURITY (LAW 2): there is NO `userOid` argument — identity is taken from the trusted
request context (`info.context["user_oid"]`), which `build_asgi_app` populates from the
auth header (dev) or a verified bearer token (prod) via `dbsearch.api.auth`. A client
cannot select identity, so it cannot impersonate anyone. See `dbsearch/api/auth.py`.

Run locally:
    from dbsearch.config import Settings
    from dbsearch.factory import build_data_plane
    from dbsearch.api.graphql_app import build_asgi_app
    app = build_asgi_app(build_data_plane(Settings.from_env()).query_service())
    # uvicorn module:app
"""
# NOTE: no `from __future__ import annotations` here — Strawberry resolves field types
# from real annotation objects in module scope, so the GraphQL types live at module level.
from typing import List

import strawberry

from dbsearch.api.resolver import search_resolver
from dbsearch.query import QueryService


@strawberry.type
class Citation:
    doc: str
    title: str
    uri: str
    locator: strawberry.scalars.JSON = strawberry.field(default_factory=dict)


@strawberry.type
class SearchResult:
    answer: str
    citations: List[Citation]
    retrieved_docs: List[str]      # what this question retrieved (wire: retrievedDocs)
    # #393: `authorizedDocs` never meant "documents you may see" - it has always been the
    # post-trim top-k for this one question. Kept as a published-schema alias so existing
    # developer-API clients keep working; new clients should read `retrievedDocs`, and an
    # entitlement count comes from the REST `corpus` block, not from here.
    authorized_docs: List[str]


def build_schema(query_service: QueryService) -> "strawberry.Schema":
    @strawberry.type
    class Query:
        @strawberry.field
        def search(self, info: strawberry.Info, question: str) -> SearchResult:
            # Identity comes from the trusted request context (set by the transport), NEVER
            # from a client argument — there is no userOid arg to spoof (LAW 2).
            user_oid = info.context.get("user_oid") if hasattr(info.context, "get") else None
            if not user_oid:
                raise Exception("unauthenticated: no trusted identity in request context")
            tenant_id = info.context.get("tenant_id") if hasattr(info.context, "get") else None
            d = search_resolver(query_service, user_oid, question, tenant_id=tenant_id)
            return SearchResult(
                answer=d["answer"],
                # FIELD BY FIELD, never `Citation(**c)`. That splat made the published
                # GraphQL schema silently depend on the exact key set of an internal dict,
                # so #633 adding `quote`/`quote_kind` to a citation - a purely presentational
                # addition for the web Sources panel - turned every GraphQL search into a
                # 500. A developer API's shape must change when somebody decides to change
                # it, not as a side effect of a UI card. Exposing quotes here is a real
                # option and a separate decision; until it is made, this seam publishes what
                # `Citation` declares and nothing else.
                citations=[Citation(doc=c["doc"], title=c.get("title") or "",
                                    uri=c.get("uri") or "",
                                    locator=c.get("locator") or {})
                           for c in d["citations"]],
                retrieved_docs=d["retrieved_docs"],
                authorized_docs=d["retrieved_docs"],
            )

    return strawberry.Schema(query=Query)


def _resolve_context(request, default_tenant: str = "") -> dict:
    """The trusted user identity for a GraphQL request — derived from the request's auth context
    (session cookie, API key, or dev header), never from a client argument.

    #184: this calls `resolve_identity` with the SAME two seams (headers AND cookies) as the
    REST `current_user` dependency, so /graphql and /search cannot diverge. It used to pass
    headers only, which meant the dev-header switcher still authenticated on /graphql after
    the REST path had been coupled to real login (#183) — an unauthenticated caller could
    read any victim's permission-trimmed documents by naming their oid. The fail-closed
    coupling now lives inside `resolve_identity` itself, so no transport can forget it.

    Shared by both transports below so the two cannot drift apart — the whole point of #184.
    """
    from dbsearch.api.auth import DEMO_PREFIX, AuthError, resolve_identity, resolve_tenant

    try:
        user_oid = resolve_identity(lambda n: request.headers.get(n), request.cookies.get)
    except AuthError:
        user_oid = None
    # #279 (ADR 0009): the GraphQL query surface is LIVE-only. A `demo:*` identity is served
    # ONLY by the demo-safe /router/* read endpoints, never here - treat it as unauthenticated
    # so an anonymous demo visitor cannot route through the live query service (default-deny,
    # mirrors the REST `current_user` split).
    if user_oid and user_oid.startswith(DEMO_PREFIX):
        user_oid = None
    # ADR 0012: derive the tenant partition at the same chokepoint as identity, so the two
    # transports (REST current_user + this context) can never disagree about either value.
    tenant_id = resolve_tenant(lambda n: request.headers.get(n), request.cookies.get,
                               default_tenant)
    return {"user_oid": user_oid, "tenant_id": tenant_id}


def build_router(query_service: QueryService, default_tenant: str = ""):
    """GraphQL as REAL FastAPI routes at the exact advertised path (#432).

    This replaces `app.mount("/graphql", build_asgi_app(...))`, which left the API dead in
    production: Starlette compiles a Mount's path to `^/graphql(?P<path>/.*)$`, so the bare
    `/graphql` never matched, and instead of the router's slash-redirect saving it, the app's
    trailing catch-all `Mount("")` (SPA/static) matched first and answered 405 for POST and 404
    for GET. `POST /graphql/` worked the whole time, which is why it went unnoticed.

    Routes, not a Mount, for two independent reasons: `/graphql` is the path the Developer
    surface (#29) and the docs advertise, and a 307 on POST is a trap anyway because not every
    HTTP client replays the request body on redirect.
    """
    from fastapi import Request
    from strawberry.fastapi import GraphQLRouter

    # `request: Request` must be annotated: context_getter is resolved as a FastAPI dependency,
    # so an unannotated parameter is taken for a QUERY parameter and every call 422s with
    # {"loc": ["query", "request"]} instead of receiving the request.
    async def get_context(request: Request) -> dict:
        return _resolve_context(request, default_tenant)

    return GraphQLRouter(build_schema(query_service), context_getter=get_context)


def build_asgi_app(query_service: QueryService, default_tenant: str = ""):
    """Standalone ASGI GraphQL app — for running GraphQL on its own (see the module docstring's
    uvicorn recipe), NOT for mounting inside the FastAPI app; use `build_router` for that (#432).
    Identity resolution is shared with the router via `_resolve_context`."""
    from strawberry.asgi import GraphQL

    class AuthGraphQL(GraphQL):
        async def get_context(self, request, response=None):  # noqa: ARG002
            return _resolve_context(request, default_tenant)

    return AuthGraphQL(build_schema(query_service))
