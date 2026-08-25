# DBSearch.AI

Ask a question in English, get an answer drawn from your databases and documents at once,
with a citation for every claim, and **never a result you were not already allowed to see**.

Connect Azure SQL, Postgres, MySQL, Synapse, Cosmos DB, BigQuery, Redshift, RDS, S3,
SharePoint, Google Drive, a shared folder link, or files you upload.
The router works out which sources a question needs, queries them as you, and cites what it used.
Permission trimming is a mandatory default-deny filter at query time, not a feature a caller can skip.

> **Building on this?** Read [`SKILL.md`](./SKILL.md) first - it is the canonical
> architecture spec (the ten LAWs and the Architecture-Correctness Gate). Decisions live in
> [`docs/ADR/`](./docs/ADR) (27 of them). Detail lives in [`docs/`](./docs).

## What "we can't see your data" does and does not mean

The architecture is built so that customer content stays in the customer's own cloud: the
data plane holds documents, embeddings, indexes and inference, and the control plane is only
allowed metadata and telemetry. That boundary is a schema plus a validator, not a promise -
see [`src/dbsearch/boundary/`](./src/dbsearch/boundary) and `tests/selftest_boundary.py`.

**That property belongs to the in-tenant deployment.** If you run the hosted demo at
dbsearch.ai instead, you are using our box: our vault, our Postgres, our Azure services, and a
third-party model provider for chat. It is a demo, and we can see what you put in it. Please
do not put anything confidential there. Self-host or in-tenant is the mode the residency claim
is about, and it is the one you can verify yourself from this repository.

## Run it

**Trying it out** - seeded, no secrets needed:
```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
# open http://localhost:8080 and use the alice/bob switcher
```

**Running it for real** - real login, no trusted headers:
```bash
export DBSEARCH_SESSION_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
docker compose up -d --build
curl localhost:8080/health
```
Models pull automatically into a Docker volume on first `up`; nothing is baked into the image.
Embeddings are `nomic-embed-text` (274 MB) in every edition.
The chat model is `llama3.2` (~2 GB, wants roughly 6 GB of Docker memory) in the base file, and
the demo overlay drops to `qwen2.5:0.5b` so it runs on a laptop.

Without a session key the server still boots and serves `/health`, but refuses every
authenticated request with a 401 that names the fix - it fails closed and says so.
Full guide: [`docs/SELFHOST.md`](./docs/SELFHOST.md), model sizing in
[`docs/MODELS.md`](./docs/MODELS.md).

## What works today

Fifteen source kinds across four groups on the canvas:

| group | kinds |
| --- | --- |
| Azure | Azure SQL, Postgres, MySQL, Synapse, Cosmos DB |
| Google Cloud | BigQuery |
| AWS | RDS Postgres, RDS MySQL, Redshift, S3 |
| Files & Links | uploaded files, Google Drive, SharePoint link, SharePoint, local index |

You wire them on a canvas, the router picks per question, and answers carry a proof pill you
can expand and re-run against the source.
Ask routes to every composed store as well as to your own documents, through one synthesizer
(ADR 0025).
The database groups need the matching account linked, because their queries run as you; the
Files & Links group needs only an account.
Operators additionally get a server-side `folder` connector.

Also shipped: multi-tenant workspaces, per-owner document partitions, Entra / Google / local
email-password sign-in, document and conversation sharing including anyone-with-the-link, and
an admin surface.

## What does not work yet

An honest list matters more here than a feature grid, so:

- **Cross-store joins are one hop only.** A question whose filter lives in store A and whose
  measure lives in store B is decomposed and answered (ADR 0014 option B; see
  `tests/selftest_cross_store_rescue.py`). Two-hop shapes - rank in one store, then carry the
  winner into another - still fail.
- **Answer accuracy is a standing problem, not a solved one.**
  [`research/retrieval/`](./research/retrieval) is the honest record: on its 38-question
  real-data pack the product answered 19 and got **7 confidently wrong**, which is the failure
  mode that matters because it reads exactly like a correct answer. Several of the named
  failures have since been fixed, but that committed capability table has not been re-measured,
  so read it as a floor rather than a current score.
- **Delegated query is built but not proven live.** A store can be configured to query as the
  signed-in user, so the database enforces its own row-level security rather than trusting us.
  The refusal path is tested and the plumbing is exercised offline, but the two-identity run
  against a live tenant - alice sees fewer rows than bob, for real - has not been done.
  Treat it as unverified until it has.
- **The control plane runs in-process.** The split is real in code and enforced by the
  boundary validator, but there is no network separation or mTLS deployed yet.

## Layout

```
SKILL.md                  the canonical spec - ten LAWs + the Architecture-Correctness Gate
docs/ADR/                 27 architecture decision records
src/dbsearch/
  server/                 FastAPI app, the canvas/ask/admin surfaces, sharing, sign-in
  router/                 which store answers which question; SQL generation; synthesis
  adapters/               per-cloud implementations behind the ports
  ports/                  the interfaces that keep the core cloud-portable (LAW 7)
  connectors/             per-source ingestion (LAW 3)
  query/                  retrieval, reranking, the relevance floor
  pipeline/               queue-driven ingest stages (LAW 4)
  boundary/               the uplink contract + validator (LAW 1)
  controlplane/           the metadata-only plane (LAW 1)
  eval/                   the golden-suite scorers and regression gate
tests/                    332 files, run them all with scripts/run_tests.py
eval_fixtures/golden_pack/  frozen corpus + 247 questions, content-hashed
site/                     the marketing site (Next.js)
```

## Tests

```bash
python3 scripts/run_tests.py            # everything, one number
python3 scripts/run_tests.py --selftest # unit/integration only, no browser
```
Currently **332 of 332 files pass**.
[`.github/workflows/tests.yml`](./.github/workflows/tests.yml) runs that same unnarrowed
command on every push and pull request.

Every test walks a real scenario - including deliberately wrong ones - and asserts a LAW
holds.
The suite names its own scope: narrow it and it prints `[PARTIAL]` rather than reporting a
green number that quietly skipped a third of the coverage.

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
get merged.
Found something that returns data to the wrong person? Please read
[`SECURITY.md`](./SECURITY.md) rather than opening a public issue.

Licensed under the [Apache License 2.0](./LICENSE).
