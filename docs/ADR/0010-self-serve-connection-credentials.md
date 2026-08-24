# ADR 0010 - Self-serve connection credentials: secret references, never manifest values

**Status:** Accepted · **Date:** 2026-07-23 · **Source:** live self-serve connect on dbsearch.ai · **Cards:** #317/#318

## Context

ADR 0009 locked the product model: before login is a demo, after login a real user connects
**their own** databases and queries them as themselves.
The hosted deployment at `dbsearch.ai` is the first place that model meets a stranger, and it
does not work.

Signed in as a real user, dropping an Azure SQL node and pressing Test connection returns:

```
Cannot check 'azure_sql'.
could not prepare check: "manifest references unset env var 'AZURE_SQL_SERVER'"
```

Two mechanisms combine to produce it:

1. `addNode` (`canvas.html`) seeds every connection field whose placeholder *looks like* an env
   reference with that reference as a real value:
   `config[f.k] = envRef ? f.ph : ...`.
   So a new node ships configured as `${AZURE_SQL_SERVER}`, `${AZURE_SQL_PASSWORD}`, and so on.
2. `resolve_env` (`router/provisioning.py`) resolves `${NAME}` from **the server process
   environment**, raising `KeyError` when unset.

That pairing is correct for an **operator-configured** deployment - a self-host box or our dev
rig, where whoever runs the server also sets the variables.
It is meaningless for a **self-serve** user: they cannot set environment variables on our
server, and they should never be asked to.
The prefill is a dev-rig assumption that leaked into the live product, and it works directly
against the stated primary objective of fewest-steps connect-and-query.

The obvious fix is worse than the bug.
Letting the user type their password into the config panel puts a plaintext credential into the
manifest, which is POSTed to `/router/compose` and then rests inside the store catalog.
That is a durable secret living in compute state (LAW 6), and a customer's database credential
is customer data (LAW 1).
It would also be readable back by anything that can read the catalog, which is a far wider
blast radius than the one store it belongs to.

A third option - putting the operator's own `AZURE_SQL_*` values in the server environment so
the existing prefill resolves - was considered and **rejected**.
It does not implement self-serve at all: it silently wires *every* signed-in user's Azure SQL
node to the *operator's* database.

This is hard to reverse (it determines where customer credentials rest) so it gets an ADR
before code, per SKILL.md §1 Reversibility.

## Decision

**1. A live-mode node starts empty.**
Env-reference seeding is an operator affordance, not a user-facing default.
It applies only where an operator configured the deployment (dev rig / self-host with no real
login); a signed-in self-serve user gets blank fields they fill with their own values.

**2. A manifest connection value has exactly three legal forms.**

| Form | Meaning | Who uses it |
|---|---|---|
| literal | the value itself | non-secret fields: host, database, user, tables |
| `${ENV_NAME}` | resolved from server env | operator-configured deployments (unchanged) |
| `secret://<handle>` | resolved through `SecretsPort` | self-serve users (**new**) |

The three are additive.
Every manifest that works today keeps working, which is what makes this reversible.

**3. Secrets never travel in the manifest and are never readable back.**
A dedicated endpoint accepts the plaintext exactly once, writes it through `SecretsPort`, and
returns **only the handle**.
No API path returns a stored secret value.
Read-back is limited to existence and a masked hint (last four characters), which is enough to
render "password is set" in the panel without ever re-serving it.

**4. `SecretsPort` gains write operations.**
It is currently read-only (`get_secret`) and unused by the router.
It gains `put_secret` and `delete_secret`, keeping the port the single seam (ADR 0004).
Azure adapter is Key Vault (already present); the self-host adapter is an encrypted-at-rest
local store so `docker compose up` still needs nothing external.

**5. Handles are tenant- and owner-scoped, and resolution is default-deny.**

```
secret://<tenant_id>/<owner_oid>/<store_id>/<field>
```

Resolution refuses any handle whose tenant or owner does not match the requesting context, so a
handle leaked into another tenant's manifest resolves to nothing rather than to a credential.
This keeps LAW 5 (isolation) and LAW 2 (default-deny) intact at the credential layer, not only
at the retrieval layer.

## Consequences

**Laws upheld.**
LAW 1: the credential stays in the customer's data plane, never in a manifest that crosses to
the control plane.
LAW 5: handle scoping prevents cross-tenant resolution.
LAW 6: the durable secret lives in a managed store, not in catalog state held by compute.
LAW 7: all of it sits behind `SecretsPort` with a per-cloud adapter.
LAW 2 is untouched - this is about connecting a store, not about who may read from it.

**Costs.**
A write path for secrets is new attack surface and needs rate limiting and auth on that
endpoint specifically.
The panel can no longer round-trip a password, so "edit" becomes "replace", which is slightly
worse UX and considerably better security.

**Deliberately out of scope** (real, documented, not solved here):

- **Network reach.** The customer's database must accept connections from the deployment's
  egress IP. That is a deployment concern; on `dbsearch.ai` it means giving the user the box IP
  to allowlist. It does not change where credentials rest.
- **Multi-tenant sign-in.** The Entra app is `AzureADMyOrg` today, so only directory members can
  sign in at all. Genuine public self-serve needs multi-tenant; that is its own ADR.
- **Rotation and expiry.** Handles are stable; rotating the underlying secret is a follow-up.
