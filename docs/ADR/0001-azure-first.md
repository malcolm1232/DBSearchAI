# ADR 0001 — Azure-first (with portability designed in)

**Status:** Accepted · **Date:** 2026-06-25

## Context
We must prove the product on one cloud first; being truly tri-cloud on day 1 is the #1 way
early teams stall. Our wedge is consulting / professional-services firms (Accenture-type), who
live in **Microsoft 365 / SharePoint**.

## Decision
Build and prove on **Azure first**:
- **Microsoft Graph** = native connectors to SharePoint/OneDrive/Teams/Outlook (the wedge data).
- **Azure OpenAI** for embeddings + answers; **Azure AI Search** for hybrid retrieval with
  built-in **security trimming**; **Entra ID** for identity/group expansion (LAW 2).
- **Azure Marketplace Managed Application** for in-tenant deployment (LAW 1 topology).

Portability is **not** sacrificed: all cloud services sit behind ports (LAW 7, ADR 0004), so
AWS/GCP are later adapters, not rewrites.

## Consequences
- Fastest path to a usable product for the first 5 customers.
- We depend on Azure-specific services initially — mitigated by the ports boundary.
- AWS/GCP support is a Phase 5 adapter exercise, gated by real demand.

## Alternatives rejected
- **AWS-first:** broader cloud share, but weaker fit for SharePoint-heavy first customers.
- **GCP-first:** smallest enterprise footprint; weakest wedge fit.
- **Agnostic day 1:** architecturally "pure" but slowest to a working product — wrong for a
  small team. We get the safety via ports instead.
