# ADR 0019 - Cross-partition document sharing: the doorway

Date: 2026-08-08 · Status: accepted · Card #582 · Builds on ADR 0012 (document-plane tenant partition), ADR 0013 (identity model), ADR 0017 (sharing with a named person), ADR 0018 (per-account partitions)

## Context

Every chunk row carries a `tenant_id`, and every retrieval runs two filters in a fixed order: the partition filter first (`WHERE tenant_id = %s`, ADR 0012), the ACL overlap second (`allowed_principals && caller_principals`, LAW 2).
A grant (ADR 0017) writes its `grant:<id>` principal onto the document's ACL inside the GRANTOR's partition, but the grantee retrieves through THEIR OWN partition (`resolve_tenant`, api/auth.py).
When those partitions differ, the ACL overlap never gets a chance to run: the grant principal is inert, the share returns 200, and nothing was shared.

The final review of #572-576 found this class (Fix 2), and the owner ruled: refuse honestly now, design the real fix in an ADR.
`_refuse_cross_partition_share` (server/app.py) is that refusal, and it is deliberately incomplete.
It refuses the two cases it can prove dead from the partition value and the grantee identifier's shape alone, and it lets one case through silently: grantor in a tenant partition, grantee GUID-shaped but living in a FOREIGN Entra tenant.
That case is undetectable today for a stated reason: ADR 0013's account store records `(idp, subject) -> account_id` rows and nothing else, so no identity's verified `tid` is on record and the grantee's partition cannot be computed.

Meanwhile ADR 0018 made `acct:` partitions common (every Google and local account has one), so "share with a colleague" now routinely means "share across a partition boundary".
The refusal is honest, but the product's answer to its own headline scenario is "no".

## Decision 1 - How far a grant may reach

**A grant may cross account boundaries within one deployment. It may never cross a foreign Entra tenant boundary, in either direction.**

Allowed, through the mechanism below: `acct: <-> acct:`, `acct: <-> home tenant`.
Every identity involved signed in on THIS deployment and every chunk involved was ingested on THIS deployment.

Refused, permanently and now completely: any share where either side's partition is a foreign Entra `tid`.
"Never let customer document content leave the customer tenant" (CLAUDE.md, locked) is read literally: a foreign tenant's partition belongs to that organization, and a grant that opened it - or opened ours to it - would move content across an organizational boundary through a path neither organization's admin can see or govern.
If federation between tenants is ever wanted, it is a different feature with a different name and its own ADR, not a loosening of this one.

Because Decision 2 records each identity's tenant, this refusal stops being a shape heuristic and becomes a statement of fact, closing the silent case for good.

## Decision 2 - Record the fact the refusal was missing: tid and email per identity

`account_identities` (server/accounts.py) gains two nullable columns, `tid` and `email`, written on every login alongside the existing best-effort `resolve` calls (Entra, Google, local - server/app.py).
Entra's `exchange_code` already returns both verified values; Google's id token carries a verified email and no tid; a local account's subject IS its email and has no tid.
The write stays best-effort for the same reason `resolve` is: a down account store must never turn a working sign-in into a 500.
A row whose `tid` was never recorded (the person has not signed in since this shipped) is treated as unknowable, and a share to them is refused with "ask them to sign in once" rather than guessed at - fail-closed, never fail-open.

The partition-canonicalization rule (home tid -> deployment constant, foreign tid -> itself, no tid but real oid -> `acct:<oid>`, otherwise `""`) currently lives only inside `resolve_tenant`.
It is extracted into ONE function, `canonical_partition` (api/auth.py), that both `resolve_tenant` and the new `account_partitions(identities, account_id, default_tenant)` call.
A pinned test asserts they agree on every branch (`tests/selftest_582_partition_rule.py`).
This is not tidiness: if the session-time rule and the share-time rule ever disagree, a grant opens a doorway onto a partition the grantee never actually reads - which is this bug again wearing a different hat.

Both live in `api/auth.py` rather than on the account store: the store holds identity ROWS, and the layer that knows the deployment constant owns the RULE.

`account_partitions` returns a SET, not a scalar, because an account with several linked identities (Entra + Google, say) legitimately resolves to different partitions depending on which route it signed in through.
That is a pre-existing property of ADR 0013 + 0018, stated here rather than papered over.

