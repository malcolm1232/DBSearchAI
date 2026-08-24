# The nine laws, and what enforces each one

> Written in the shape that survives a hostile question: **the claim** in plain
> English, **the mechanism** that makes it true, and **where in the code** to look.
> A law with no enforcement point is a slogan. Every one below has one.
>
> Two laws carry an honest caveat about what is not yet true. Read those before you
> present them; the caveat is short, and saying it costs nothing next to being caught.

---

## One - Data residency

**The claim.** Their documents never leave their own cloud. We run inside the customer's
tenant, so we cannot see their data even if we wanted to. Users query their own databases
without us ever holding a copy.

**How it's enforced.** Every payload bound for our control plane must pass
`BoundaryValidator.validate` before it is allowed to cross. Two independent layers: an
**allow-list**, where only fields declared in `boundary.schema.json` may appear at the root,
and a **deny-list** of content-bearing key names rejected at *any* nesting depth. A payload
that would carry document text raises `BoundaryViolation`, is dropped, and is audited -
before it leaves the tenant.

**Where.** `boundary/validator.py` · `boundary/boundary.schema.json` · `controlplane/agent.py`

**The line that proves it.** The air-gapped edition is that uplink turned off: `emit()`
returns `False` and sends nothing. A config flag, not a code fork, not a separate build.

---

## Two - Permission-faithful retrieval

**The claim.** No query may ever return what you are not allowed to see. Not filtered
afterwards - never retrieved in the first place.

**How it's enforced.** Three gates, in order:

1. **Catalog visibility.** A hereditary ACL walk decides which stores the router may even
   *consider*, and which it may *name in an explanation*. Visibility is inherited: a store
   under a business unit you cannot see is invisible even if its own ACL would admit you.
2. **Per-store authorization.** `authorize()` resolves you into an `AccessContext`;
   `retrieve()` takes that context and never a raw untrimmed query. No store can retrieve
   un-authorized.
3. **Synthesis.** Everything arriving has already been trimmed, so merging is subtractive
   only. It can drop and reorder. It can never add.

**Where.** `router/catalog.py` (`visible_stores`) · `router/identity_broker.py`
(`access_for`) · `router/store.py` (`AccessContext`) · `router/synthesizer.py`

**The line that proves it.** Asking for a store that does not exist and asking for one you
cannot see return the **identical** response. An invisible store has to be indistinguishable
from a nonexistent one, or the error message itself becomes the leak.

---

## Three - Connectors are isolated, idempotent, resumable

**The claim.** Each connector minds its own business. If the SharePoint connector breaks,
OneDrive keeps working. One failing connector never corrupts the index and never blocks
another.

**How it's enforced.** Each source is a separate module implementing `ConnectorPort`, with
its own authentication and its own `list_changes(cursor)` for resumable incremental sync.
A connector that throws cannot reach another connector's code or the shared index. Stage
workers are idempotent on `(tenant, doc_id, content_hash)`, so a retry or a replay can never
double-index.

**Where.** `ports/base.py` (`ConnectorPort`) · `connectors/sharepoint.py` ·
`connectors/sharepoint_graph.py` · `connectors/folder.py` · `connectors/upload.py`

---

## Four - Async, queue-driven, horizontally scalable

**The claim.** Ingesting ten terabytes is not one enormous request. Parse, embed and index
are separate stages joined by durable queues, each scaling on its own signal - parse is
CPU-bound, embed is API-bound, index is IO-bound.

**How it's enforced.** Stages are decoupled behind `QueuePort` and chained
`parse → chunkembed → index`. Messages carry **object-store references, never document
bytes**. In production each stage is its own autoscaling worker; drained in-process for a
local run, the stage code is byte-identical.

**Where.** `pipeline/runner.py` · `ports/base.py` (`QueuePort`) ·
`adapters/azure/servicebus.py`

**⚠ Honest caveat.** The in-app SharePoint connector does **not** yet use this path.
`POST /connectors/sharepoint/finish` ingests a whole library in one synchronous request that
can run for minutes. The queue-driven pipeline is real and is the designed path; that one
endpoint bypasses it. If you are asked "so is anything still synchronous?", say yes and name
it. It is a known gap, not a contradiction of the design.

---

## Five - Multi-tenant isolation by construction

**The claim.** One data plane per customer. No shared index across customers, ever. A large
customer cannot degrade a small one, because they do not share anything to contend over.

**How it's enforced.** The catalog is rooted at a tenant node and every visibility walk
terminates there, so there is no path from one tenant's tree into another's. Index
operations are keyed by `tenant_id` at the port boundary. Isolation is a property of the
shape, not a filter someone has to remember to apply.

