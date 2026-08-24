# ADR 0011 - Multi-tenant sign-in: one app, /organizations, operator-gated affordances

Date: 2026-07-29 · Status: accepted · Builds on ADR 0006 (delegated auth), ADR 0010 (self-serve credentials)

## Context

Steps 1 and 2 of "anyone signs in with their own account and queries their own DB" are live:
per-owner workspaces persist in Postgres (#368), and a signed-in user's credentials travel
through `/secrets` as owner-scoped `secret://` handles (#417/#418).
The remaining gate is Microsoft itself: the sign-in app `dbsearch-sql-login` is
`signInAudience: AzureADMyOrg`, so an account from any other tenant is rejected before
DBSearch ever sees it.

`user_auth.py` already contains a half-mechanism: with the tenant app unconfigured it falls
back to the multi-tenant SharePoint connector app and the `/organizations` authority.
That path is not acceptable as the product path because `data_scopes()` is empty in it -
no `offline_access`, no `https://database.windows.net/user_impersonation` - so query-as-user
(#156, ADR 0006) dies for everyone.

Two exposures block a naive flip:

1. **Operator-credential prefill.** `addNode` prefills any `${ENV}` ref the server can
   resolve (#320), and all the operator's `AZURE_*` connection vars resolve on the hosted
   box. A signed-in stranger would get a node pre-wired to the operator's own database -
   a private copy since #368, but still the operator's data and the operator's env-var
   names. The one previous attempt to gate this (#317, on `!realLoginConfigured()`) broke
   the local rig - which legitimately has BOTH real login and operator-provisioned vars -
   and was reverted.
2. **The document plane is not partitioned.** SharePoint ingest lands in the single shared
   edition index under the deployment-constant tenant id (#389). Retrieval trim is
   ACL-faithful, so foreign users *see* nothing - but letting a foreign admin *ingest*
   into an unpartitioned index would be dishonest multi-tenancy.

## Decision

**1. The tenant app itself goes multi-tenant; the authority follows a flag.**
`dbsearch-sql-login` flips to `AzureADMultipleOrgs` (one `az ad app update`).
A new env `DBSEARCH_MULTI_TENANT=1` makes `_authority()` return `/organizations` while the
app id, secret and full delegated scopes stay those of the tenant app.
Unset, behavior is exactly today's single-tenant flow - the flip is one env var + one
manifest field, and reversing both reverts it.

Rejected: signing everyone in via the SP connector app (loses DB delegation - a #156
regression); a two-app split per audience (double consent flows, no benefit).

**2. Tenant identity needs no new token verification.**
Tokens arrive from the token endpoint over TLS in a confidential-client code exchange, so
the `tid` claim is already trustworthy; it already flows into the session cookie.
Group expansion in a foreign tenant fails closed today - Graph unconsented → `None` →
the user expands to their own oid alone (LAW 2 default-deny) - and that is the correct
multi-tenant behavior, not a gap: a stranger sees exactly the stores ACL'd to their oid,
and `addNode` ACLs new stores to their oid by default (#291).

**3. Operator affordances are gated on an explicit operator list.**
New env `DBSEARCH_OPERATOR_OIDS` (comma-separated oids).
`/config` gains `operator: bool`, computed server-side from the session oid - the list
itself never reaches the client.
The canvas prefills `${ENV}` refs, and receives `env_present` names at all, only when
`operator` is true.
Everyone else starts blank and uses the ADR 0010 credential panel.
The same operator test is enforced at `/router/compose` (#423), not only in the canvas: a
non-operator's manifest may contain no `${ENV}` reference and no local-filesystem source
(`folder`, `csv` with `files:`). A gate the client applies and the API does not is a hint.
Dev-header rigs are untouched (not real-login), which is what dissolves the #317 burn:
the local rig's operator simply lists their own oid.

**4. Secret handles keep the deployment-constant tenant segment.**
Owner-oid scoping already isolates (Entra oids are globally unique GUIDs), and moving to
real tids now would invalidate every existing handle for zero security gain.
Real tids enter the handle when #389's tenant partitioning lands - one migration, not two.

**5. The document plane is gated, not partitioned.**
SharePoint consent/drives/finish endpoints refuse a session whose `tid` is not the home
tenant (`AUTH_TENANT_ID`), with an honest "not yet available for external organizations"
message.
Foreign document queries stay allowed - ACL trim already yields them nothing.
Partitioning the index per tenant is #389's job and is explicitly out of scope here.

## Consequences

**Laws upheld.** LAW 2: foreign identities expand fail-closed to their own oid; the
doc-plane gate compares verified `tid`, never a client claim. LAW 5: workspace keys are
oids (globally unique across tenants); handles stay owner-scoped. LAW 1: the env-var
NAMES themselves stay visible to everyone - they are placeholders baked into the canvas
palette, and pretending otherwise would describe a gate we do not have. Three things are
gated: which of them this server actually has SET (`env_present`), server-side resolution
of a `${ENV}` ref at compose (operator-only, #423 - otherwise naming a variable IS reading
it), and the echo of any resolved value back to the caller (skipped/failure reasons are
sanitized; full text goes to the server log).

**Costs.** Foreign users see an "unverified publisher" consent screen (accepted for
launch; publisher verification is a later, independent step). A foreign tenant's admin
can consent the app for their org - that grants sign-in, nothing more. The operator list
is one more env var to keep correct; an empty list means NO one gets prefill, which fails
safe.

**Out of scope, deliberately.** Tenant-partitioned document index (#389). Publisher
verification. Personal Microsoft accounts. Landing-page sign-in entry (#386 - "Try it
free" already lands on the canvas, where the button lives). Rotation of the ADR 0010
secret key.
