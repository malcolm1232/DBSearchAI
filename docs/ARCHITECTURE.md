# Reference Architecture

> Detail for SKILL.md LAWs 1, 4, 5, 6, 7, 8. Azure-first, control/data-plane split.
> When this disagrees with SKILL.md, SKILL.md wins — fix this doc.

## 1. Two planes, one narrow wire

| | **Data plane** | **Control plane** |
|---|---|---|
| Runs in | The **customer's** Azure subscription/VNet | **Your** Azure |
| Holds | All documents, embeddings, indexes, blobs, LLM calls | No customer content — ever |
| Purpose | Ingest, index, answer queries | Release/updates, billing, health, onboarding |
| Crosses the wire | **Telemetry/metadata only** (counts, health, versions) | Config + signed release artifacts pushed down |

The wire is **mutual-TLS, signed, schema-validated, and audited**. The set of fields that
may cross **up** is an explicit allow-list (the **boundary contract**, §6). Turning the
wire off = the **air-gapped edition** (a config flag, not a code fork).

> **LAW 1 restated for builders:** if you are about to send anything to the control plane,
> it must validate against the boundary contract. Document text, file bytes, chunk text,
> embeddings, query strings, and answer text are **never** allowed up.

## 2. Data-plane pipeline (the spine)

```
Connectors ─push─> Service Bus ─> Parse/OCR ─> Chunk+Embed ─> Index + ACL store
   │  (content + ACLs, incremental)   (AKS)        (AKS)        (Azure AI Search)
   └─ Microsoft Graph first (SharePoint/OneDrive/Teams/Outlook)
```

Each arrow is a **durable queue boundary** (LAW 4). A message carries references + small
metadata, not giant payloads — large content lives in Blob and is fetched by reference.

| Stage | Azure service (default) | Behind which port |
|---|---|---|
| Source pull | Microsoft Graph API | `ConnectorPort` |
| Queue | **Azure Service Bus** | `QueuePort` |
| Raw doc cache | **Azure Blob Storage** (in tenant) | `ObjectStorePort` |
| Text/OCR extraction | **Azure AI Document Intelligence** | `ExtractorPort` |
| Embeddings | **Azure OpenAI** embeddings | `EmbeddingPort` |
| Vector + keyword index | **Azure AI Search** (hybrid + security filters) | `IndexPort` |
| ACL / principals store | Azure AI Search fields + Postgres (Azure DB for PostgreSQL) | `AclStorePort` |
| Identity / group expansion | **Microsoft Entra ID** (Graph) | `IdentityPort` |
| Answer generation (LLM) | **Azure OpenAI** chat | `LlmPort` |
| Secrets | **Azure Key Vault** | `SecretsPort` |

Every row's port is why we stay portable (LAW 7): the AWS adapter later swaps Service Bus→SQS,
Blob→S3, AI Search→OpenSearch, Azure OpenAI→Bedrock, Entra→IAM/Identity Center — **core logic
unchanged.**

## 3. Query path (the part users feel)

```
user question
  → embed query (Llm/EmbeddingPort)
  → hybrid retrieve from IndexPort (vector + keyword)
  → SECURITY-TRIM by user's Entra group membership   ← MANDATORY, default-deny (LAW 2)
  → rerank top-K
  → LLM answer WITH CITATIONS (every claim points to a source doc the user can open)
  → return answer + sources
```

The query service is **stateless** (LAW 6) — scale out behind the AKS ingress. Session/history,
if any, lives in Postgres, not in the pod. See `PERMISSIONS.md` for the trimming detail.

## 4. Scale model (the QM lesson, applied)

- **Decouple stages with queues** so a 10TB initial crawl can run for days without blocking
  queries, and each stage **autoscales independently** (parse is CPU-heavy, embed is
  API-bound, index is IO-bound — they must scale on different signals).
- **Backpressure** via queue depth; **idempotent** stage workers keyed by `(tenant, doc_id,
  content_hash)` so retries/replays never double-index (LAW 3).
- **No single-box assumptions.** Nothing lives only in one worker's memory or disk (LAW 6).
- **Per-tenant data plane** means scale is horizontal across tenants by construction (LAW 5) —
  a big customer never degrades a small one.

## 5. Packaging & deployment

- Data plane ships as an **Azure Marketplace Managed Application** (or Helm chart + Bicep/
  Terraform module) deployed **into the customer's subscription** with their consent.
- Customer grants a scoped Entra app registration (least-privilege Graph permissions for the
  sources they connect). We request the **minimum** scopes per connector.
- Updates: control plane pushes a **signed release** reference; a small in-tenant **agent**
  pulls and applies it. Air-gapped customers apply releases manually (same artifact).
- **Everything is IaC.** No click-ops. A new tenant = `terraform apply` of the data-plane module.

## 6. The boundary contract (LAW 1 made concrete)

A single versioned schema (e.g. `boundary.schema.json`) defines the **only** payloads allowed
to cross **up** to the control plane. Illustrative — not content:

```jsonc
{
  "tenant_id": "string",          // opaque id, not a name
  "event": "ingest.completed | query.served | sync.error | heartbeat",
  "counts": { "docs_indexed": 12000, "queries": 42, "errors": 1 },
  "cost":   { "embed_tokens": 1.2e6, "llm_tokens": 90000, "search_units": 3 },
  "health": { "index_pct": 0.94, "queue_depth": 12, "version": "2.3.1" },
  "ts": "iso8601"
  // NO document text, NO file names, NO query strings, NO user content. Ever.
}
```

Anything not in the contract is **rejected at the wire and logged as a violation.** This check
is itself a Gate item (SKILL.md §1, LAW 1).

## 7. Security posture (summary; expand per customer security review)

- Data plane in customer VNet; **private endpoints**, no public data egress.
- **Customer-managed keys (CMK)** for Blob + index encryption where supported.
- Least-privilege Entra app per connector; secrets in **Key Vault**.
- Full audit log of (a) every cross-plane message and (b) every query's permission decision.
- Air-gapped edition for customers who forbid any uplink (`uplink.enabled = false`).
