# Integrations — Vector DBs, LLaMA, GraphQL, LangChain

None of these are foundational. Each attaches at ONE defined seam and is a swappable
adapter/layer behind an existing port — the invariants (data stays in tenant, retrieval is
permission-faithful) sit above all of them. That separation is the whole point of the ports
architecture (LAW 7).

| Tech | What it is | Seam | Module | Enable |
|---|---|---|---|---|
| **Vector DB** | embedding storage + similarity search | `IndexPort` | `adapters/vectordb/pgvector.py` | `DBSEARCH_INDEX_BACKEND=pgvector` + `PGVECTOR_DSN`; `pip install '.[vectordb]'` |
| **LLaMA** | open-weight model (answers + embeddings) | `LlmPort` / `EmbeddingPort` | `adapters/llama/` | `DBSEARCH_MODEL_BACKEND=llama` + `LLAMA_*`; `pip install '.[llama]'` |
| **GraphQL** | external API query language | API layer over `QueryService` | `api/graphql_app.py` | `pip install '.[graphql]'` |
| **LangChain** | RAG/agent orchestration framework | retriever over `QueryService.retrieve()` | `adapters/langchain/` | `pip install '.[langchain]'` |

## Vector DB → `IndexPort` (pgvector)
Default index is Azure AI Search; **pgvector** is the in-tenant alternative — it lives in the
customer's own Postgres, so it satisfies **LAW 1** (unlike SaaS vector DBs such as Pinecone,
where data would leave the tenant). The **security trim (LAW 2)** is a mandatory SQL clause:
`WHERE allowed_principals && %s::text[]` (array overlap). Self-hosted Qdrant/Milvus/Weaviate
would be additional adapters at the same seam.

## LLaMA → `LlmPort` / `EmbeddingPort`
The pluggable-model port (**LAW 9**). LLaMA (served by vLLM/Ollama/TGI via an OpenAI-compatible
API) is the **in-tenant / air-gapped model option**: point `LLAMA_BASE_URL` at a model server
inside the customer's VNet and **no content ever leaves** (LAW 1) — no Azure OpenAI, no external
API. It's a drop-in for the Azure OpenAI adapters.

## GraphQL → API layer over `QueryService`
GraphQL is an **API query language, not a database** (don't conflate it with vector/graph DBs).
It's the external surface in front of the query service; retrieval and permissions happen
upstream, untouched. **Security:** identity is taken from the trusted request context (the
`X-DBSearch-User` header in dev, a verified bearer token in prod via `dbsearch.api.auth`),
**not** a client argument — there is no `userOid` arg, so a caller cannot impersonate anyone
(LAW 2). Proven by `tests/selftest_gqlauth.py`.

## LangChain → retriever over `QueryService.retrieve()`
Use LangChain for orchestration, but **keep the security boundary in our hands**: the
`build_retriever()` helper returns a LangChain `BaseRetriever` whose results are ALREADY
permission-trimmed by `QueryService.retrieve()`. LangChain never sees a document the user
isn't authorized for. We deliberately do NOT push the trim into a LangChain chain — explicit
beats magic when a bug equals a data breach.

## Proof
`tests/selftest_integrations.py` asserts LAW 2 holds at both attach points using the
dependency-free cores, and — when the libs are installed — runs the **real** Strawberry
GraphQL schema and the **real** LangChain retriever and confirms a user without access never
sees the confidential doc through either. (pgvector and LLaMA need a live Postgres / model
server to run; they are compile-checked and their trim/SQL is the LAW-2-critical line.)
