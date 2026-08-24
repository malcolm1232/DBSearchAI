# DBSearch.AI — Canonical Architecture Spec (SKILL.md)

> **This file is the source of truth.** Every agent and every developer reads it
> **before** designing or building anything, and runs the **Architecture-Correctness
> Gate** below on every change. This file is intentionally lean (the enforceable spine).
> Detail lives in `docs/`. Do not bloat this file — push how-to into `docs/`.

---

## 0. What we are building (one paragraph)

DBSearch.AI lets an enterprise (first customer profile: a **consulting / professional
services firm** like Accenture) **search and ask questions across all of its own
documents** — SharePoint, OneDrive, Teams, PDFs, decks, wikis — and get **cited
answers**. The killer use case for the wedge is *"have we done this before / draft this
proposal from our past work."* We run **inside the customer's own cloud** so we **never
have access to their proprietary data**. The product is enterprise knowledge search /
RAG. The moat is **not "search"** — it is (1) **permission-faithful retrieval** and (2)
**connector breadth & quality**. Protect those two above all else.

**Standing research objective (Task 1, Malcolm 2026-07-31):** find the most accurate way
to retrieve information for a question, and verify which embedder / retriever / mechanism
is best. Measured ONLY on real third-party data (the model-authored golden corpus grades
itself — see `research/retrieval/README.md` for state, findings, and open cards).

---

## 1. THE ARCHITECTURE-CORRECTNESS GATE (run this BEFORE you build anything)

> You asked for this so we never scale into a rewrite again. **No feature is designed,
> coded, or merged without passing every check.** If any box is unchecked or you are
> unsure: **STOP, re-read this file + the relevant `docs/`, and redesign.** A feature
> that violates a LAW is redesigned — never merged "for now."

- [ ] **LAW 1 — Data residency.** Does any customer document **content** (text, file
      bytes, embeddings derived from content) cross out of the customer tenant — e.g. to
      the control plane, logs, error reports, analytics? If yes → **redesign.**
- [ ] **LAW 2 — Permission-faithful.** Can this path ever return, log, cache, or leak a
      result the user is **not already authorized** to see in the source system? Is the
      permission filter **mandatory and default-deny**? If it can be bypassed → **redesign.**
- [ ] **LAW 4 — Async.** Is anything done **synchronously in a request** that should be a
      durable queued stage (parse, embed, index, full sync)? If yes → **redesign.**
- [ ] **LAW 7 — Portable.** Is there **cloud-specific** code (Azure/AWS/GCP SDK calls)
      outside an adapter, leaking into core logic? If yes → **move it behind a port.**
- [ ] **LAW 6 — Stateless.** Is durable **state** being held in compute (in-memory, local
      disk) instead of a managed store? If yes → **redesign.**
- [ ] **LAW 5 — Isolation.** Does this preserve **per-tenant isolation** (no shared index,
      no cross-tenant path, no global mutable state)? If not → **redesign.**
- [ ] **LAW 8 — Observable.** Does it emit **structured telemetry + per-tenant cost/usage
      counters** (metadata only, never content)? If not → **add them.**
- [ ] **Reversibility.** If this decision is wrong at 100× scale, is it a **config change**,
      not a rewrite? If it's a one-way door → write an ADR in `docs/ADR/` first.

---

## 2. THE LAWS (invariants — never violate; full rationale in `docs/`)

1. **Data residency.** All customer data, embeddings, indexes, and LLM inference run in
   the customer's tenant (**data plane**). The **control plane** receives **metadata /
   telemetry only** — never document content. The allowed-uplink schema is the contract.
   → `docs/ARCHITECTURE.md`, `docs/ADR/0002-control-data-plane-split.md`
2. **Permission-faithful retrieval.** No query returns a result the user isn't already
   authorized to see. Permissions are a **mandatory, default-deny filter** at query time;
   no feature may bypass it. → `docs/PERMISSIONS.md`, `docs/ADR/0003-…`
3. **Connectors are isolated, idempotent, resumable.** Each connector is an independent
   module: own auth, rate-limit handling, incremental sync (change tokens), crash
   recovery. One connector failing never corrupts the index or blocks others.
   → `docs/CONNECTORS.md`
