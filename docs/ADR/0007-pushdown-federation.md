# ADR 0007 — Pushdown-first federation; in-tenant DuckDB for loose files only

**Status:** Accepted · **Date:** 2026-07-04 · **Source:** Phase E design §11.2 (decided 2026-07-03)

## Context
Federated structured stores (E4) can hold 1TB+ per source. An analytical question ("total Q3
EMEA sales") needs aggregation over many rows, but the *answer* is tiny. Where does the query
execute — inside the source engine, or in a central federation engine that pulls rows out?

## Decision
**Pushdown-first.** NL2SQL generates dialect-aware SQL that executes **inside the source
engine** (BigQuery, Synapse/Azure SQL, Redshift, Athena); only the aggregated/filtered result
set (KB, not TB) returns as Evidence. A central federation engine that pulls raw rows across
cloud boundaries is **rejected**.

**Loose structured files** (`.csv`/`.xlsx` not in any warehouse) run through a lightweight
**DuckDB inside the tenant boundary**, over files already in the tenant — same pushdown
principle, the "engine" just happens to be embedded.

Cross-source combination (Scenario C/D) happens at the **synthesis layer** (E6) over per-store
result sets — never as a distributed raw-data JOIN executed by DBSearch.

## Consequences
- **LAW-1 holds by construction:** raw rows never leave the customer's cloud; what moves is a
  result set the user was authorized to read (per ADR 0006).
- Scales with source size: query cost lives where the data lives, on engines built for it.
- Constraint accepted: no exact cross-source JOINs — E6 compares/merges *answers*, not tables.
  Revisit only if a real customer workload demands federated joins (then evaluate the source
  clouds' own federation features, e.g. BigQuery Omni, before building anything).
- SQL surface must be **read-only, parameterized, allow-listed** against the visible schema,
  and every generated statement is audited (design §8).

## Alternatives rejected
- **Central federation engine (Trino/Presto-style) pulling rows cross-cloud:** violates LAW-1,
  moves TB to answer KB questions, and adds a giant always-on cost center. Rejected.
- **Ingesting warehouse extracts into the DBSearch index:** stale copies of 1TB sources,
  defeats federation, double data (see ADR 0008). Rejected.
