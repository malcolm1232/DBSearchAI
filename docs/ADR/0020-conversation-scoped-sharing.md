# ADR 0020 - Sharing a conversation, and where a conversation-scoped grant is enforced

Date: 2026-08-08 · Status: accepted · Cards #600, #601 · Builds on ADR 0017 (sharing a document with a named person), ADR 0019 (cross-partition document sharing), ADR 0012 (document-plane tenant partition)

**ADR 0019 stays accepted and unedited.** This ADR does not change what a `ReadScope` is or what its doorway does. It changes how a `ReadScope` is READ - specifically, it records that a rule expressed only in the doorway is not enforced at all whenever the two parties share a partition, and moves the conversation rule to the ACL side where the rest of authorization already lives.

## Context

ADR 0017 shares one DOCUMENT with one named person. #600 asks for the other shape the HR case wants: share a THREAD.

Bob has been asking questions about his HR policies. Alice needs the same answers and a few of her own. The owner's decision, taken 2026-08-08:

> The recipient reads the thread and keeps asking her OWN questions, answered from exactly the documents that thread cited, and she never gains general access to the sharer's files.

That sentence has two halves, and the whole of #601 is the discovery that we had built only the first.

- She must be able to ASK - so she needs an authorization over the cited documents.
- She must not gain GENERAL access - so that authorization must be confined to the conversation.

## Decision

### 1. A conversation share is two records, not one

- One `ConversationShare` row, which opens the transcript. It carries `turn_cutoff`: how many of the grantor's turns it hands over.
- One ordinary ADR 0017 grant per document the shared turns cited, each carrying a `conv_id`.

Nothing new is invented for retrieval. The grants are ADR 0017 grants and pass through the same single ACL enforcement point as every other read. What is new is only that a grant can now say WHICH conversation it belongs to.

### 2. Conversation scoping is enforced in PRINCIPAL EXPANSION, not in partition routing

This is the correction #601 exists for, and it is the reason this ADR is worth writing.

**What we built first, and why it looked right.** `Grant.conv_id` was filtered in `_request_scope`, which builds the `ReadScope.doorway`. The reasoning was that ADR 0019 already had exactly one place that routes (the doorway) and one place that authorizes (the ACL overlap), and that putting a conversation opinion in both would let the two drift apart. So the ACL principal was made deliberately conversation-blind and the doorway did the scoping.

**Why it was wrong.** `ReadScope.allows` is:

```python
return tenant_id == self.partition or (tenant_id, doc_external_id) in self.doorway
```

The partition arm short-circuits. When grantor and grantee are in the SAME partition, the doorway is never consulted - so a rule expressed only in the doorway is never applied. And the same partition is not an edge case: it is every self-host deployment and every single-organization Entra tenant, because `resolve_tenant` canonicalizes every home-tenant session onto the deployment's own partition. The shape that DID exercise the doorway - two people in separate `acct:` partitions - was the only shape our tests built, which is why it stayed green.

Reproduced end to end on a single-partition rig, with the recipient's only authorization anywhere being one grant naming one conversation:

| surface | what the conversation share handed over |
| --- | --- |
| `POST /search`, no conversation | the document, the full answer text, the citation, the title, the uri |
| `GET /admin/documents` | the row, its title "Board pack Q3 (confidential)" and its uri |
| `GET /admin/documents/{id}/segments` | 200 with the chunk preview text |
| `GET /admin/documents/{id}/download` | 200, the 566 bytes of the original file |
| `GET /ask/suggestions` | `authorized_docs: 1` |
| `POST /chat` in an unrelated `conv_id` | the document and its content |

**The decision.** A conv-scoped grant's principal is expanded only when its conversation is the active one, in `GrantRegistry.live_principals_for`, and **the default - no conversation active - drops it.**

- Expansion is authorization, and this is an authorization rule. It belongs where authorization already is.
- The ACL overlap is the single enforcement point every read already passes through, so one change covers every surface at once, including surfaces nobody thought to enumerate.
- The default is what makes it safe. A read path that has no conversation concept says nothing, and gets less. Widening requires naming a conversation explicitly; nothing can widen by omission. Every existing caller became correct without being edited.

