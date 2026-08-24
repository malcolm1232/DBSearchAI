# ADR 0006 — Delegated authorization: OIDC token-exchange (OBO), native RLS per cloud

**Status:** Accepted · **Date:** 2026-07-04 · **Source:** Phase E design §11.1 (decided 2026-07-03)

## Context
Phase E federates queries to stores DBSearch does not own (BigQuery, Azure SQL/Synapse,
Redshift/Athena, SharePoint). Each source has its own identity system and row/column security.
If DBSearch maintained its own mapping of "user → what rows they may see" per source, every
mapping bug would be a silent data leak (gate #2 of the leak defense). Someone must enforce
per-row authorization on federated queries — the question is who.

## Decision
**Query as the user.** Standardize on **OIDC/OAuth2 token exchange (RFC 8693, on-behalf-of)**
as the portable delegation primitive. The identity broker (`IdentityPort` extension) exchanges
the user's session token for a source-scoped credential, and the query executes under the
**source's own IAM/RLS**, per cloud:

- **Azure** → Entra OBO flow + Azure SQL / Synapse row-level security.
- **GCP** → Workload Identity Federation + BigQuery row-access policies / authorized views /
  column policy tags.
- **AWS** → STS AssumeRole + Lake Formation row/column permissions (Redshift, Athena).

**Row-policy predicate injection** (DBSearch composes a `WHERE` clause from a policy) is kept
**only as a fallback** for sources with no delegation path, and each such predicate must be
reviewed as security-critical code.

`AccessContext` carries the result either way: `delegated_credential` (preferred) or
`row_policy` (fallback) — see design §9.

## Consequences
- DBSearch stays **out of the identity→policy mapping business**; the enforcement point is the
  same one the customer already audits. A DBSearch bug can produce *no results* but not
  *unauthorized rows*.
- Requires per-cloud broker adapters (E5) and tenant-admin consent flows (Entra app, WIF pool,
  IAM role) at onboarding — a real setup cost, paid once per source.
- Latency: one token exchange per (user, source), amortized by short-lived credential caching.
- The manifest's optional service credential (design §5b) is for **schema introspection only**,
  never for user queries — the manifest must never become a bypass key.

## Alternatives rejected
- **Central service account + DBSearch-side row filtering:** every filter bug is a breach;
  duplicates policy already encoded in the source. Rejected as the primary path.
- **Per-source stored user credentials:** credential sprawl, rotation burden, phishing surface.
- **Predicate injection as the default:** acceptable fallback, unacceptable default — it is
  exactly the identity→policy mapping we want to avoid owning.

## Addendum (2026-07-08, #156) — web-app topology grant

For the topology where the DBSearch canvas server IS the confidential client the
user signs into, Entra delegation uses the **authorization-code + refresh grant**
behind the same `TokenExchangePort` seam (`EntraRefreshExchange`, manifest
`kind: entra_refresh`): one sign-in vaults a multi-resource refresh token
(server-side only), and the broker redeems it per (user, resource). OBO
(`EntraOboExchange`) remains the shape for true middle-tier deployments where a
front-end app presents an access token to a separate API tier. The invariant is
unchanged either way: the query executes as the user; the source enforces.

## Addendum (2026-07-11, #193) - GCP web-app grant + WIF warning

For the same web-app topology, GCP delegation uses **Google sign-in with the
authorization-code + refresh grant** behind the same `TokenExchangePort` seam
(`GoogleRefreshExchange`, manifest `kind: google_refresh`): "Connect Google" links
a Google credential under the existing session identity (one session, N cloud
credentials), the vault holds one refresh token per (user, idp), and the broker
redeems it per (user, resource). Two Google-specific facts: refresh redemption is
NOT scoped per resource (one multi-scope access token; least-privilege comes from
incremental authorization at link time), and no group expansion is needed (every
GCP channel enforces natively against the user's own token).

**Warning - WIF service-account impersonation collapses identity.** The
`GcpWifExchange` second hop (`iamcredentials.generateAccessToken`) makes every
user's query execute as ONE service account: the source sees the SA, not the
user, silently breaking query-as-user. If WIF is ever wired live, map users to
direct `principal://` IAM bindings (no SA hop), or treat the store as
service-identity + row-policy fallback - never present the SA hop as delegation.

## Addendum (2026-08-12, #650/#666) - AWS web-app grant

For the same web-app topology, AWS delegation uses **the user's own vaulted
access keys** behind the same `TokenExchangePort` seam (`AwsKeysExchange`,
manifest `kind: aws_keys`): the account panel's Amazon row vaults the keys once
(falsified against `sts:GetCallerIdentity` first), and the broker redeems them
per (user, resource) via `GetSessionToken` into a temporary triple. No role hop
exists, so no identity collapse is possible - each caller IS their own IAM
principal, and Redshift GRANTs / Lake Formation enforce source-side. `aws_sts`
(AssumeRoleWithWebIdentity) remains the no-stored-secret enterprise shape, but
it requires a real OIDC JWT as its subject, which the refresh-token vault does
not hold - see ADR 0024 for the full decision and the rejected alternatives.
