# DBSearch.AI

Ask a question in English, get an answer drawn from your databases and documents at once —
with a citation for every claim, and **never a result you were not already allowed to see**.

Connect Azure SQL, BigQuery, Redshift, RDS, S3, SharePoint or a folder; the router works out
which sources a question needs, queries them as you, and cites what it used. Permission
trimming is a mandatory default-deny filter at query time, not a feature a caller can skip.

> **Building on this?** Read [`SKILL.md`](./SKILL.md) first — it is the canonical
> architecture spec (the ten LAWs and the Architecture-Correctness Gate). Decisions live in
> [`docs/ADR/`](./docs/ADR) (24 of them). Detail lives in [`docs/`](./docs).

## What "we can't see your data" does and does not mean

The architecture is built so that customer content stays in the customer's own cloud: the
data plane holds documents, embeddings, indexes and inference, and the control plane is only
allowed metadata and telemetry. That boundary is a schema plus a validator, not a promise —
see [`src/dbsearch/boundary/`](./src/dbsearch/boundary) and `selftest_boundary.py`.

**That property belongs to the in-tenant deployment.** If you run the hosted demo at
dbsearch.ai instead, you are using our box: our vault, our Postgres, our Azure services, and a
third-party model provider for chat. It is a demo, and we can see what you put in it. Please
do not put anything confidential there. Self-host or in-tenant is the mode the residency claim
is about, and it is the one you can verify yourself from this repository.

## Run it

**Trying it out** — seeded, no secrets needed:
```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
# open http://localhost:8080 and use the alice/bob switcher
```

**Running it for real** — real login, no trusted headers:
```bash
export DBSEARCH_SESSION_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
docker compose up -d --build
curl localhost:8080/health
```
Models (`nomic-embed-text` plus a chat model) pull automatically into a Docker volume on first
`up`; nothing is baked into the image. Without a session key the server still boots and serves
`/health`, but refuses every authenticated request with a 401 that names the fix — it fails
closed and says so. Full guide: [`docs/SELFHOST.md`](./docs/SELFHOST.md), model sizing in
[`docs/MODELS.md`](./docs/MODELS.md).

## What works today

Nine connector kinds across five groups — Azure (SQL, Synapse), Google Cloud (BigQuery), AWS
(Redshift, RDS Postgres, RDS MySQL, S3), Microsoft 365 (SharePoint), and local folders. You
wire them on a canvas, the router picks per question, and answers carry a proof pill you can
expand and re-run against the source.

Also shipped: multi-tenant workspaces, per-owner document partitions, Entra / Google / local
email-password sign-in, document and conversation sharing including anyone-with-the-link, and
an admin surface.

## What does not work yet

An honest list matters more here than a feature grid, so:

- **`/ask` does not route to connected databases.** It answers from the document index only,
  while the canvas routes to every composed store. Same question, two answers. Being fixed.
- **Cross-store joins fail.** The retrieval study scores 0 of 5 on questions that need a join
  spanning two stores. Single-store lookups and aggregates are solid.
- **Two browser tests are red**, and there is no CI yet. `python3 scripts/run_tests.py` runs
  the whole `tests/` directory and reports one honest number — currently 254 of 256.
- **Delegated query is built but not proven live.** A store can be configured to query as the
  signed-in user, so the database enforces its own row-level security rather than trusting us.
  The refusal path is tested and the plumbing is exercised offline, but the two-identity run
  against a live tenant — alice sees fewer rows than bob, for real — has not been done.
  Treat it as unverified until it has.
- **The control plane runs in-process.** The split is real in code and enforced by the
  boundary validator, but there is no network separation or mTLS deployed yet.

[`research/retrieval/`](./research/retrieval) is the standing investigation into how well
retrieval actually works, including the numbers that are bad and why.

## Layout

```
SKILL.md                  the canonical spec — ten LAWs + the Architecture-Correctness Gate
docs/ADR/                 24 architecture decision records
src/dbsearch/
  server/                 FastAPI app, the canvas/ask/admin surfaces, sharing, sign-in
  router/                 which store answers which question; SQL generation; synthesis
  adapters/               per-cloud implementations behind the ports
  ports/                  the interfaces that keep the core cloud-portable (LAW 7)
  connectors/             per-source ingestion (LAW 3)
  query/                  retrieval, reranking, the relevance floor
  pipeline/               queue-driven ingest stages (LAW 4)
  boundary/               the uplink contract + validator (LAW 1)
  eval/                   the golden-suite scorers and regression gate
tests/                    256 files, run them all with scripts/run_tests.py
eval_fixtures/golden_pack/  frozen corpus + 247 questions, content-hashed
site/                     the marketing site (Next.js)
```

## Tests

```bash
python3 scripts/run_tests.py            # everything, one number
python3 scripts/run_tests.py --selftest # unit/integration only, no browser
```
Every test walks a real scenario — including deliberately wrong ones — and asserts a LAW
holds. The suite names its own scope: narrow it and it prints `[PARTIAL]` rather than
reporting a green number that quietly skipped a third of the coverage.

## Docs

[Architecture](./docs/ARCHITECTURE.md) ·
[Permissions](./docs/PERMISSIONS.md) ·
[Connectors](./docs/CONNECTORS.md) ·
[Self-host](./docs/SELFHOST.md) ·
[Deploy to Azure](./docs/DEPLOY_AZURE.md) ·
[Integrations](./docs/INTEGRATIONS.md) ·
[Models](./docs/MODELS.md) ·
[Design system](./docs/DESIGN_SYSTEM.md) ·
[Roadmap](./docs/ROADMAP.md)

## Contributing and licence

[`CONTRIBUTING.md`](./CONTRIBUTING.md) covers how to run the suite and what a change needs to
get merged. Found something that returns data to the wrong person? Please read
[`SECURITY.md`](./SECURITY.md) rather than opening a public issue.

Licensed under the [Apache License 2.0](./LICENSE).
