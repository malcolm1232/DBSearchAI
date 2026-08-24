# ADR 0022 - The caller's own credential introspects a delegated store's schema

Date: 2026-08-12 · Status: accepted · Card #656 · Builds on ADR 0006 (delegated auth / OBO), ADR 0008 (per-source retrieval mode), ADR 0013 (identity model)

## Context

`BigQueryProvider` was built with two identities, and said so in its own docstring:

> Two identities: server-side ADC introspects schema; a `delegation: {kind: google_refresh}` block makes user queries run as the user.

That split is right for the topology it was written for - an operator-owned warehouse, where the operator holds a service identity for the project and users are given trimmed views of it.

It is unusable for the topology the product now sells. #193 invites a user to link *their own* Google account so queries run as them against *their own* BigQuery. dbsearch.ai is a hosted deployment: the operator has no Application Default Credentials, and could not meaningfully have them for a user's personal GCP project even in principle.

The result, verified on prod on 260812 with a genuinely linked Google account, a consented `bigquery` scope and a vaulted refresh token:

```
✗ probe · 154ms  unreachable: Your default credentials were not found.
```

Everything the user was asked to do had been done. The capability was still unreachable, because introspection insisted on an identity the deployment does not have and should not need. `GOOGLE_APPLICATION_CREDENTIALS` is unset on the box and there is no gcloud ADC directory - correctly, for a self-host deployment.

This is the third instance this week of the same shape: an offer with nothing behind it (see #646, #652, #654).

## Decision

**When a store declares a `delegation:` block, the caller's own delegated credential introspects its schema.** ADC is used only when no delegation is declared.

Concretely:

- `/router/health` and `/router/probe` mint the caller's delegated access token from the entry's `delegation:` block, using the same `exchange_from_config` + subject-provider path that `/router/compose` already uses to register delegations. The credential is minted at the **server** layer, where identity and secrets live; the router layer stays credential-agnostic and simply receives a token.
- `BigQueryProvider` gains an optional `probe_as(config, credential=...)`. This follows the existing optional-capability idiom in `health.py` (`getattr(provider, "build_isolated", provider.build)`) rather than changing `StoreProviderPort.probe`, which eleven providers implement.
- Failure to mint is **not** swallowed into an ADC fallback. `NotSignedIn` propagates and becomes the honest verdict - "connect Google to query this source" - which is the one remediation the user can actually act on.

## Consequences

**The schema a user sees is the schema that user is entitled to see.** This is the substantive change, and it is a tightening, not a loosening. An ADC-introspected schema reflects the *operator's* rights: it can expose table and column *names* the delegated caller has no rights to, and those names flow onward into the SQL generator's prompt and potentially into an answer. Introspecting as the caller removes that channel. LAW 2 is better served, not merely unharmed.

**Two callers can now see different schemas for one store id, and that is correct.** It is the same property retrieval already has - two users asking one question get different results - applied one layer earlier. Anything that caches a profile must therefore key on the caller, not on the store id alone.

**The operator-owned-warehouse topology is unaffected.** A store with no `delegation:` block introspects with ADC exactly as before. Azure SQL, Postgres, MySQL, Synapse and Cosmos keep the service-identity probe they have today; this ADR does not migrate them, and their `require_signin` path continues to affect queries only. Extending the rule to them is a later decision, not an implied one.

**A probe now costs a token redemption.** Google refresh tokens do not rotate on redemption and `_CachedExchange` already caches by `(user, resource)`, so a repeated Test-connection does not repeatedly hit Google's token endpoint.

## Alternatives rejected

- **Require operator ADC and say so honestly in the UI.** This is the honest-refusal shape, and honest, but it deletes the bring-your-own-account capability that #193 exists to provide.
- **Fall back to the user credential only when ADC is absent.** Smaller, but it leaves two schema views that can silently disagree, and makes which one you get depend on deployment state rather than on the store's declared identity model. A store that declares delegation has already said whose identity it runs as; introspection should not quietly disagree with that.
- **Change `StoreProviderPort.probe` to take a caller.** Eleven implementers, ten of which do not want it.
