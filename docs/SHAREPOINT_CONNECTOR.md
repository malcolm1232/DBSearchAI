# In-app "Add SharePoint" connector (card #148)

A new user connects their Microsoft SharePoint from **inside the DBSearch dashboard** —
Connectors → **+ Add SharePoint** → sign in & grant consent → pick a library → query.
Every answer is permission-trimmed to what each user is allowed to see (LAW 2).

## How it works

DBSearch is registered **once** as a **multi-tenant Entra app**. A customer admin clicks
*Add SharePoint*; we redirect them to Microsoft's **admin-consent** screen for that app in
*their* tenant. On Accept, Microsoft redirects back with their `tenantId`; DBSearch then holds
**app-only** Graph access to that tenant (read-only) and lists their document libraries. Pick
one → it ingests → it's queryable. No per-customer app registration; the only human step is
the one-time consent click Microsoft requires.

```
[UI] + Add SharePoint
   → GET /connectors/sharepoint/consent-url        (JSON; SPA then navigates)
   → Microsoft admin-consent (Global Admin clicks Accept)
   → GET /connectors/sharepoint/callback           (captures tenantId; CSRF-signed state)
   → #/connectors?tenant=…  → library picker  (GET /connectors/sharepoint/drives)
   → POST /connectors/sharepoint/finish            (ingest → register source)
   → Ask (permission-trimmed answers)
```

## One-time operator setup

1. **Register the app** in your (the operator's) Azure tenant:
   ```bash
   ./scripts/register-sp-app.sh
   # or, for a deployed host:
   ./scripts/register-sp-app.sh --redirect https://<your-host>/connectors/sharepoint/callback
   ```
   It prints `SP_CONNECTOR_CLIENT_ID/SECRET/REDIRECT_URI`.
2. **Configure the server** with those three env vars and restart. Until they're set, the
   connector endpoints return **503** and the UI shows a "not configured" note.

## Testing end-to-end (needs a real SharePoint tenant)

The connector is built and unit-tested with mocked Graph, but a **live** run needs a tenant
that actually has SharePoint:

- Get a **Microsoft 365 E5 trial** (30 days, includes SharePoint; requires a credit card).
  The M365 Developer Program free sandbox now requires a Visual Studio subscription and may
  not be available.
- Put a few docs in a library and create **2 Entra groups** (e.g. `deal-team` + everyone) so
  you can demo the permission contrast.
- Run DBSearch on the **azure backend** (`SELFHOST_BACKEND=azure`, real AI Search + AOAI),
  set the `SP_CONNECTOR_*` env, open the dashboard, and click **Add SharePoint**.

## Security notes

- **App-only** read scopes only (`Sites.Read.All`, `Files.Read.All`, `GroupMember.Read.All`,
  `User.Read.All`). Delegated/OBO query-as-user is the SQL-federation path (#102), not this.
- The consent redirect is guarded by an **HMAC-signed, short-TTL `state`** (CSRF).
- Retrieval is unchanged: the connector only **adds a source**; the mandatory `EntraIdentity`
  group-expansion trim still runs at query time (LAW 2).
- **Identity in prod:** the dashboard's `X-DBSearch-User` header is dev-auth; a real
  deployment must derive the caller identity from a verified token (#9) — the trim itself is
  server-side regardless.

## Tests

- `tests/selftest_sp_connect.py` — OAuth flow (consent URL, signed state, callback parse,
  drive listing), endpoints (503/302/400), all mocked.
- `tests/selftest_sp_ingest.py` — `connect_sharepoint` ingest + source registration + LAW-2
  trim on the freshly connected data.