It returns `None` - UNKNOWABLE, distinct from empty - when an Entra identity has no recorded tid.
That distinction is load-bearing rather than fussy: `canonical_partition("", oid, ...)` yields `acct:<oid>`, so treating "no record" as "no tenant" would hand a foreign-tenant Entra user a private partition they do not have, allow the share, and retrieve nothing - the original failure rebuilt inside its own fix.
An account with no identity rows at all is a different case and is ALLOWED, because Decision 3 enforces the boundary at read time when that person's real partition finally exists; refusing it would break sharing with a colleague who simply has not signed in yet.

## Decision 3 - The mechanism: a per-(partition, document) doorway

Read paths stop taking a bare `tenant_id` and take a `ReadScope`:

```
ReadScope(
    partition: str,                                  # the caller's own, from resolve_tenant
    doorway:   frozenset[tuple[str, str]],           # (tenant_id, doc_external_id) pairs
)
```

The retrieval predicate becomes:

```
WHERE ( tenant_id = %(own)s
        OR (tenant_id, doc_external_id) IN %(doorway)s )
  AND allowed_principals && %(principals)s
```

The doorway is built in exactly one place, next to `_request_tenant`, from the caller's LIVE grants: `{(g.tenant_id, g.doc_external_id) for g in registry live for this oid}`.
The grant record has stored its `tenant_id` since ADR 0017, so no new state is needed.
Nothing the client sends ever reaches the doorway - it is server-derived, same discipline as the partition itself.

Why this shape and not the smaller alternatives, both considered and rejected:

- **Widening the partition to a set** (`tenant_id = ANY(own + granted partitions)`) is a smaller diff, but it hands the grantee's query the grantor's ENTIRE partition and leaves the ACL overlap as the only thing standing.
  Defence in depth drops from two independent filters to one.
  With the doorway, a bug in the ACL test still cannot expose more than the exact documents already deliberately shared.
- **Copying chunks into the grantee's partition** needs no retrieval change at all, but it duplicates content, and revocation needs a sweep - the exact security-relevant staleness ADR 0017 was designed to never have.

**Where Decision 1 is ENFORCED, corrected during implementation.**
This ADR originally placed the foreign-tenant guarantee at share time, and that is not sufficient.
A grant may name an account with no identity rows yet - a colleague who has not signed in - so the share-time check has nothing to inspect; if that person later signed in from a foreign tenant, the doorway would hand them a home-tenant document by a route the share-time check never saw.
So the doorway is withheld from any foreign-partition session outright, decided in `_request_scope` against the session's own verified partition, which is the only place both partitions are known as fact.
Share-time refusal remains, as honest early feedback; retrieval is the guarantee.
A grant whose GRANTOR partition is foreign contributes nothing either - nothing in the API can create one now, but a grant made before this shipped can still be rehydrated from Postgres on restart.

Properties preserved, by construction:

- The ACL overlap stays the ONE enforcement point (ADR 0017 s1).
  The doorway is partition ROUTING, not authorization: it decides where retrieval may look, and the ACL still decides what the caller may see there.
- Revocation stays instantaneous and expiry still needs no sweeper: the doorway is rebuilt per request from live grants, so a revoked or expired grant's pair simply stops appearing.
- `ReadScope(partition, frozenset())` is byte-for-byte today's behaviour, so every existing caller and test is untouched by the type change alone.

Scope of the change: the read methods of `IndexPort` (`search`, `corpus_status`, `list_doc_acls`, `list_doc_segments`) and the Edition read paths that feed the user-facing surfaces, including `document_bundle`.
Write and ownership methods - `upsert`, `delete`, `add_doc_principals`, `docs_owned_by` - keep the mandatory scalar `tenant_id`.
A grantee therefore CANNOT delete, re-ingest over, re-ACL, or (per ADR 0017 s2, unchanged - `_may_share` uses direct principals only) re-share the owner's document, structurally.

The Azure `aisearch` adapter RAISES on a non-empty doorway until it is a tested path (LAW 7).
A share that silently returns owner-only results is the precise bug this ADR exists to kill; it must not reappear as an adapter gap.

## Decision 4 - What the grantee gets: every read surface the owner has

