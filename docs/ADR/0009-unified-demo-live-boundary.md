# ADR 0009 — Unified demo/live on one server: local-backed demo scope, live Azure scope, selected by auth state

**Status:** Accepted · **Date:** 2026-07-21 · **Source:** GOAL (one public site: before-login demo, after-login live) · **Cards:** #278/#279/#280/#281

## Context

The product GOAL is **one public site**. A visitor who is not signed in plays a **demo** —
acting as `alice` or `bob` over sample data ("chat with the database", five connectors) — and
can then **Sign in with Microsoft** to query **their own live Azure data as themselves** (OBO,
ADR 0006). The demo must sell the *permission-faithful, multi-connector* story: Alice sees more
than Bob; five heterogeneous sources federate into one cited answer.

Two facts make this non-trivial today:

1. **The two modes need opposite server configs.** A server with `AUTH_*` set requires a real
   login and, by `#183`, **refuses the `X-DBSearch-User` act-as header** (so a live server can
   never honour a spoofable identity). A dev-auth server honours the header but has no real
   login. Demo (act-as) and live (Microsoft) therefore cannot coexist on one running instance —
   they run as two servers (`live_entra_up.sh`: `:8080` live, `:8081` demo).
2. **Hosting the live Azure fleet 24/7 for a demo is fragile.** A single QA session hit a paused
   serverless SQL DB, a downed Synapse pool, and a new interactive-MFA gate — none of which a
   public demo visitor should ever experience. The demo must be **fully local / offline**.

We need the demo to be self-contained *and* to coexist with live on one deployable, with a
**seamless login→live swap** (the same badged node flips from local sample data to the
customer's real Azure). This touches LAW 2 (the anon↔live boundary) and is hard to reverse, so
it gets an ADR before code (SKILL.md §1 Reversibility, §5).

## Decision

**One server, two disjoint scopes selected by authentication state. The demo scope is confined
to local fixtures and fixed demo principals; the live scope is the existing OBO product. They
never share a store, a credential, or an identity.**

### 1. Scope selection by session, not by user input
- **No valid `dbs_session` cookie → DEMO scope.** Identity is resolved from a constrained
  act-as selector whose value is validated against a **fixed allowlist of demo principals**
  (`alice`, `bob`; default `bob` = least access). It serves **only** the demo catalog
  (tenant `acme-demo`), whose stores are backed by bundled local fixtures.
- **Valid Microsoft session → LIVE scope.** Identity is the signed-in user's `oid`; it serves
  that user's real catalog, every federated query executing under the user's delegated token
  (ADR 0006). Unchanged.

### 2. The act-as selector can never cross into live
The demo act-as selector **cannot name a real user `oid`**, **cannot mint or read a delegated
token**, and **cannot reach a live store**. Live stores require a real delegated credential that
the demo scope structurally cannot obtain — so LAW 2's default-deny is preserved *by
construction*, not by a filter that could be bypassed. `#183`'s protection is kept for live: the
dev header never authenticates a live identity. We relax `#183` only to let the allowlisted
act-as value select a **demo** principal reaching the **demo** catalog.

### 3. Badging: cloud connector kinds gain a demo-local backing behind `SqlEnginePort`
To badge demo nodes as the real connectors (so login→live is a same-node swap), the
`azure_sql` / `postgres` / `mysql` / `synapse` / `cosmos_db` providers gain a **local-fixture
adapter** selected **by scope**:
- **DEMO scope** → the store resolves to a local embedded engine (`SqliteEngine` for the four
  SQL-family kinds; a local JSON/doc store for Cosmos) reading a **bundled, read-only fixture**.
- **LIVE scope** → the same kind resolves to the real cloud adapter under the user's delegated
  token.

This sits **behind the existing `SqlEnginePort`** (which already treats SQLite as a sibling of
Synapse/BigQuery/Redshift) — no cloud-specific branch leaks into core (LAW 7). The node keeps
its real kind/badge; only the engine behind the port differs by scope.

### 4. Demo answer generation
The demo chat uses a real LLM for fluent cited answers (Groq default; keeps a paid Anthropic key
off a public endpoint), with the local **extractive** model as an automatic fallback so the demo
never hard-fails (#280). Public-demo safeguard: a separate demo key with a spend cap + per-visitor
rate limit.

## Consequences
- **Demo is fully offline** — no Azure dependency; it survives fleet pause, MFA changes, and
  outages. A hosted visitor can always use it.
- **One deployable serves both**; **login→live is an in-place data-source swap** on the same
  badged node — the GOAL's exact flow.
- The five cloud providers each need a local-fixture adapter path (moderate, all behind the port).
- A **one-time ETL snapshot** dumps the fleet's sample data to bundled read-only fixture files.
  The fixtures are **our own sample data, never customer content**, so LAW 1 is not implicated;
  they ship as read-only build artifacts, not mutable compute state (LAW 6 respected).
- A **hard LAW-2 regression test is mandatory**: an anonymous/act-as request must be proven
  unable to reach any live store or any real identity — this test gates the merge.
- The manifest/service-credential invariant of ADR 0006 is unchanged: the demo backing is a
  **scope-selected engine, never a bypass key** to live data.

## Alternatives rejected
- **Two deployments / subdomains** (`demo.` dev-auth + `app.` real-login): simplest and needs no
  code, but loses the seamless in-place login→live and duplicates hosting. Kept only as a
  fast-path fallback if one-server slips.
- **Cosmetic relabel** (`kind: csv` titled "Azure SQL"): the node renders as a CSV/federated-SQL
  node, not the real connector, so the login→live swap isn't seamless, and a node claiming to be
  Azure while running SQLite is a small deception. Rejected.
- **Live Azure fleet powering the demo:** exactly the 24/7 fragility we are removing (serverless
  pause, MFA, outages reach demo visitors). Rejected.
- **Local DB containers** (real Postgres/MySQL/Cosmos emulator) for authenticity: reintroduces
  stateful 24/7 infra on the host, contradicting the offline-demo goal; a Cosmos emulator on a
  Linux host is painful. Rejected.
