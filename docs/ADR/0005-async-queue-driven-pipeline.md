# ADR 0005 — Async, queue-driven ingestion pipeline

**Status:** Accepted · **Date:** 2026-06-25

## Context
A single customer can have **10TB+** across documents, PDFs, SharePoint, etc. The initial crawl
runs for hours-to-days and must not block live queries, and ingest stages have very different
scaling profiles (parse = CPU-bound, embed = API/rate-limited, index = IO-bound). A synchronous
"crawl→parse→embed→index in one flow" design collapses at scale — this was the QuantifyMe failure
mode (built for one box, had to be redone for load).

## Decision
Ingestion is a set of **decoupled stages joined by durable queues** (Azure Service Bus), per LAW 4:

```
Connectors → [queue] → Parse/OCR → [queue] → Chunk+Embed → [queue] → Index
```

- Each stage is a **stateless worker** (LAW 6) that scales **independently** on its own signal
  (queue depth / CPU / API limits).
- Messages carry **references + small metadata**, not large payloads (content lives in Blob).
- Workers are **idempotent**, keyed by `(tenant, doc_id, content_hash)` (LAW 3) — retries and
  replays never double-index.
- **Backpressure** via queue depth; failures dead-letter per-item without stalling the pipeline.

## Consequences
- A multi-TB crawl can run continuously while queries stay fast (Phase 4 exit test).
- Scaling is a knob (worker count / autoscale rule), **not a redesign** — the explicit goal.
- More moving parts than a monolith: justified by the scale requirement; observability (LAW 8)
  makes it operable.

## Alternatives rejected
- **Synchronous monolithic pipeline:** simplest, but cannot absorb 10TB or scale stages
  independently — the exact corner we're avoiding.
- **One giant background job per tenant:** no independent scaling, no backpressure, poor recovery.