4. **Async, queue-driven, horizontally scalable.** Ingest → parse → chunk → embed → index
   are decoupled stages joined by durable queues. No "do it all in one request" path.
   *(The QuantifyMe lesson: design for 10TB / many tenants on day 1.)*
   → `docs/ADR/0005-async-queue-driven-pipeline.md`
5. **Multi-tenant isolation by construction.** One data plane per customer; **no shared
   index across customers, ever.** Tenant boundary enforced at every layer.
6. **Stateless compute, durable state in managed services.** Parsers/embedders/API are
   stateless and replaceable; state lives in the vector index / blob / Postgres. Enables
   scale-out and zero-downtime deploys.
7. **Cloud-portable via ports & adapters.** Every cloud-specific capability (object store,
   queue, secrets, identity, LLM, vector index, doc-extraction) sits behind an internal
   **port** with a per-cloud **adapter**. **Azure adapter first**; AWS/GCP later without
   touching core. → `docs/ADR/0004-ports-and-adapters-portability.md`
8. **Observability & cost metering built-in.** Every stage emits structured events +
   per-tenant usage/cost counters, so we can answer "healthy? what did it cost?" per
   tenant **without seeing data**.
9. **Pluggable LLM / embedding.** Model providers sit behind an interface. **Azure OpenAI
   default**; customer can bring an in-tenant open model (Azure ML / vLLM) with no code
   change.
10. **The Gate is mandatory.** §1 runs on every change. Violations are redesigned, not
    merged.

---

## 3. Shape of the system (the 30-second mental model)

```
        ┌──────────────────── CUSTOMER AZURE TENANT (DATA PLANE) ────────────────────┐
        │  Connectors → Service Bus → Parse/OCR → Chunk+Embed → Index (AI Search)     │
        │  (pull content + ACLs)        (AKS workers)        ↘ ACL store (principals)  │
        │                                                                             │
        │  Query API ──→ retrieve ──→ SECURITY-TRIM (Entra groups) ──→ rerank ──→ LLM │
        │                                                          answer + citations │
        │  Data, embeddings, blobs, LLM calls — ALL stay in here. Private endpoints.   │
        └─────────────────────────────▲───────────────────────────────────────────────┘
                                       │  metadata/telemetry ONLY (mTLS, signed, audited)
                                       │  ← config/releases pushed down
        ┌──────────────────────────────┴──────────── YOUR AZURE (CONTROL PLANE) ──────┐
        │  Release/orchestration · Billing/metering (counters) · Health · Admin/onboard│
        │  Air-gapped edition = this uplink turned OFF (config flag, not a rewrite).    │
        └─────────────────────────────────────────────────────────────────────────────┘
```

**Locked decisions:** Azure-first · Consulting/professional-services wedge ·
Control/data-plane split (air-gap = config flag). See `docs/ADR/` for the why.

---

## 4. Where to read next

| You are about to… | Read first |
|---|---|
| Design any component / understand the whole | `docs/ARCHITECTURE.md` |
| Touch search results, ACLs, or "who can see what" | `docs/PERMISSIONS.md` (LAW 2) |
| Add or change a data source | `docs/CONNECTORS.md` (LAW 3) |
| Decide what to build now vs later | `docs/ROADMAP.md` |
| Understand or revisit a big decision | `docs/ADR/` |
| Run the tests, or quote a suite number | `python3 scripts/run_tests.py` — runs the WHOLE `tests/` directory. A `selftest_*` glob silently omits the browser tests, which is how three of them sat red for a day behind a green figure (#678). |

---

## 5. Build protocol (how we avoid the QM corner)

- **Phase 0 before features.** Ports/adapters skeleton, IaC, boundary-contract schema,
  and this file exist **before** any feature code. See `docs/ROADMAP.md`.
- **One slice at a time, end-to-end.** Prove the whole spine on one connector and one
  tenant before widening. Vertical slices, not horizontal layers.
- **Every big or one-way decision → an ADR** in `docs/ADR/` (one decision per file).
- **The Gate (§1) is the definition of done for design.** If it doesn't pass, it isn't built.
