# ADR 0024 - AWS as a delegated data source: the user vaults their own AWS credential

Date: 2026-08-12 · Status: accepted · Card #650 · Builds on ADR 0006 (delegated auth), ADR 0010 (self-serve credentials), ADR 0022 (delegated schema introspection), ADR 0023 (account linking)

## Context

The owner ruled (260812) that the account panel's Amazon row means **AWS as a data source** (Redshift, later S3/Athena), not "Login with Amazon".
That ruling invalidated the wiring note #648 carried, which prescribed an `enabledFlag`, two OAuth routes and a Google-shaped linking callback.
AWS has no OAuth-refresh-token shape to link: delegated access to AWS data services is IAM - long-lived keys, or STS role assumption.

What exists today, and why none of it reaches the hosted product:

- `AwsStsExchange` (kind `aws_sts`) implements STS `AssumeRoleWithWebIdentity` and serializes the returned triple as JSON, and `RedshiftEngine` already accepts that JSON as a per-user delegated credential (`_user_client_factory`).
  But the kind binds the **entra** subject provider, and on the hosted deployment `_subject_provider` returns the vaulted Entra **refresh token** - not a JWT - so STS can never redeem it.
  `aws_sts` works only with the dev env seam (`DBSEARCH_SUBJECT_TOKEN` holding a real Entra assertion).
  It is an offer with nothing behind it on dbsearch.ai, the same shape as #646/#652/#654/#656.
- The canvas offers "AWS - 1 service" with a Redshift tile, but its `KINDS.redshift` panel collects `cluster`/`database`/`key` while `RedshiftEngine.from_config` requires `workgroup`/`database`.
  A Redshift node configured from the panel cannot compose at all.
- The account panel's Amazon row renders "Not yet supported" (`enabledFlag: null`), which is currently the honest sentence.

