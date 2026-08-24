# ADR 0008 — No double data: per-source retrieval mode (pushdown | native | index)

**Status:** Accepted · **Date:** 2026-07-04 · **Source:** user decision 2026-07-04 ("central
data can't be DBSearch's data — it has to be THEIR central database; no double data, just
connectors").

## Context
The customer's system of record must stay the **only** copy of their data. DBSearch must never
become a second data platform they have to trust, migrate, or delete from. At the same time,
semantic search over 1TB of unstructured documents is impossible without *some* precomputed
index — you cannot embed a terabyte at query time. These two truths have to be reconciled
per source, not with one global answer.

## Decision
Every store in the catalog carries an explicit **retrieval mode**, chosen at connect time
(`stores.yml` / `dbsearch connect`), with this **preference order**:

1. **`pushdown`** — structured engines (BigQuery, Azure SQL/Synapse, Redshift/Athena).
   Query executes in the source engine (ADR 0007), delegated auth (ADR 0006). Zero copy.
2. **`native`** — sources that already expose a search API: **Microsoft Graph Search**
   (SharePoint/OneDrive/Teams), **Google Vertex AI Search / Cloud Search** (Workspace, GCS),
   **AWS Kendra**. DBSearch routes the query live to the source's search service **as the
   user** (ADR 0006 delegation applies identically); the source ranks, trims by its own ACLs,
   and returns hits that we wrap as Evidence. Zero copy, zero sync, no index of ours.
   This is a third store family — **NativeSearchStore** — beside IndexedStore/FederatedStore.
3. **`index`** — only for sources with no queryable surface of their own (loose file shares,
   uploads, wikis, exports). DBSearch builds a **derived index** (embeddings + chunk text +
   ACLs). The index is *derived data, not a second system of record*: it lives **inside the
   customer's tenant** (LAW-1 / ADR 0002 — their subscription, their region, their kill
   switch), is rebuildable from source at any time, and is deleted with the tenant.

Defaulting rule for providers: a `StoreProviderPort` for a given `kind` declares which modes
it supports and its default; `probe()` may detect that a better mode is available (e.g. tenant
has Graph Search licensing) and recommend it.

## Consequences
- "Connect Accenture's everything" stops implying "ingest Accenture's everything": warehouses
  federate, SharePoint/Drive can go `native` with zero ingestion, and only the residue needs
  the in-tenant index. Onboarding cost and data-duplication anxiety both collapse.
- `native` trades ranking control for instant onboarding: we can't tune BM25/vectors inside
  Graph Search, per-query latency is the source's, and result quality varies by licensing
  tier. Accepted — a tenant can migrate a source `native → index` later for quality, and the
  Evidence envelope makes that swap invisible downstream.
- Hybrid ranking caveat: `native` scores are not comparable with our index's cosine scores;
  the synthesizer must treat per-store rank, not raw score, as the merge signal (E3 concern).
- `index` mode remains subject to the existing connector contract (ACL capture, delta sync,
  revocation propagation — CONNECTORS.md); ADR 0008 does not weaken LAW 2/3.
- `StoreProfile.kind` gains `native_search`; `ManifestEntry` gains `mode`; the E9 compose
  layer must surface mode per source (design §5b/§9/§10 updated to match).

## Alternatives rejected
- **Index everything (Glean model):** simplest quality story, but creates the very "double
  data" platform the customer doesn't want, with TB-scale sync pipelines per source. Rejected
  as the default; retained only as `index` mode for sources that need it.
- **Never index (pure connectors):** makes semantic search over raw blob stores impossible
  and turns every query into N live crawls. Rejected as a blanket rule.
- **DBSearch-hosted central index:** violates LAW-1 outright. Never on the table.
