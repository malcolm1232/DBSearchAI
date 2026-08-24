"""DBSearch.AI self-host edition — a runnable REST + GraphQL server.

The free, self-hostable edition: a tester runs it on THEIR OWN machine (Docker), points
it at THEIR data, and queries via the API. Because they run it, their data never comes to
us — which is the honest version of the whole data-residency thesis (LAW 1).

Real backend = pgvector (in-tenant Postgres) + Ollama (local LLaMA, OpenAI-compatible).
Test backend = in-memory, so the server logic is verifiable without Docker.
"""
