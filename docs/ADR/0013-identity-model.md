# ADR 0013 - Identity model: auto-provisioned accounts, opaque keys, explicit linking

Date: 2026-07-31 · Status: accepted (implemented 2026-08-07, #572) · Builds on ADR 0006 (delegated auth), ADR 0010 (self-serve credentials), ADR 0011 (multi-tenant sign-in)

## Context

"Anyone signs in with their own account and queries their own DB" is live end to end, and #386 finally put a door on it.
But the account model underneath was never decided.
It accreted, one callback at a time, and it now has a defect (#442) that cannot be fixed without settling it.

**What exists today, verified by reading the code rather than assumed:**

There is no signup, no users table, and no approval queue.
`/auth/callback` (`app.py:914`) exchanges the code, signs a session cookie carrying `{oid, name, email, tid}`, and returns to the canvas.
The workspace materializes lazily, on first write, as a `user_manifests` row keyed `owner_oid` (`manifest_store.py`).
So an account is auto-provisioned already, and the workspace *is* the account.
The operator allowlist (`operators.py`) gates operator affordances only, never sign-in, so a stranger is not turned away.

Two IdPs mint identities, and they do not agree on what the key is.
Entra sets `oid` to the Entra object id, a GUID.
Google sets it to the **verified email address** (`app.py:1011`, `oid = (sess or {}).get("oid") or u["email"]`), deliberately and with a documented reason recorded at the top of `google_auth.py`: BigQuery `SESSION_USER()` row-access policies, Cloud SQL IAM users and Drive ACLs all key on the email, so the session string, the store ACL and the source-side policy must all read the same thing (LAW 2).

Linking already exists and is already the right shape in one direction.
`/auth/google/callback:1008` keeps the identity the caller is **already signed in as** and hangs the Google refresh token off it.
Only a signed-out caller *becomes* their Google identity, at line 1011, via `oid = u["email"]`.

**The defect that forces this ADR (#442).**
One human who signs in with Microsoft on Monday and with Google on Tuesday while signed out owns two distinct `user_manifests` rows: one keyed on a GUID, one keyed on an email string.
The second is an orphan.
Their stores are missing, nothing in the UI reveals a second workspace exists, and there is no way to merge.
It reads as data loss.

The tempting fix is to link the two when the email matches.
That fix is unsafe, and the reason is the whole point of this ADR.
The Entra `email` / UPN claim is **tenant-controlled and unverified**: anyone who controls any Azure tenant, including one they created five minutes ago, can mint a user carrying an arbitrary email address, including a victim's.
Google's email, by contrast, *is* verified, because `google_auth.py:114` refuses `email_verified: false` outright.
Auto-linking on the pair would import the weaker of the two guarantees and silently undo the stronger one.
The blast radius is not cosmetic: a workspace holds `secret://` handles to its owner's databases (ADR 0010), so a wrong link hands an attacker the victim's database credentials.

## Decision

**1. Auto-provisioning is ratified, and made explicit.**
First successful sign-in creates an account.
No signup form, no email verification of our own, no approval queue: the IdP has already proven everything a form would ask, and a form would only add a step that proves nothing.
The change is that the account becomes a **row written at callback time**, not an implicit side effect of the first manifest write.
Today an account that has never connected a store is indistinguishable from one that never existed, so "how many people have signed in" is unanswerable and there is nowhere to hang `created_at`, `last_seen`, or the linked-identity set that decision 3 needs.

Rejected: a signup form or an invite gate.
Both contradict the self-serve wedge, and neither adds a fact the IdP has not already established.

**2. The account KEY and the authorization PRINCIPAL are split.**
This is the load-bearing decision, and it is what makes #442 fixable at all.

One string currently does two unrelated jobs: it says *which workspace is mine*, and it is the value compared against source-system ACLs.
For Entra the two coincide harmlessly, because the oid is genuinely both.
For Google they were **forced** to coincide, which is why the account key ended up being an email.
While they remain one field, the Google account key cannot be changed to something stable without breaking the LAW 2 match that email exists to satisfy.

So:

- **`account_id`** is opaque, internal, generated at first sign-in, and derived from nothing mutable. It is the only thing that keys a workspace.
- **`identity`** is `(idp, subject)` using the IdP's own immutable subject: Entra `oid`, Google `sub`. An account has one or more.
- **`principal`** is the string a *given source system* recognizes for LAW 2 comparison: the oid for Entra-backed sources, the verified email for Google-backed ones. It hangs off the identity, and it is explicitly allowed to be mutable, because an email can change while the account must not.

`account_identities (idp, subject) -> account_id` becomes the lookup every callback performs.

**3. Linking is explicit, authenticated, and never inferred.**
An identity may be attached to an account only by a caller who is **already authenticated as that account** and who then completes a full OAuth round on the second IdP within that session.
That is exactly what `/auth/google/callback` does when a session exists; this decision formalizes it as the *only* path and forbids the alternative.

Never link on a matching email, matching display name, or any other claim.
Rejected explicitly, for the reason in the Context: the Entra email claim is attacker-controllable, so email-linking is an account-takeover primitive against a store of database credentials.

**4. A signed-out Google sign-in is a sign-in, and the mapping table resolves #442.**
On a signed-out Google callback, look up `(google, sub)` in `account_identities`.

- **Found** (the user linked this Google account to their workspace earlier): sign them into that existing account. This alone eliminates the orphan for every user who has ever linked, and it is the actual fix for #442.
- **Not found**: create a new account, and say so plainly in the UI. A Google-primary customer with no Entra tenant at all is a legitimate first-class user, and refusing them to avoid an orphan would be the wrong trade.

If the freshly-authenticated Google identity is not linked but the workspace list shows the user has another account reachable by linking, the UI may offer to merge.
That offer is safe to make only *after* the OAuth round has completed, because at that point the caller has proven control of the identity in question, so nothing is disclosed to an unauthenticated party.

Merging two populated workspaces is additive and one-way, must not silently collide store ids, and is logged.
It is specified here but is the last thing to build, because decision 4's lookup removes the common case.

Rejected: refusing signed-out Google sign-in outright. It would fix #442 by deleting a legitimate customer shape.

**5. AWS is deferred, with the reason recorded.**
No AWS IdP is added.
The only AWS source in the canvas palette is Redshift, and its config fields are `cluster` / `database` / `key` with **no `require_signin`** (`canvas.html:657`), so it has no query-as-user path for an AWS identity to serve.
An AWS IdP today would buy zero LAW 2 capability and add a third key shape to a model that is being unified precisely because it has two.
Revisit when Redshift gains delegation.
Decision 2 is what makes this cheap to revisit: a third IdP is a new `(idp, subject)` row type, not a migration.

**6. An account is not an organization, and not a billing subject.**
Tenant grouping stays `tid`, owned by ADR 0011 and ADR 0012.
An Entra identity carries its tenant partition; a Google-only account has no Entra tenant, which is exactly why its session `tid` is `""`.

**Superseded in part by ADR 0018 (260807).** This line used to continue "...and the document plane refuses it (`app.py:1017`, ADR 0011 s5)."
It no longer refuses it: a verified no-tid session partitions to its own private `acct:<oid>` corpus (ADR 0018), so a Google or local email/password account can ingest and retrieve inside a partition nobody else can read.
Decision 6 itself is unchanged - an account is still not an organization, and tenant grouping still keys on the verified `tid`.

## Consequences

**Migration is additive, and there is no data migration on day one.**
Add `accounts` and `account_identities`.
Seed `account_identities` from the existing `user_manifests` keys: `(entra, <oid>) -> <oid>` and `(google, <email>) -> <email>`, with `account_id` set to the existing key string.
Existing rows therefore keep working untouched, because their account_id is simply their old key.
Only accounts created after this ADR get a generated opaque id.
The one-way door being closed is the *shape*, not the current values.

**Laws upheld.**
LAW 2 is strengthened, not weakened: the principal used for ACL comparison is unchanged for both IdPs, and the class of bug that could substitute one user's principal for another's (email-linking) is ruled out by construction rather than by care.
LAW 5 holds: account ids are opaque and globally unique, so no cross-tenant path is created; tenant partitioning is unaffected and still keys on verified `tid`.
LAW 6 holds: all of this is durable state in Postgres, none in compute.

**Costs.**
Two new tables and a lookup on every callback, on a path that already does a network round trip to an IdP, so the cost is not measurable.
A Google-primary user who has never linked still gets a second workspace until they merge; this ADR makes that state visible and recoverable rather than silent, which is the honest half of the fix, but it does not make it impossible.
The merge operation is real work and is deliberately sequenced last.

**Out of scope, deliberately.**
Publisher verification (#428) and the consent-screen display name (#444), which are Entra app-registration branding, not identity modelling.
Personal Microsoft accounts, still excluded by the `/organizations` authority (ADR 0011).
Any change to how `secret://` handles are scoped (ADR 0010): they stay owner-scoped, and owner continues to mean the account key, which is why decision 2's migration deliberately preserves existing key values.
Billing, quotas, and anything that would make an account a commercial subject.
