# Connector Contract (LAW 3)

> Connectors are the actual product. Glean/Microsoft win on connector **breadth and quality**,
> not on "search." Each connector is unglamorous, messy integration work — treat it as a
> first-class component with a strict contract, not a script.

## 1. The contract every connector MUST satisfy

A connector is an **independent module** that implements `ConnectorPort`. It must be:

- **Isolated** — its own auth, config, and failure domain. One connector crashing or getting
  rate-limited **never** corrupts the index or blocks other connectors.
- **Idempotent** — re-processing the same item (same `content_hash`) produces no duplicate and
  no corruption. Keyed by `(tenant_id, source_id, external_id, content_hash)`.
- **Resumable / incremental** — uses the source's **change feed / delta token** (e.g. Microsoft
  Graph delta queries) to sync only what changed; survives a crash by persisting its cursor.
- **Permission-aware** — pulls each item's **ACL** alongside its content (see `PERMISSIONS.md`).
  A connector that can't retrieve ACLs is **not shippable** — content without ACLs is unsafe to index.
- **Rate-limit-respecting** — honors source throttling (429/Retry-After), backs off, never hammers.
- **Least-privilege** — requests the **minimum** source scopes needed (read-only wherever possible).

## 2. The interface (shape, not final code)

```
ConnectorPort:
  authenticate(config) -> session
  list_changes(cursor) -> (items[], next_cursor)      # incremental; full crawl = null cursor
  fetch_content(item)  -> { bytes | text, mime }      # large bodies go to Blob by reference
  fetch_acl(item)      -> principals[]                 # users + groups allowed to read
  to_documents(item)   -> [{ external_id, title, uri, content_ref, acl, source_meta }]
```

Output is normalized `Document` records dropped onto the ingestion queue. Everything downstream
(parse, embed, index) is **source-agnostic** — it only sees `Document`, never connector specifics.

## 3. Build order (matches ROADMAP)

1. **Microsoft Graph** — SharePoint + OneDrive (the wedge; consulting firms live here).
2. Microsoft Graph — Teams + Outlook.
3. Confluence / Jira.
4. Slack.
5. Generic: S3 / Azure Blob / network file shares.

Each new connector is **only** a new `ConnectorPort` adapter — if adding one forces changes to
parse/embed/index/query, that's an architecture smell: **STOP and re-read SKILL.md** (the
pipeline must stay source-agnostic).

## 4. Connector failure rules

- Partial failures are **per-item**, queued to a dead-letter with reason; the sync continues.
- A connector reports health (items synced, errors, lag) as **metadata** (LAW 8) — never content.
- Auth expiry / permission loss surfaces as a tenant-visible health state, not a silent stall.
- Re-running a sync after any failure is always safe (idempotency, LAW 3).
