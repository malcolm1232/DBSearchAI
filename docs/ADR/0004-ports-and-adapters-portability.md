# ADR 0004 — Ports & adapters for cloud portability

**Status:** Accepted · **Date:** 2026-06-25

## Context
We go Azure-first (ADR 0001) but must support AWS/GCP later **without a rewrite** — the founder
was previously forced into a full rewrite (QuantifyMe) by scaling/coupling decisions baked in too
early. Cloud SDK calls scattered through business logic are exactly that trap.

## Decision
Adopt **hexagonal architecture (ports & adapters)**. Core logic depends only on internal
interfaces (**ports**); each cloud provides **adapters**:

| Port | Azure adapter (first) | AWS adapter (later) |
|---|---|---|
| `QueuePort` | Service Bus | SQS |
| `ObjectStorePort` | Blob Storage | S3 |
| `IndexPort` | Azure AI Search | OpenSearch / vector DB |
| `EmbeddingPort` / `LlmPort` | Azure OpenAI | Bedrock |
| `IdentityPort` | Entra ID (Graph) | IAM / Identity Center |
| `ExtractorPort` | AI Document Intelligence | Textract |
| `SecretsPort` | Key Vault | Secrets Manager |
| `ConnectorPort` | Microsoft Graph, … | same (source-, not cloud-, specific) |

**No cloud-specific type or SDK call may appear outside an adapter** (Gate item, LAW 7).

## Consequences
- A new cloud = a new set of adapters + IaC, with **zero changes to core** (Phase 5 exit test).
- Small upfront cost: defining clean port interfaces in Phase 0 before features.
- Also enables **pluggable LLM/embedding** (LAW 9) and swapping the vector index without touching
  query logic.

## Alternatives rejected
- **Direct SDK calls (no abstraction):** fastest to write, but recreates the QM coupling trap.
- **A heavy multi-cloud abstraction framework:** over-engineered; we only abstract the handful of
  capabilities we actually use.
