# ADR 0002 — Control/Data-plane split (data stays in the customer tenant)

**Status:** Accepted · **Date:** 2026-06-25

## Context
The core promise to enterprise buyers (and the founder's explicit goal) is **"we never have
access to your proprietary data."** Big-firm security teams reject multi-tenant SaaS that
ingests their documents into a vendor cloud. But a startup also can't survive doing manual
on-prem installs for every customer.

## Decision
Split into two planes:
- **Data plane** runs **inside the customer's Azure tenant** (their VNet): connectors, queues,
  parsing, embeddings, indexes, blobs, **and LLM inference**. All customer data and any
  content-derived artifacts (text, embeddings) live and die here.
- **Control plane** runs in **our** Azure: release/updates, billing/metering, health,
  onboarding. It **never receives document content** — only metadata/telemetry defined by the
  **boundary contract** (`ARCHITECTURE.md` §6).
- The link is a **mutual-TLS, signed, schema-validated, audited** wire: config/releases down,
  metadata up.

**Air-gapped edition** = the same data plane with `uplink.enabled = false`. It's a **config
flag, not a code fork** — preserving one codebase.

## Consequences
- Strong security story (data residency, LAW 1) **and** SaaS-like operations (central updates,
  metering, monitoring).
- We must rigorously enforce the boundary contract — a single leak of content up the wire
  breaks the core promise. The wire validator + audit log are mandatory.
- Per-tenant data plane means deployment is IaC-driven (LAW 7) and isolation is structural (LAW 5).

## Alternatives rejected
- **Fully air-gapped only:** maximum security but every customer becomes a manual on-prem
  install we babysit — doesn't scale for a startup. Kept as an *edition*, not the default.
- **Multi-tenant SaaS in our cloud:** easiest to build, but contradicts the no-access promise;
  big-firm security review rejects it.
