# ADR 0012 - Document-plane tenant partition: make `tenant_id` a mandatory retrieval filter

Date: 2026-07-30 · Status: **accepted** (implemented + prod-proven 2026-07-30, #389) · Builds on ADR 0011 (multi-tenant sign-in), ADR 0006 (delegated auth)

## Context

ADR 0011 turned multi-tenant sign-in on and #429/#431 made a foreign org able to sign in and
connect a database as themselves. One gate remains deliberately closed: `_require_home_tenant`
(`app.py`) 403s SharePoint connect and document upload for any foreign tenant. That 403 is not a
policy - it is a **stand-in for a partition that does not exist**. This ADR removes the need for
the stand-in.

The structured plane is already per-owner: #368 made each user's store catalog a Postgres row
keyed by `owner_oid`, and #431 did the same for connector-connection state. The **document
plane** never got that treatment. SharePoint ingest and `/ingest` land every chunk in one shared
pgvector table, and - the load-bearing fact - retrieval does **not** filter by tenant at all:

```sql
-- adapters/vectordb/pgvector.py :: search()
SELECT ... FROM chunks
WHERE allowed_principals && %s::text[]      -- the ONLY trim (LAW 2)
ORDER BY embedding <=> %s::vector LIMIT %s
```

`tenant_id` is written on every row (the column exists, `NOT NULL`) but appears in **no** SELECT
path. So today, cross-tenant separation on the document plane rests entirely on **ACL values not
colliding**: a chunk is invisible to a stranger only because its `allowed_principals` are Entra
group/object ids the stranger's token does not carry. That is true, and it is not enough:

- It is one accident from a leak. An empty, wildcard, or all-staff ACL on an ingested document
  would be visible to **every** signed-in identity in **every** tenant, because nothing scopes
  the search to a tenant. Within a single customer that is a bug; across tenants it is a breach.
- It rests on a property of the **data** (ACLs happen not to collide), not a property of the
  **schema**. #431's lesson was exactly this: isolation you can state as a key
  (`PRIMARY KEY (owner_oid, tenant_id)`) cannot be forgotten; isolation that depends on values
  can. The document plane should be provably isolated by construction, the way the structured
  plane now is.

This is the last hard blocker on the self-serve story ("Amaris connects their own SharePoint and
queries it"). It is a one-way door: once a real multi-tenant corpus exists, changing the
partition key means a data migration, so the decision is recorded before code.

## Decision

**Partition the document plane by Entra tenant id (`tid`), and make `tenant_id` a mandatory
predicate in every retrieval query - a second independent trim alongside the ACL trim, not a
replacement for it.**

Tenant id, not owner oid, because:

- It matches how a customer reasons about their data ("Amaris's documents"), and it lets
  colleagues in one org share an ingested library - a per-**owner** document index would force
  every Amaris employee to re-ingest the same SharePoint site and would duplicate the corpus N
  times. The structured plane is per-owner because a database *connection* is a personal
  credential; a document *corpus* is an organizational asset. Different grain, on purpose.
- It is the natural isolation boundary for LAW 5 (tenant isolation) and it is the value the
  sign-in already carries: the verified `tid` from the code exchange is in the session
  (ADR 0011 §5), and the ingest connector already authenticates against a specific customer
  tenant.
- Owner is still recorded for **attribution** (who ingested this), just not as the partition
  key. A new nullable `owner_oid` column on the chunk row; it never gates retrieval.

Defense in depth is the reason this is a *second* trim and not a swap. The ACL trim stays exactly
as it is (LAW 2, per-document permissions within a tenant). The tenant trim is added around it:

```sql
WHERE tenant_id = %s                        -- ADR 0012: mandatory tenant partition
  AND allowed_principals && %s::text[]      -- LAW 2: per-document ACL (unchanged)
```

A leak now requires **both** an ACL collision **and** a tenant-id collision - and `tenant_id` is
a single server-supplied verified value per request, not user data, so the second can't happen by
accident the way the first can.

### Where the filter is enforced

In the index query (the SQL above), not in application code after the fetch. A post-fetch filter
is a filter someone can forget to call; a mandatory WHERE clause is enforced by the one path all
retrieval already goes through. `search()` gains a required `tenant_id` parameter - required, not
optional-with-default, so a caller that omits it fails to compile rather than silently querying
across tenants. The demo/self-host single-tenant deployments pass their constant tenant id and are
unaffected.

## Consequences

**Schema.** `tenant_id` already exists on `chunks` and is `NOT NULL`, so no column add for the
partition itself - only a composite index `(tenant_id, ...)` to keep the added predicate cheap,
and a new nullable `owner_oid` for attribution. Follows the repo's first-touch
`CREATE INDEX IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` idiom (pgvector.py already does both).

**Partition values (implementation note, 260730).** The home tenant's partition value IS the
deployment constant (`DBSEARCH_TENANT_ID`), not the raw Entra tid: `resolve_tenant()` (the #184
auth chokepoint, where every transport derives the partition) canonicalizes a session whose
verified `tid` equals `AUTH_TENANT_ID` to the constant, and passes any foreign tid through
as-is. Reason: every chunk ever ingested on a box is stamped with the constant, so home
sessions must land there — the alternative (aligning two env vars + migrating data) recreates
exactly the value-dependent fragility this ADR removes. One rule, one place; the constant is
never GUID-shaped, so a foreign tid cannot collide with it.

**Superseded by ADR 0018 (260807).** This paragraph used to end "sessions without a tid (old
cookies, Google identities per ADR 0011 §5) and non-operator API keys partition to `""`, which
matches no chunk - fail-closed, never the home corpus."
The API-key half is still current.
The no-tid half is not: ADR 0018 gives a VERIFIED real-login session with no Entra tid its own
private `acct:<oid>` partition, which is strictly narrower than per-tenant partitioning and is
what makes the Google and local email/password logins able to ingest and retrieve at all.
Only an anonymous session, a session with neither `tid` nor `oid`, and a `demo:`-namespaced
principal still partition to `""`.

**Backfill.** Every existing prod chunk was written under the deployment-constant home tenant id,
so the existing corpus is already correctly labeled as the home tenant's - no data migration, no
re-embedding. This is the cheap moment to do it, precisely because no foreign corpus exists yet.
(Verify before coding: confirm the distinct `tenant_id` values currently in the prod `chunks`
table are exactly the home tenant, so "backfill is a no-op" is a checked fact, not an assumption.)

**Removing the stand-in.** Once retrieval is tenant-scoped and ingest writes the caller's verified
`tid`, `_require_home_tenant` on the SharePoint/upload surfaces can be lifted: a foreign org
ingesting into *its own* partition is the product working, not a boundary crossed. Lifting it is a
separate, reviewable step **after** the partition is proven - the gate stays until the thing it
stands in for is real.

*Done (260730).* The gate is now `_require_partitioned_tenant`, which keeps exactly the check the
partition genuinely needs: the caller's resolved partition must be **non-empty**. Retrieval-side
`""` is harmless (it matches no chunk), but as an *ingest target* `""` would be a bucket shared by
every tid-less identity on the box, so the write is refused rather than co-mingled. Operator api
keys resolve to the deployment constant and keep working; dev rigs are unaffected. The test that
pinned the old rule (`selftest_doc_plane_tenant_gate.py`) was rewritten to the new contract - the
lift's own regression guard is that a foreign ingest and a home ingest, **ACL'd to the same
group**, are mutually invisible over HTTP.

**Connection state.** Already per-owner and durable (#431). This ADR is only the document plane.

**Verification bar - MET (260730).** The two-tenant proof ran against prod's real pgvector index
using the production code objects: a home-tenant document and a foreign-tenant document
(`797fd32a…`, the #423 throwaway rig) whose ACL was deliberately **wide** and matched the home
caller's principals. Result: the home caller retrieved its own document and **not** the foreign
one - the ACL trim alone would not have separated them, so the tenant predicate is what held.
Each tenant saw only its own; a third tenant saw neither; omitting `tenant_id` raised
`TypeError`; `owner_oid` persisted for both. Probe rows were cleaned back to zero. Prod's
`chunks` table held **0 rows** beforehand, so "backfill is a no-op" is a checked fact. The
HTTP-layer twin of this proof is now a permanent test (see the lift note above).

Original bar, for the record. Not "tests pass" but a
two-tenant proof on prod: ingest a document as the home tenant and one as the throwaway foreign
tenant (`mex3woofgmail`, the rig from #423/#429), then confirm each identity's query returns only
its own tenant's document **and** that a deliberately wide ACL (`all-staff`) on one tenant's
document is still invisible to the other - i.e. the tenant trim holds even when the ACL trim would
not have. That last case is the whole reason for the second filter, so it is the case that must be
shown, not assumed.

## Alternatives considered

- **Partition by `owner_oid` (mirror the structured plane).** Rejected: forces re-ingest per
  colleague and duplicates an organizational corpus per user. Wrong grain for documents.
- **Separate physical table/database per tenant.** Real isolation, but it turns "add a tenant"
  into "provision infrastructure", breaks the single-`chunks`-table retrieval path, and is far
  more than the threat model needs while a mandatory WHERE clause on a verified server-supplied
  value is available. Revisit only if a customer contractually requires physical separation.
- **Keep ACL-only trimming, tighten ingest to forbid wide ACLs.** Rejected: it defends the leak
  by policing *data* (no document may ever have a broad ACL), which is exactly the value-dependent
  guarantee #431 taught us to replace with a structural one. It also would not stop a same-value
  tenant collision from a future connector that mints its own principals.