**Where.** `router/catalog.py` (the `TENANT` root) · `server/edition.py` (`tenant_id`) ·
`ports/base.py` (`IndexPort.delete` / `list_doc_acls` both take `tenant_id`)

---

## Six - Stateless compute, durable state in managed services

**The claim.** Any process can die at any moment and nothing important dies with it. That is
what makes scale-out and zero-downtime deploys possible.

**How it's enforced.** The query service holds no session. Identity travels as a signed,
httpOnly cookie that contains an oid, a name, an email and an expiry - and no capability.
Durable state lives in the index, blob storage and Postgres, never in a worker's memory or
local disk.

**Where.** `query/service.py` · `server/user_auth.py` (`sign_session` / `read_session`)

**⚠ Honest caveat.** `TokenVault` is in-memory today, and the code says so plainly: *"In
memory by design for now: restart => link again."* So a restart forces users to re-link their
cloud accounts. Deliberate, documented, and a real deviation from this law. Related: a
restart also used to wipe group memberships, which is exactly the bug on the "I don't know
vs nothing" slide - the same root cause, already fixed at the identity layer.

---

## Seven - Cloud-portable via ports and adapters

**The claim.** Azure is an adapter, not an assumption. The AWS build swaps the queue, the
object store, the index and the identity provider without touching core logic.

**How it's enforced.** Every cloud-specific capability sits behind an internal port with a
per-cloud adapter: object store, queue, secrets, identity, LLM, embedding, index,
document extraction. Cloud SDKs are imported **lazily inside connection factories**, so the
optional dependency is only needed when a manifest actually composes that kind of store.

**Where.** `ports/base.py` (the port definitions) · `adapters/azure/` · `adapters/local/` ·
`adapters/vectordb/` · `router/providers/` (ten store providers behind one interface)

**The line that proves it.** `import pymssql` lives *inside* `_pymssql_connect`, not at the
top of the module. You can run the whole product with none of the cloud SDKs installed.

---

## Eight - Observability and cost metering built-in

**The claim.** We can answer "is this tenant healthy, and what did it cost?" without ever
seeing their data.

**How it's enforced.** Every stage emits structured events carrying counts, cost and health
only. The emit path itself runs the boundary validator, so telemetry **physically cannot**
carry content - the observability layer is bound by LAW 1 rather than trusted to respect it.
Every query records an audit entry.

**Where.** `controlplane/agent.py` (`emit`) · `boundary/validator.py` ·
`router/structured.py` (`audit_trail`)

**The line that proves it.** The audit record for a federated SQL query logs
`delegated: true` - the *fact* that delegation happened, never the credential itself.

---

## Nine - Pluggable LLM and embedding

**The claim.** The model is a configuration choice. A customer who will not send text to a
hosted model can run an in-tenant open model instead, and nothing in the product changes.

**How it's enforced.** Generation and embedding sit behind `LlmPort` and `EmbeddingPort`.
The edition builds a dictionary of named chat models, and capability is detected rather than
assumed: a model that can generate SQL is wired in as the NL2SQL generator, and absent that
capability the store falls back to a deterministic default instead of breaking.

**Where.** `ports/base.py` (`LlmPort`, `EmbeddingPort`) · `server/edition.py`
(`chat_models`) · `adapters/llama/` · `adapters/groq/` · `adapters/anthropic/`

---

## Ten, for completeness - the Gate is mandatory

The first nine are invariants. The tenth is the process that keeps them true: an
architecture-correctness checklist that runs on **every** change, before it merges. A feature
that violates a law is redesigned, never merged "for now."

**Where.** `SKILL.md` §1

---

## How to present these

Do not give nine laws nine slides. Nobody retains nine of anything, and seven of them are
engineering hygiene that a technical audience will grant you in a sentence.

- **Laws 1 and 2 get real time.** They are the product. Everything else is how a competent
  team would build any system.
- **Laws 3 to 9 go on one slide**, as a list with the enforcement point beside each. The
  message is not the content of each law; it is that *every one of them has a named
  mechanism*. Say that out loud: "seven more, and none of them is a slogan - each has a file
  you can open."
- **Keep this document as the leave-behind.** It is the answer to "which of these are
  actually enforced?", which is the question a security reviewer asks after the talk.

The two caveats are an asset, not a liability. A team that can name the two places its own
architecture is not yet honoured reads as a team that measured, rather than a team that
recited its design document.