Ask/search with citations, the document listed in their own "Your data" (attributed as shared-by, not owned), per-document segments, and download of the text and original bytes.
Anything less produces a citation the grantee can see but cannot open, which reads as a bug and invites a workaround (mailing the file), defeating the audited share.
The audit trail keeps working: every read remains attributable to a real signed-in person (ADR 0017's whole point).

## Decision 5 - Naming the grantee: by email, resolved to an existing account

`POST /documents/{doc_id}/grants` accepts `grantee_email`, resolved through the recorded `account_identities.email` to a real `account_id`.
`grants.py` is unchanged and still stores an account id; email is a lookup key, never an authorization value.
Raw account ids remain accepted for compatibility, but email is the product surface: nobody knows their colleague's `acct_<hex>`.

An unknown email is refused honestly: "nobody has signed in with that address yet".
The cost is stated, not hidden: this tells an authenticated user sharing their own document whether an address has an account here - a mild existence oracle, accepted.
Pending invites (grant now, activates when they sign up) are explicitly NOT this ADR: storing an authorization decision against an unverified identifier, where whoever claims the address first inherits the grant, is a different feature with its own threat model.

`_refuse_cross_partition_share` is rewritten from two shape heuristics plus one silent pass-through into one rule over recorded facts: resolve the grantee's partitions, refuse if either side sits in a foreign tenant (Decision 1) or if the grantee's partition is unknowable (Decision 2), otherwise share - and now it works.

## Not in this slice

- UI affordances beyond the existing share/audience picker sending an email: "shared with you" labelling in Your data, grantee empty states - a follow-up card.
- Pending invites (above).
- Sharing a CONVERSATION.
  #596 records that conversations are in-process and die on every deploy; a conversation share would evaporate.
  Documents first, deliberately - the conversation store must become durable (#596) before conversation sharing is designable.

## Consequences

LAW 5 (tenant isolation) gains its one deliberate, narrow exception, and this ADR is its definition: a read-only, per-document doorway, derived server-side from a live named-person grant, never crossing a foreign tenant boundary, revoked the moment the grant dies.
Everything about the exception is enumerable at runtime (the doorway set), auditable (the grant records), and reversible (revoke; the ACL was never rewritten).

The partition filter is no longer the single line `tenant_id = %s` in each adapter, which is a real cost: two adapters (local, pgvector) must implement the doorway predicate identically, and a third (aisearch) must refuse it.
The pinned agreement test (Decision 2) and the two-partition test suite below are the countermeasure.

Two tests that pinned the OLD contract were FLIPPED rather than deleted, and each says so in its own docstring - `selftest_538_document_grants.py`'s "a share out of an `acct:` workspace is refused" and "a share to a non-Microsoft account is refused".
Both were correct when the grantee could never retrieve; both are now the bug.
They were rewritten to assert RETRIEVAL rather than a status code, because a 200 that shares nothing was the original defect and a status code is exactly what it got right.
`selftest_tenant_partition.py`'s signature check was renamed (`tenant_id` -> `scope`), not relaxed: the property it guards is unchanged.

## Verification

Shaped by how this class survived so long - every share test on the branch put both parties in the home tenant.
`tests/selftest_582_share_across_partitions.py` puts them in two DIFFERENT partitions for every pairing this ADR allows (acct->acct, acct->home) and every pairing it refuses (foreign in either direction, unknown email, unrecorded tid), plus a foreign-tenant reader holding a live grant, and a legacy grant naming a foreign partition built directly on the registry.

Each guard is mutation-tested before it is believed. Four were: the doorway predicate, the share-into-foreign refusal, the share-out-of-foreign refusal, and the unknowable-tid refusal.
Two findings came out of that and are worth recording, because both were guards that looked protected and were not:

- The pgvector/`ReadScope` parity test initially SURVIVED its mutant. Its helper reimplemented the rule in Python instead of reading the emitted SQL, so gutting the SQL join condition - turning every doorway pair into a whole-partition key - left it green. It now derives behaviour from the emitted SQL text.
- The doorway's foreign-partition filter had no failing test, because the share-time refusal had just closed every API route into that state. A test now builds such a grant directly on the registry, which is how one really arrives: a pre-#582 row rehydrated from Postgres on restart.

Browser-verified in Chrome across two real `acct:` accounts, which is what found the last defect: the API was correct and the PAGE was not.
Before the share the grantee's Ask says "I couldn't find anything matching that. No documents are indexed yet"; after it, the same question in the same session answers with a citation and "Answered from 1 of the 1 document you can access".
The grantee's listing renders the row badged "Shared with you" with actions [Check text, Download]; the owner's is unchanged, badged "Only you" with [Share, Check text, Download, Delete].
Share had been drawn on the grantee's row and could only 404 (ADR 0017 s2) - the #551 tile-that-always-fails trap, which no test caught because no test had ever rendered a listing containing somebody else's document.
