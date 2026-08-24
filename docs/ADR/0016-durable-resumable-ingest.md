# ADR 0016 - Ingest as a durable, resumable, observable job

Date: 2026-08-06 · Status: **accepted** 2026-08-07 (Malcolm) · Cards #454, #455, #327, #251, #366, #390, #371, #534 · Builds on ADR 0012 (document-plane tenant partition), #391 (delete-before-index)

## Context

The owner's goal is that a user connects SharePoint (or uploads files) and talks to their data.
Every remaining obstacle to that sits in one subsystem - ingest - and this ADR exists because the Architecture-Correctness Gate refuses the current design on three separate checks, not because the code is untidy.

### What is measured, not assumed

`#536` ran the shared ingest pipeline over a padded copy of the doc pack on 2026-08-06:

| corpus | documents | outcome |
| --- | --- | --- |
| 4.0MB | 120 | composed in 285.9s (70.6 s/MB) |
| 12.1MB | 1187 | composed, scored 29/33 |
| 40.2MB | 4884 | **`/router/compose` exceeded its 3600s timeout** |

40MB did not finish in an hour, so the effective rate was at least 89.6 s/MB - worse than the 4MB rate, cause unattributed.
This was the LOCAL doc pack, not SharePoint: the defect is in the shared pipeline, and SharePoint merely inherits it.
A real SharePoint library is larger than 40MB, so on today's code the flow the owner wants cannot complete at all.

### Which gate checks fail

**LAW 4 (Async)** - *"Is anything done synchronously in a request that should be a durable queued stage (parse, embed, index, full sync)?"*
`router_api.sync_store` calls `provider.sync(store_id)` inline, and `ConnectorStoreProvider._build` calls `self.sync(sid)` as the "initial full crawl" during compose.
So both re-sync AND first connect run an entire crawl inside one HTTP request.

