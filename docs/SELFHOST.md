# Self-Host Edition — Quickstart

Run DBSearch.AI on **your own machine**, point it at **your own documents**, and query it
over a REST + GraphQL API. Everything runs locally — pgvector for the index, a local LLaMA
(via Ollama) for embeddings + answers. **Nothing leaves your box** (the honest version of
the data-residency thesis, LAW 1). This is the free, open self-host edition; the managed
Azure/in-tenant edition is the paid one.

## 1. Run it

**Trying it out?** Use the demo overlay. It is seeded, it needs no secrets, and it turns the
dev identity switcher on for you:
```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
# then open http://localhost:8080  (log in via the alice/bob dev switcher)
```

**Deploying it for real?** The base file is production-shaped: pgvector, local llama3.2, and
a real login rather than a trusted header. It needs one secret from you before anyone can
sign in — see §"Before exposing this" for the alternatives to local email/password:
```bash
export DBSEARCH_SESSION_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
docker compose up -d --build          # models auto-pull on first up
curl localhost:8080/health
```
Skip that export and the box still boots and answers `/health`, but every authenticated
request returns a 401 that tells you this, and the startup log says the same at ERROR. That
is deliberate: it fails closed and loudly rather than quietly trusting whoever asks.

**Models & RAM.** (Full guide — where to download models, how to swap them, the sizing
ladder and how to benchmark a swap: **[MODELS.md](MODELS.md)**.)
Models auto-pull on first `up` (no manual step). The base edition uses
`llama3.2` (~2 GB), which wants ~6 GB of Docker memory; if the `ollama` container gets
OOM-killed (`llama-server ... signal: killed`), either raise Docker's memory or switch models:
set `OLLAMA_CHAT_MODEL` (e.g. `qwen2.5:0.5b` for ~400 MB, or `llama3.2:1b`). The demo overlay
already defaults to `qwen2.5:0.5b` so it runs on a laptop. `nomic-embed-text` (274 MB) is used
for embeddings in every edition.

## 2. Ingest your documents
Each doc carries an **ACL** — the list of group ids allowed to read it (LAW 2). Permissions
are enforced at query time, so set these to mirror your real access rules.
```bash
curl -s localhost:8080/ingest -H 'content-type: application/json' -d '{
  "external_id": "handbook", "title": "Staff Handbook",
  "text": "Holidays, expenses, onboarding for all staff.", "acl": ["all-staff"]
}'

curl -s localhost:8080/ingest -H 'content-type: application/json' -d '{
  "external_id": "deal-falcon", "title": "Project Falcon (Confidential)",
  "text": "Confidential merger valuation, deal team only.", "acl": ["deal-team"]
}'
```

## 3. Define who is in which group
Mount a `users.json` (user → groups) at `/data/users.json`, or rely on the demo default
(`alice` ∈ {all-staff, deal-team}, `bob` ∈ {all-staff}):
```json
{ "alice": ["all-staff", "deal-team"], "bob": ["all-staff"] }
```

## 4. Query — permission-trimmed, cited answers
Identity is never a body field, so a caller can't select whose results they get (LAW 2).

The `curl`s below use the **`X-DBSearch-User` header**, which is the *dev* identity seam and
is **off unless you opt in** (#315). Export `DBSEARCH_DEV_AUTH=1` for this section, or run the
demo overlay, which sets it for you. Do this on a laptop rig only: while it is on, anyone who
can reach the port may act as any user simply by naming them. On a real deployment leave it
off and sign in instead (§"Before exposing this", below).
```bash
# alice is on the deal team -> gets the confidential doc
curl -s localhost:8080/search -H 'X-DBSearch-User: alice' \
  -d '{"question":"what are the falcon deal terms?"}' -H 'content-type: application/json'

# bob is NOT -> the doc is never retrieved; the model can't answer it
curl -s localhost:8080/search -H 'X-DBSearch-User: bob' \
  -d '{"question":"what are the falcon deal terms?"}' -H 'content-type: application/json'

# no identity header -> 401 (no anonymous access)
```
GraphQL is at `http://localhost:8080/graphql` (same `QueryService`); identity is the header,
there is no `userOid` argument to spoof:
```graphql
{ search(question: "falcon deal terms") { answer citations { doc title } retrievedDocs } }
```

## API
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/health` | – | `{status, backend, tenant}` |
| POST | `/ingest` | `{external_id, title, text, acl[], uri?}` | `{indexed, acl}` |
| POST | `/search` | `{question}` + `X-DBSearch-User` header | `{answer, citations[], retrieved_docs[], corpus}` |
| ALL | `/graphql` | GraphQL + `X-DBSearch-User` header | `search(question)` |

### Two different numbers: retrieved vs entitled (#393)

`retrieved_docs` is what **this question** drew on - the permission-trimmed top-k. It moves
when the question moves.

`corpus` is what **this caller** may see at all: `{indexed: bool, authorized_docs: int}`,
computed with the same mandatory ACL predicate as retrieval, so it can never overstate what
a query would return. It is `null` when the backend cannot count - treat that as *unknown*,
never as *empty*.

Do not report the first as the second. A UI that prints the retrieved count as "documents
you can access" tells the user their permissions changed when only their question did, and
renders an empty index as a permissions refusal. `authorized_docs` (REST) and
`authorizedDocs` (GraphQL) are **deprecated aliases of `retrieved_docs`**, kept so existing
clients keep working; they never meant entitlement.

## ⚠ Before exposing this beyond local testing
- **Identity:** the `X-DBSearch-User` dev header is **opt-in and off by default** (#315).
  Setting `DBSEARCH_DEV_AUTH=1` makes the server trust whatever user id a caller writes into
  that header, with no session, cookie or key — so it is a laptop-only switch, and turning it
  on for anything reachable hands every document to anyone who can open a TCP connection. It
  used to default *on*, which meant the published `docker compose up` produced exactly that
  box; if you are upgrading, check nothing in your deployment was relying on the old default.
  A client still cannot select identity via the body or GraphQL args either way (LAW 2).

  For a real deployment pick one real login and leave the dev header off:
  - **local email/password** — `DBSEARCH_LOCAL_AUTH=1` plus `DBSEARCH_SESSION_KEY` (a long
    random value; it signs session cookies and refuses to run without a real one, #574).
    This is what `docker-compose.yml` arms by default.
  - **Entra / Google** — set their client id + secret; the session key is derived from those.
  - **bearer JWT** — install a verifier via `dbsearch.api.auth.set_bearer_verifier(...)` so
    identity comes from a verified Entra-issued token.

  With none of them configured the server still boots and serves `/health`, but every
  authenticated request is refused with a 401 naming the fix, and startup logs the same at
  ERROR. Put the server behind TLS / a reverse proxy regardless.
- **Ingestion** here is synchronous, single-doc, and unauthenticated — fine for local
  testing; add admin auth + batch/streaming + real connectors for the enterprise edition.

## Run the server logic without Docker
```bash
pip install -e '.[server]'
python3 tests/selftest_server.py     # in-memory backend; proves REST+GraphQL + LAW 2 trimming
```