`ReadScope` gains `active_conv_id` so that "where this request may look" and "which conversation it is looking from" travel together instead of being derived twice. It deliberately does **not** participate in `allows()` - putting it there would repeat the mistake this ADR corrects.

`dbsearch.ports.base.expand_principals` is the single seam that carries the scope into expansion. An identity that has never heard of conversations falls back to plain `expand_groups`, which is the narrower answer.

### 3. The doorway filter is kept, as the backstop

It is not deleted, and the docstrings now say which is which. Across partitions the doorway does real work: a grantee in her own `acct:` partition reaches a grantor's document only through a pair, so withholding the pair outside its conversation is a second, independent refusal. Defence in depth - but the guarantee is the ACL side, and the code must never again claim otherwise.

### 4. The share is a snapshot of a transcript, not a subscription to a thread

`turn_cutoff` freezes which turns the recipient reads, recorded when the share is made. Documents cited later are not included; turns added later do not travel. A fresh share is how new material is included, and it updates the same row.

A count rather than a timestamp because that is what the data supports: a `Turn` carries no time, and history is oldest-first and append-only per `(conv_id, user_oid)`.

### 5. Withholding propagates through the thread

A turn travels only if every document it drew on is one the sharer may pass on (ADR 0017 s2 - a share cannot be re-shared, and a thread cites whatever the sharer could read, including documents she holds only through somebody else's grant).

Withholding does not apply per turn. `ConversationService.ask` condenses each follow-up against a history window that includes prior ANSWERS, so a turn citing only shareable documents can have been generated from a question synthesized out of a withheld turn's content, and a model that restates the question in its answer carries it forward. **Once a turn is withheld, every later turn is withheld.** The shared half is the prefix up to the first withheld turn - which also keeps it contiguous, chronological, and expressible as the single count in 4.

Withheld turns are dropped, not refused: one received document must not poison an otherwise legitimate share. The count of what did not travel goes back to the SHARER only, as `turns_withheld`. The recipient is not told how much was held back, because that count is itself a fact about documents she may not know exist.

### 6. What the recipient may read equals what she may retrieve

The transcript re-checks each turn against the documents this share actually granted. It is one invariant carrying both channels: **you may read a turn exactly when you may retrieve everything it drew on.**

This was learned the hard way, twice. Filtering only the grants left the answer TEXT of a refused document travelling in the transcript - the same disclosure by a different channel, and worse for that document's owner than the bug it replaced, because with no grant minted she saw no trace of it at all.

### 7. One live share per (conversation, recipient, sharer)

Re-sharing updates that row rather than adding a second. Duplicates were three defects at once: the later share was ignored for the transcript, revoking the older one WIDENED what the recipient could read, and revoking either dropped every conversation grant while leaving a survivor row open. `grantor_oid` is in the key because `conv_id` is client-chosen and two people can hold threads under the same id - which is also why the share route's grant cleanup is scoped to the caller's own grants.

## Consequences

**Accepted.**

- A recipient who also holds an ordinary document grant on a cited document still sees that turn withheld, because only this conversation's own grants are counted. Conservative; never a leak.
- A re-share can NARROW what the recipient could previously read, if a cited document has since become unshareable. Correct - the new share is the sharer's current decision - but visible, and the surface should say so rather than let it look like data loss.
- `turn_cutoff` reaches the recipient on the share record. Harmless: after 5 the shared half is a contiguous prefix, so it is exactly the number of turns she is already reading.
- **A single app worker is a CORRECTNESS constraint here, not a scaling default.** `GrantRegistry._by_id` and `ConversationShareRegistry._by_id` are per-process caches hydrated once at boot, and nothing invalidates them across processes. Under `--workers 2` a revoke served by worker A leaves the grantee still reading, and still retrieving, through worker B until the box restarts. That is BROKEN REVOCATION, not merely the duplicated share row 7 talks about, and a unique constraint on `(conv_id, grantor_oid, grantee_oid)` would not touch it - the stale row is in memory, not in the table. The Dockerfile's `CMD` runs uvicorn with no `--workers`, so this holds today. Anyone adding workers, or a second replica behind a load balancer, must first give both registries cross-process invalidation (read-through, a pub/sub bust, or a short TTL); until then the constraint belongs in the deploy notes, not in a comment. The same property is what forces the ordering in the Rollback section below: a cache that is only ever hydrated at `__init__` cannot be corrected by a `DELETE`, only by a restart.
- **The recipient reads the thread, but the model does not.** `ConversationService.ask` condenses each follow-up against history keyed by `(conv_id, user_oid)` - the CALLER's own key. Bob's turns are rendered to Alice through the transcript, but they never enter her condense window, so her follow-ups are retrieved standalone: "what about part-timers?" after reading Bob's parental-leave answer resolves against nothing. This is the SAFE direction, and deliberately so - feeding Bob's answer TEXT into Alice's prompt would reopen the content channel 6 spent two rounds closing, and would do it in the one place we cannot re-check per document. But it is a real product consequence: the thread is context for the HUMAN, not for the retriever. If continuity is wanted later it has to be built as an explicit, per-document re-checked window, not by widening the history key.
- **The new tables assume the app's DSN role can read them.** The design spec called for an explicit `GRANT` to the app role on `conversations` and `conversation_shares`; it was never implemented, and it is moot only because the shipped deployment connects as `postgres`, which owns every table it creates (`docker-compose.yml`, `PGVECTOR_DSN`). A self-hoster pointing the app at a non-owner role gets the failure this repo has been bitten by before: `load_all` raises, `GrantRegistry.__init__` and `ConversationShareRegistry.__init__` swallow it by design (a store outage must cost durability, never the ability to serve), and the box boots with ZERO grants and ZERO shares. It renders as a perfectly normal "no documents you are permitted to see" - a permissions answer with a permissions shape, produced by a permissions bug. The log line is the only tell.

**Rollback is a data step, not just the old image.**

If this ships and is later REVERTED, the pre-branch code cannot see `doc_grants.conv_id`: `GrantRegistry.live_principals_for` has no conversation filter, `_request_scope` has none either, and `PgGrantStore.load_all` selects an explicit seven columns that simply do not include it. Every conv-scoped grant therefore comes back as an ORDINARY unscoped ADR 0017 grant - the row does not change, the code that narrows it disappears. Each conversation recipient silently gains general access to every document that conversation cited: `POST /search` with no conversation, `GET /admin/documents`, the segment previews, and the file bytes from `/download` - exactly the leak table in section 2, which is what this ADR exists to prevent.

So any revert of #600/#601 MUST also run:

```sql
DELETE FROM doc_grants WHERE conv_id IS NOT NULL;
```

**BEFORE the old image comes up, and that ordering is part of the fix, not a preference.** Run afterwards it is a NO-OP against the running box: as the single-worker bullet above records, `GrantRegistry` hydrates `_by_id` once in `__init__` and every read serves from that dict, so the old process has already loaded each conv-scoped row as an unscoped grant and will never re-read the table. The `DELETE` reports success, the shell history looks like a clean revert, and the recipient keeps unscoped access for the life of that container. If the old image is already up, the recovery path is the `DELETE` **and then a restart of the api container** - nothing else re-hydrates the registry.

Run first, it is simply safe: the new code stops honouring those shares, which is a refusal, not a leak. It is idempotent, and nothing is lost that re-sharing cannot recreate. Dropping the column is NOT a substitute - the old `INSERT ... ON CONFLICT` would keep working while the widened rows stayed live. The full sequence, forward and back, is in `docs/DEPLOY_CONVERSATION_SHARING.md` (not in the public tree, #685 - it is a runbook for our own
hosted box); this ADR is the reason, that file is the procedure.

**Rejected.**

- *A ContextVar for the active conversation.* Implicit request-scoped global state does not survive the SSE generator boundary, which the streaming path already has a comment about, and it would make the scope invisible at the call sites that must be auditable.
- *Widening `IdentityPort.expand_groups` for every adapter.* A parameter only one wrapper can honour, carried by every adapter, is contract rot. The optional `expand_groups_scoped` plus a fallback keeps the port unchanged and fails narrow.
- *Storing the shared document set on the share row.* A second stored list can drift from the grants. Making the grants the record means a grant that later goes away takes its turn with it, rather than leaving orphaned readable text.