**LAW 6 (Stateless)** - *"Is durable state being held in compute?"*
`SourceRegistry` is a `dict` in the server process and the pipeline runs on `InMemoryQueue`.
Restarting the process loses the cursor, the queue, and the crawl (#327).

**Reversibility** - the choice of job store and checkpoint granularity is not a config change at 100x. Hence this ADR.

### The failure the code already admits

From `pipeline/runner.py`, written for #391:

> *"...a real re-ingest blanked the index for hours and every query answered 'Searched 0 documents you can access' - indistinguishable from a broken product, and nothing was checkpointed, so a restart mid-crawl lost the run AND left the corpus empty."*

#391 fixed the blanked-corpus half by moving delete-before-index per document.
The uncheckpointed half is still open and is this ADR's subject.

### The trap that makes the obvious fix wrong

`ConnectorPort.list_changes(cursor) -> (items, next_cursor)` advances the cursor **per batch, not per item**.

```python
result = run_ingestion(..., cursor=desc.cursor)   # whole crawl, blocking
except Exception:
    self.sources.record_error(store_id)           # cursor NOT advanced
summary = self.sources.record_sync(..., cursor=result.cursor)   # only on success
```

Today's behaviour is therefore *correct but wasteful*: a failure re-crawls from the last good cursor and loses no document.
**Persisting `next_cursor` earlier - the intuitive "make it resumable" fix - would silently skip every unprocessed item in the batch.**
Silent document loss is far worse than a slow re-crawl, and it would be invisible: the store would simply answer as though those documents did not exist.

Any resume design must keep the cursor pinned until the batch completes, and checkpoint *within* the batch instead.

## Decision

Ingest becomes a **job**: started by a request, executed off it, checkpointed as it runs, and observable while it runs.

### 1. The request starts a job and returns

`POST /router/stores/{id}/sync` and the compose-time initial crawl both enqueue a job and return immediately with a handle and `status: "syncing"`.
`SourceDescriptor.status` already reserves this value - the comment reads `"syncing" reserved for async slice` - so the state machine was anticipated and never built.

### 2. Checkpoint per document, never per batch

The unit of progress is the document, because that is the unit `run_ingestion` can safely redo: the pipeline is documented as idempotent ("re-running REPLACES a document's chunk set, never duplicates and never leaves orphans").

- The job records each `(tenant_id, external_id)` as it is indexed.
- The cursor advances **only** when every item in the batch has been indexed.
- A resumed job calls `list_changes` with the SAME cursor and skips the recorded ids.

No document can be skipped, and completed work is not redone.

### 3. Job state is durable, not in-process

Job records (id, source, status, cursor-in-flight, completed ids, progress, error) live in the same managed store as the rest of the document plane, inside the tenant (ADR 0012 partitioning applies unchanged).
This is what makes restart-resume (#327) work rather than merely retry-resume.

### 4. Progress is honest and specific

`run_ingestion` already accepts `progress(phase, done, total)` (#302), which is currently discarded by the connector path.
The job persists it, and the API reports phase, documents done, documents total, and the terminal reason.
This closes #251, #366 and #390, and fixes #371 - a source that reports `0 docs / never-synced` while its content is queryable is the UI contradicting the data, which is a LAW 8 failure, not a cosmetic one.

## Consequences

**A long ingest stops being indistinguishable from a hang.**
That is the single biggest user-visible change, and it is worth more than the throughput work (#534) that follows it: a slow job with a progress bar is usable, a fast job with no feedback is not.

**Throughput work becomes measurable.**
#534's levers (batch size above 16, concurrent batches) can be measured per stage once the job records stage timings, using `scripts/latency_slice.py` as a controlled pair.

**Retry semantics stay honest.**
Because the cursor is pinned until a batch completes, a partially-ingested source is *behind*, never *wrong*. That is the correct trade for a permission-faithful product: stale is recoverable, missing is not.

**Two assumptions made explicit on acceptance (2026-08-07).**

*The resume replays the batch.* Skipping recorded ids only works if `list_changes(cursor)`
returns the same batch when called again with the same cursor. That is an assumption about
every connector, not just SharePoint, and it was unstated. A source that reorders or drops
items across a replay would lose documents silently. It holds for the connectors in the repo
(folder: mtime scan; Graph: delta token) and any new connector must satisfy it.

*The completed-id set is bounded by construction.* Encoding completed ids as a list on the
job row would rewrite 4884 ids per document at the #536 acceptance size - quadratic write
amplification against the exact corpus this design exists to survive. They are therefore
append-only rows keyed `(job_id, external_id)`, and the job row is written only when
phase or status changes.

**What this ADR does NOT decide.**
The concrete job store and worker mechanism (thread pool in-process vs an external queue) is deliberately left open: it is a portability question (LAW 7) that should follow the deployment target, and both satisfy the checkpoint contract above. The contract - pinned cursor, per-document checkpoint, durable job record - is the part that must not be renegotiated.

## What implementation found that the design did not (2026-08-07)

Each of these was discovered by making the change, not by reasoning about it, and each is a
consequence of moving ingest off the request rather than an incidental bug.

**Ingest gained a concurrent reader.** Every read path on the in-memory index iterated the
chunk map that the crawl now mutates: `RuntimeError: dictionary changed size during
iteration`, raised for a user asking a question while their library indexes. For a library
that takes an hour that is the normal case, not an edge case. Reads take a snapshot under a
lock and score outside it.

**Indexed is not the same as routable.** The compose layer snapshots a store's profile into
the catalog node and the router ranks on that snapshot. Taken over a not-yet-filled index, a
freshly connected document store is correctly indexed, correctly permission-trimmed, and
invisible to routing - #306/#453 reintroduced through the back door. A store's profile is
re-derived when its content lands, and the completion event is sticky because a small source
can finish before the caller subscribes.

**A store can be rebuilt mid-crawl.** Composing the same id again gives it a new index and
descriptor, so an in-flight crawl over the previous ones must not commit its cursor, and the
rebuild must start its own crawl rather than be handed the running one. Impossible while
ingest was inline; routine now (the conversational setup flow composes twice).

**A build is no longer side-effect-free enough for health.** `health.py` builds a store to
exercise it, which now supersedes the live one. Health builds an isolated clone.

**Compose can no longer report a bad source synchronously.** A connector error used to raise
inside `build()` and land in compose's `skipped` list; it now happens on a worker. The honest
replacement is the job's terminal status plus a freshness that distinguishes `syncing`,
`sync-failed` and `ingested@` - and `probe()` (the canvas Test-connection) is still
synchronous, so an unreachable source is still caught before compose.

## Sequencing

1. **#454 + #455 together.** A background job without a checkpoint just relocates the failure from a timed-out request to a silently lost overnight run.
2. **#251 / #366 / #390 / #371** - progress and honest state, which the job record makes cheap.
3. **#327** - restart-resume, which falls out of durable job state.
4. **#534** - throughput, last, because faster-but-unresumable is still unusable.

## Verification

The acceptance test already exists: the `#536` padded packs.
`doc_pack_10x` (40.2MB, 4884 documents) is the case that cannot complete today.
Done means it completes, survives a mid-crawl process kill by resuming rather than restarting, and reports truthful progress throughout - measured, not asserted.

## Amendment (2026-08-21, #883): the registry's sync-state is durable too

This ADR made the JOB durable and left the SOURCE REGISTRY in memory, and that seam was the
whole of #883.
A restart wiped `cursor`, `last_sync_at`, `doc_count`, `unreadable` and `status`, so after
every deploy the canvas connector node read "never-synced / 0 docs" over a pgvector corpus
that was still answering questions with citations.
The owner re-ran a full crawl once because of that zero.

The registry now takes an optional durable store (`source_sync_state`, keyed by
`(scope, source_id)`), following the repo's store idiom: the store owns its DDL on first
touch, and failures are swallowed and logged rather than raised, because sync-state is display
state plus a resume optimisation and a blip must degrade to the old behaviour rather than 500 a
canvas load.

Three decisions worth carrying forward:

- **The cursor is still only written at `_commit`**, from `record_sync`.
  That is this ADR's existing rule and the store does not soften it: persisting `next_cursor`
  any earlier silently skips every unprocessed item in the batch.
- **`register()` is a MERGE, not an overwrite.**
  `build_edition` re-registers its seeded sources on every boot with virgin descriptors, so an
  overwrite would durably zero the row a moment after reading it.
- **The row also carries the connector BUILD RECIPE** (`az_tenant_id`, `drive_id`,
  `folder_path`, `owner_oid`), never a credential.
  Without it `sharepoint:<tid>` could not be rebuilt at all: its drive id arrives on the
  connect request and was written down nowhere, so after a restart the source did not exist and
  `/admin/resync` 404'd on the id the canvas was asking about.

`mark_syncing` deliberately does NOT write through, and a persisted `"syncing"` is coerced back
to `"idle"` on rehydrate: no crawl outlives the process that was running it, and the interrupted
work is resumable through `/admin/resync`.
