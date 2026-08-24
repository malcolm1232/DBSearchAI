# ADR 0023 - Linking Microsoft to an account you are already signed in as

Date: 2026-08-12 · Status: accepted · Card #646 · Builds on ADR 0013 (identity model), ADR 0011 §5 / ADR 0018 (partitions), ADR 0006 (delegated auth)

## Context

`/auth/callback` minted a session unconditionally. It never read the existing one:

```python
token = user_auth.sign_session({"oid": u["oid"], ..., "idp": "entra"})
```

So a user already signed in as `avery@example.com` who reached Microsoft sign-in did not *connect* Microsoft to that account. They were silently re-principaled to the Entra `oid`: a different account, a different partition, and their email-account workspace, conversations and stores simply not there. Same human, two accounts, no warning, no error.

Google has done the opposite since #193, and says so in the code:

> Account linking (#193): keep the identity the user is ALREADY signed in as, and hang the Google credential off it. Only a signed-out user becomes their Google identity.

**The asymmetry is the defect.** Entra never got #193's treatment. The owner found it by signing in with email and pressing Connect on the Microsoft row - a row that could not succeed, in front of a path that would have swapped their account had it worked.

It hid because every previous check used the owner's *Microsoft* account, where the credential is vaulted at callback and the row reads "Connected". An email session is the first thing that exercises the local-to-Entra cell at all.

## Decision

**Entra gets #193's treatment.** One route decides mint-vs-link by reading the session, exactly as Google's callback does. `/auth/callback` becomes a three-way branch on the *post-exchange* identity:

| Session | Verified Entra oid | Behaviour |
|---|---|---|
| none | - | **Mint** - unchanged: account row, groups, tid, vault, cookie |
| present, `oid == u["oid"]` | the same human | **Mint** - unchanged. This is a re-authentication (the "Sign in again" pill), not a swap |
| present, `oid != u["oid"]` | a different identity | **Link** - vault the Entra refresh token under the *session's* oid; the cookie is not touched |

`/auth/entra/link` starts the leg and requires a session. It reuses `AUTH_REDIRECT_URI`, so **no Azure app-registration change is needed** - which is what keeps this a code-only fix.

The link branch inherits the refusals Google's callback already learned:

- **The Entra identity already belongs to another DBSearch account** → refuse. `ACCOUNTS.link` returns the pre-existing owner rather than re-pointing it; vaulting anyway would report "Connected" while the account graph says otherwise. The message names neither account - it must not become an oracle for who owns what.
- **No refresh token came back** → refuse. Saying "Connected" and failing on first use is the lie Google's callback already declines to tell.

## What a link buys, and what it does not

**Credential only.** The link writes nothing to the session: no `tid`, no `set_user_groups`, no partition change. An email account that links Microsoft keeps its `acct:<oid>` partition.

So "Connected" means **"DBSearch can redeem Microsoft as you"**, not "you are in your org's workspace". That is exactly what a Google link has always meant (`app.py` writes `tid: ""` for a minted Google session deliberately), so the panel keeps one meaning across all providers rather than two.

This is the conservative half of the decision and it was chosen on purpose. Promoting a linked session to carry the verified `tid` and the user's transitive Entra groups would let an email/password account read the org's tenant partition - effectively merging two partitions on the strength of an OAuth round trip. That may well be what users eventually want; it is a separate decision with its own LAW 2 analysis, and it is not this one.

**Known limit, stated rather than discovered:** SharePoint ingest is gated on a real tenant consent flow independent of the vaulted credential, so a linked local account holding an Entra token still cannot establish a SharePoint *connection*. The credential is redeemable for delegated calls; the connector gate is a different door.

## Consequences

**The silent swap becomes impossible through the linking route**, and stays possible only where it is correct: a signed-out user signing in, and a signed-in user re-authenticating as themselves.

**One session can now hold two clouds' credentials under a non-Entra principal**, which the vault has supported since #193 (`(oid, idp) -> refresh_token`) and never exercised on the Entra side.

**Sessions that predate this keep working.** Nothing about the cookie's shape changes; the branch reads only `oid`.

## Alternatives rejected

- **Refuse honestly** - tell a locally-signed-in user Microsoft cannot be connected, and offer nothing. Cheap and not a lie, but it deletes a capability the product implies, and leaves Google and Microsoft behaving differently for no reason a user could infer.
- **Merge on verified email** - treat matching verified emails as the same human and join the accounts. This joins two partitions on a claim we did not verify ourselves. LAW 2 lives here; rejected outright rather than deferred.
- **A second redirect URI** (`/auth/entra/link/callback`) - cleaner to read, but it requires an Azure app-registration change on every deployment, turning a code fix into an operations task for every self-host operator.