The vault question the card raised dissolves on inspection.
`TokenVault` is keyed `(oid, idp)` and since #435 rests IN the Fernet secrets store - the same store that holds user-supplied database passwords (#319/#417).
So "vault vs secrets store" is not a choice of stores; it is a choice of **veneer**, and the vault veneer is the one that buys `linked()` in `/auth/me`, the account-panel row states, `/auth/disconnect/{idp}` (#652), and the broker's fail-closed `not_linked` drop-and-disclose - all for free.

## Decision

**A DBSearch account links AWS by vaulting its own AWS credential, and a Redshift store with a `delegation: {kind: aws_keys}` block runs every query - and introspects schema (ADR 0022) - as that caller's own IAM principal.**

Concretely:

1. **`aws` becomes a known cloud in the vault.** `KNOWN_IDPS` gains `"aws"`.
   The vaulted value is a JSON credential document, not a refresh token: `{"access_key_id": ..., "secret_access_key": ...}`.
   The vault's real contract was never "refresh tokens"; it is "one long-lived per-(account, cloud) credential that an exchange redeems into short-lived per-request credentials", and AWS keys satisfy it exactly.
2. **A new exchange kind `aws_keys`** redeems the vaulted keys via STS `GetSessionToken` into a temporary `{access_key_id, secret_access_key, session_token}` triple - the exact JSON `RedshiftEngine` already parses - cached by `_CachedExchange` for the session's lifetime.
   The block carries **no credential fields at all** (`{kind: aws_keys, resource: redshift}`): the credential is per-caller in the vault, so there is nothing for `DELEGATION_SECRET_FIELDS` to guard, same as `aws_sts`.
3. **Connect is a form, not an OAuth dance.** `POST /auth/aws/connect` takes the two key fields, **falsifies them against STS `GetCallerIdentity` before believing them**, and vaults them under `(session oid, "aws")`.
   Write-only, per ADR 0010's asymmetry: the value goes in once and never comes back out over any API.
   Disconnect is the existing `/auth/disconnect/aws` for free.
4. **Redshift joins ADR 0022.** `RedshiftEngine` gains `introspect_as`, `RedshiftProvider` gains `probe_as`/`build_as`, so a delegated store is probed, health-checked AND queried as the caller - and a caller with no AWS link gets `NotSignedIn` -> "connect Amazon", never a silent empty result.
5. **No delegation block -> ambient server credentials, unchanged.** The self-host topology where the operator's box holds AWS credentials (LAW 1) keeps working exactly as before, mirroring ADR 0022's ADC stance for BigQuery.
6. The canvas `KINDS.redshift` panel is corrected to the fields the engine actually reads (`workgroup`, `database`, `region`), and `delegationFor("redshift")` emits the `aws_keys` block when the node requires sign-in.

## LAW 2

This is the card's hard part - "a shared IAM role is a single principal" - and the decision avoids it rather than solving it: **there is no shared role and no role hop in this shape at all.**
Each DBSearch account vaults its own IAM principal's keys; STS session credentials inherit exactly that principal; Redshift GRANTs / Lake Formation decide what comes back, source-side, per user; CloudTrail attributes per user.
Two callers are two AWS principals end to end - the identity-collapse trap `GcpWifExchange`'s warning documents (every user funneled through one service account) is structurally unreachable here.

The known LAW 2 residue is the same one ADR 0022 already named: anything caching schema or clients must key on the credential, never the store id alone.

## What "Connected" means on the Amazon row

A credential that **validated against `GetCallerIdentity` at connect time** and still decrypts now (`linked()` refuses what it cannot decrypt).
The same two-fact standard as the Microsoft and Google rows - it does not claim the keys still work this second, and the first query says so honestly if they have been revoked at AWS.
Showing WHICH AWS account is behind the row is #662(b), a roster-wide fix, not special-cased here.

## Consequences

- **Long-lived keys at rest, deliberately.** ADR 0006 rejected "per-source stored user credentials" as the *default* path, and this ADR does not reopen that: ADR 0010 already crossed the bridge for self-serve database passwords, and a self-serve user's AWS keys are the same class of credential in the same encrypted store, per account instead of per store.
  The enterprise successor - `AssumeRoleWithWebIdentity` federating from a real Entra/Google JWT against an IAM OIDC provider in the customer's AWS account, no stored secret at all - is `aws_sts`, which stays wired for exactly that future; it needs an id-token minting step the refresh-token vault does not have today, and that is its own card when an enterprise customer wants it.
- `GetSessionToken` validates the keys at redemption and yields expiring credentials, so a revoked IAM key dies at the exchange with an honest STS error, not deep inside a query.
  (Session credentials cannot call IAM APIs; the Redshift Data API is unaffected.)
- `boto3` becomes an optional extra (`aws`), lazily imported (LAW 7), and must be baked into the prod image - declared in `pyproject.toml` AND synced with the Dockerfile to the box, the #654/#655 deploy trap.
- S3/Athena later reuse the identical credential and exchange; only engines/connectors are new.
- The demo Redshift tile keeps its honest refusal until a real AWS rig exists; browser-verified E2E against real Redshift needs the owner's AWS account and is tracked separately (the #659 discipline: the negative proof needs a second principal).

## Alternatives rejected

- **Login with Amazon.** Ruled out by the owner: consumer OAuth, no path to AWS data-plane access.
- **A DBSearch-owned AWS principal assuming a customer role (external-id pattern, the Fivetran shape).** The right shape for a managed SaaS with an AWS control plane - but DBSearch's hosted deployment has no AWS account to be that principal, and LAW 7 says the product must not require one.
  Revisit if the control plane ever grows an AWS footprint.
- **Web-identity federation as slice 1** (`aws_sts` against the user's Google/Entra id token).
  No secret at rest - but it demands the customer create an IAM OIDC provider + role + trust conditions before anything works, an onboarding wall in the product whose PRIMARY OBJECTIVE is fewest-steps connect-and-query.
  It is the enterprise successor, not the first slice.
- **Keys per store entry (in `config:`, like database passwords).** Works, but ties the credential to one node, answers nothing about the account panel's Amazon row, and duplicates keys across N stores with N rotation points.
  The account-level link is what the owner's ruling describes.
