# ADR 0003 — Permission-faithful retrieval (mandatory, default-deny)

**Status:** Accepted · **Date:** 2026-06-25

## Context
The most common reason enterprise-search deployments fail is returning a result a user wasn't
allowed to see. For a consulting firm with client Chinese walls and need-to-know data, a single
such leak is a breach that ends the relationship. This is our biggest risk **and** our potential
moat.

## Decision
Permission correctness is a **first-class, non-bypassable invariant (LAW 2)**:
- Connectors capture each document's **ACL** (allowed principals) at sync time and denormalize
  it onto each indexed chunk.
- At query time the server expands the user's **transitive Entra group membership** and applies
  a **mandatory, default-deny security filter** — applied by the query service, never the caller.
- The **LLM, rerankers, caches, and citations consume only post-trim data.** Trimming happens
  **before** anything reaches the model.
- Revocations/deletions propagate via incremental sync + tombstones; optional **late-binding**
  re-check for high-assurance tenants.

Full mechanics and traps in `docs/PERMISSIONS.md`.

## Consequences
- Slightly higher query complexity and some staleness bounded by cache TTL — an accepted cost.
- Erring toward returning **too little** is safe; **too much is a breach** — the system is biased
  to deny on uncertainty.
- A standing **cross-user permission test** runs on every change; it is part of "done."

## Alternatives rejected
- **Post-hoc / prompt-based discretion** ("retrieve broadly, tell the model to be careful"):
  unsafe — the model is a leak path. Rejected outright.
- **Index-time-only filtering without query-time group expansion:** misses dynamic group/ACL
  changes; not faithful enough.
