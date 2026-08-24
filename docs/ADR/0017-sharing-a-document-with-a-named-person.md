# ADR 0017 - Sharing a document with a named person

Date: 2026-08-06 · Status: accepted · Card #538 · Builds on ADR 0012 (document-plane tenant partition), #549 (metadata plane), #539 (private by default)

## Context

#539 made an uploaded document private to whoever uploaded it, which is the safe half of the story.
The other half is missing: there is no way to let a colleague read it.
The HR case that prompted this is concrete - someone ingests their policies and wants a specific person to be able to ask questions about them.

The owner's decision, taken 2026-08-06: **a share is a grant to a named person who signs in**, not a secret link.

That was a real fork and it is worth recording why, because the rejected option is the one that looks friendlier.
A secret link makes the URL itself the credential: forwarding it is an unlogged transfer of access to HR documents, nobody can say afterwards who read what, and revocation is all-or-nothing.
The product's claim is "permission-faithful by construction"; a bearer URL is the one shape that cannot be reconciled with it.
The cost of the chosen option is real and accepted - the recipient needs an account.

## Decision

### 1. A grant is a first-class record, and the ACL never gets rewritten

On grant, a fresh `grant:<grant_id>` principal is added to the document's `allowed_principals`.
The grant record itself holds who it is for, who made it, and when it expires.

The grantee's principal expansion gains `grant:<grant_id>` **only while the grant is live**.

This is the whole design, and every property below falls out of it rather than being bolted on:

- **Revocation is instantaneous.** Delete the record and the principal stops being expanded. The document's ACL is untouched - a dangling `grant:<id>` matches nobody, forever. No rewrite, no sweep, no window in which a revoked reader still reads.
- **Expiry needs no background job.** It is evaluated during expansion, on every request, so it is always fresh. A scheme that materialised the grantee's oid into `allowed_principals` would need a sweeper, and a sweeper that fails to run is an expired grant that still works - a security-relevant staleness we would have to reason about forever.
- **LAW 2 keeps exactly one enforcement point.** The overlap test between the caller's principals and the document's `allowed_principals` is unchanged. Sharing does not introduce a second authorization path that could disagree with the first - which is precisely how permission systems rot.

### 2. Who may share: someone who can see the document *directly*

The requester's **direct** principals must intersect the document's ACL.
Direct means the identity port's own expansion - their oid and their real groups - **excluding** grant principals.

So a share cannot be re-shared.
The person you gave access to cannot pass it on, which keeps the audience a bounded set the owner chose rather than a chain they cannot see.

This is deliberately weaker than a true ownership model, and the trade is stated rather than hidden: a member of a group that holds a document may also share it.
For the case this is built for it is exact, because a `#539` upload is ACL'd to the uploader alone.
`owner_oid` is already stored on every chunk (ADR 0012) but is not exposed through `DocACL`; when a strict owner-only rule is wanted, that is the seam, and it is a change to this ADR rather than a workaround.

### 3. What a grant may not do

- It may not widen a document beyond what its holder can see - it copies access, never escalates.
- It never grants the metadata plane. A grantee reads the document; they do not become an operator (#549).
- It is per document, not per store. Store-level access stays a compose-time concern.

### 4. Not in this slice

The grant registry is in memory, so grants do not survive a restart - accepted while the model settles, and the same position `ApiKeyRegistry` already occupies.
There is no UI yet; this is the API and the enforcement.
Making it durable is a storage decision that should not be taken while the sharing model is still new.
Durability landed via #575: `GrantRegistry` takes an optional write-through Postgres store, and memory stays the only read path, so expiry evaluation and the single LAW 2 enforcement point are unchanged.
Revocation is fail-closed on that store: the delete is attempted there first and memory is only cleared once it succeeds, so a store outage makes revoke unavailable (it raises) rather than let a revoked grant quietly reappear on the next restart's hydration.
`create` keeps the opposite, best-effort stance - a share still works in-process during a store outage, it just does not survive a restart.

## Consequences

A recipient signs in as themselves, sees the shared document in their own workspace, and gets their own conversation - `ConversationService.ask` is already keyed by user, so no transcript is shared along with the document.

The audit trail keeps working and now means something: every read is attributable to a real person, which is the property the secret-link option would have destroyed.

If we later want link-sharing for a genuinely public document, it should be a *different* feature with a different name, not a loosening of this one.
