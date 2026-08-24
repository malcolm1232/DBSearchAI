"""External API layer over the query service (REST/GraphQL bindings live here).

`resolver.search_resolver` is the dependency-free core both bindings call — so the API
surface is a thin wrapper and the permission trim (LAW 2) lives in QueryService, never
in the transport.
"""
from dbsearch.api.resolver import search_resolver

__all__ = ["search_resolver"]
