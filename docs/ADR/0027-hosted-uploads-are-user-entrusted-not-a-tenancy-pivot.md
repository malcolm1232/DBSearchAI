# ADR 0027 - Hosted uploads are user-entrusted data, not a tenancy pivot

Date: 2026-08-18 · Status: accepted (owner, 2026-08-18) · Cards #706 #775 · Scopes the locked law · Builds on ADR 0012 (partitioning + attribution), ADR 0018 (account identity), ADR 0019 (write paths are never doorways)

## Context

The locked thesis says: never let customer document content leave the customer tenant.
Card #706 asked whether a hosted paid tier - where dbsearch.ai stores and meters user data - contradicts that law and therefore requires an architectural pivot before any billing work (#775) can start.

The question dissolves once the two classes of content the product touches are named:

1. **Connector content** (SharePoint, Azure SQL, Redshift, RDS, S3, Google Drive, BigQuery, ...).
   The data-plane reads happen inside the customer's own tenant with the caller's own delegated credential (OBO, vaulted keys - ADR 0022/0024/0026).
   This is the consulting/enterprise thesis and it is untouched here.
2. **Explicitly uploaded files** (the `upload://` path).
   A user who drags a file onto the canvas has *chosen* to hand those bytes to the service - and production has stored them since the upload path shipped.
   For a self-serve user with no infrastructure, their DBSearch account **is** their tenant.

So a hosted quota-metered tier does not move anyone's content anywhere new.
It meters what the user already entrusts to us, and nothing else.

## Decision

**The law is scoped, not amended: connector content never leaves the customer tenant; explicitly uploaded content is user-entrusted to the hosted service, metered per account, and bounded by the account's tenant partition.**

1. **Connector data-plane stays in-tenant.** No paid tier, quota, or billing feature may ever route connector content through hosted storage.
   A feature that needs connector bytes on our disks is a violation of the law, full stop - it gets redesigned, not excepted.
2. **The account is the consumer's tenant boundary.** Uploaded content lives in the account's canonical partition and never crosses accounts.
   The supersede-by-uri write path is owner-scoped (#791) for exactly this reason: one user's filename can never delete another user's document.
3. **Quota applies only to uploaded content.** Metering counts the bytes an account has entrusted (raw blobs plus their derived index artifacts).
   Connector content is never metered - it is never held.
4. **Hosted upload blobs live in an S3-compatible object store** behind the existing blob port (`ports/base.py`), as a new adapter beside the local and Azure ones.
   The application host keeps no durable upload bytes; index rows and blobs remain separately owned, delete-together (the retention sweep's four blob-key families, `server/retention.py`).
5. **Tier definitions are configuration, not code.** Free and paid tiers - each carrying its own quota and price - ship as one config structure read at boot.
   Changing a quota, a price, or the number of tiers is a config edit and a restart, never a source change.
6. **Self-hosting is the free-forever, fully-in-tenant escape hatch.** The repo is open source; anyone who prefers not to entrust uploads to the hosted service can run the same product inside their own tenant with no quota and no bill.
   This is a feature of the thesis, not a leak in the business model, and product copy may say so plainly.

## LAW 2 (never return an unauthorized result)

Unchanged and unaffected: retrieval remains ACL-trimmed and is never owner-gated (ADR 0012).
Quota enforcement acts only on the WRITE path - an over-quota upload is refused loudly with the remedy named (upgrade or delete); nothing about what a caller may *read* changes.

## Consequences

- #775 (metering, entitlement, billing) is unblocked once this ADR is accepted, and is bounded by rules 1-5.
- The object-store migration (rule 4) precedes any quota enforcement, because metering bytes on the application host's disk would meter a surface we are about to move.
- An account's deletion story already exists (retention sweep); quota accounting must ride the same delete-together families rather than keeping a second ledger of what exists.

## Amendment 1 (2026-08-18, owner-approved) - rule 4 is volume-first; object storage is the endpoint, not the start

The measurement that forced this (card #829, taken on prod the same day the ADR was accepted): the store held 212MB across 4,344 objects, of which 4,340 (`chunk/` + `emb/`) average 4-20KB.
Hetzner Object Storage bills a 64KB minimum per object, so 52MB of real small-object data would bill as ~278MB, and at the account's own `/v1/pricing` a block-storage Volume ($0.0836/GB/mo) is cheaper than Object Storage ($8.71/mo flat) below roughly 104GB.
A Volume also needs zero code: `FilesystemObjectStore` already writes to `OBJECT_STORE_DIR` (`server/edition.py`), so a Volume mounts there and the whole store moves without a source change, while an S3 adapter must be written and must re-prove the retention sweep's delete-together contract.

**Superseded (this amendment).** Rule 4 used to read: "Hosted upload blobs live in an S3-compatible object store behind the existing blob port (`ports/base.py`), as a new adapter beside the local and Azure ones."
It now reads: **hosted upload blobs live on durable storage the application host does not own - independently sizable, surviving a server rebuild - reached in two steps: a mounted Volume at `OBJECT_STORE_DIR` first (zero code, cheaper below ~100GB), and an S3-compatible object store as the endpoint once usage clears ~100GB AND the blob layout has been reworked for the per-object billing floor.**
Rule 4's second sentence (index rows and blobs separately owned, delete-together, the retention sweep's four blob-key families) is unchanged and binds both steps.

**Superseded (this amendment).** The second Consequences bullet used to read: "The object-store migration (rule 4) precedes any quota enforcement, because metering bytes on the application host's disk would meter a surface we are about to move."
Events falsified it, deliberately and after measurement: quota enforcement shipped first (#775, 260818), because at 212MB against 20GB free the surface is ~90x from moving anywhere, and usage is metered from `chunks.doc_bytes` - the INDEX rows - not from the blob store's location, so moving the blobs never invalidates the meter.
What replaces it: **attach the Volume when usage approaches the disk (a trigger, not a date - card #829), and the #831 disk-headroom guard is the backstop that turns "we waited too long" into refused uploads rather than an outage.**

Rules 1-3, 5, 6 and the LAW 2 section are untouched by this amendment.
