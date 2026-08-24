# ADR 0026 - RDS IAM database auth over the vaulted AWS keys

Date: 2026-08-18 · Status: accepted · Card #814 · Extends ADR 0024 (aws_keys) · Builds on ADR 0022 (delegated introspection), ADR 0010 (self-serve credentials)

## Context

The `rds_postgres` / `rds_mysql` kinds (#672) shipped as truthful aliases of the Postgres/MySQL engines - and inherited a password-shaped auth model that the panel then could not honestly present.
The wave-2 prod audit (#780) found the dead end the owner had already hit live on 260813: a palette-added RDS node fails Test connection with `postgres config missing [password]` - a password the panel does not collect, named under a kind the store is not.
`RdsPostgresProvider`'s own docstring named the honest alternative and left it unwired: `rds:generate-db-auth-token`, a 15-minute IAM auth token used AS the password, composable with the vaulted `aws_keys` credential.
The owner ruled (260818): option 2 - authenticate the RDS kinds through the caller's vaulted AWS keys, the same delegation rail Redshift and S3 already ride.

## Decision

**An RDS store authenticates with an IAM auth token minted from the caller's own vaulted AWS keys; nobody types a database password on the canvas.**

1. **The RDS kinds join the always-delegated AWS kinds on the canvas** (`_AWS_KINDS` / `_ALWAYS_DELEGATED`, the #809 shape): a palette-added `rds_postgres` / `rds_mysql` entry always carries `delegation: {kind: aws_keys, resource: rds}`.
   `resource` remains only the STS session cache key (ADR 0024): one session per caller serves every AWS service.
2. **New engines `RdsPostgresEngine` / `RdsMySqlEngine`** (subclasses of the base engines).
   `from_config` requires `host` / `database` / `user` - NOT `password` - and reports its OWN kind in the error (the #814 leak fix).
   The delegated connect parses the STS triple, calls `rds.generate_db_auth_token(DBHostname, Port, DBUsername=config user)` with those session credentials (region from `config.region`, else parsed from the RDS hostname), and opens the connection with the token as the password.
   The token mint is a local SigV4 presign - no network call, no added latency.
   TLS as the base engines already enforce it (postgres `sslmode=require` default; mysql always-on TLS context) - IAM auth requires it.
   The Entra `aad_user` path is structurally unreachable here: the RDS engines override `user_connect` entirely, so an Entra token can never be redeemed against an AWS database (the docstring's warning, kept true).
3. **Delegated introspection (ADR 0022)**: the RDS engines gain `introspect_as`, the RDS providers gain `probe_as` / `build_as`, mirroring `RedshiftProvider` - a delegated store is probed, health-checked and queried as the caller, schema cache keyed per credential.
4. **A typed password stays legal, exactly where it already was** (ADR 0010 form 2): a hand-written manifest with `password` composes on the service path unchanged - the self-host topology where the operator IS the credential owner.
   The panel simply stops collecting one.
5. **No silent fallback on a failed mint.** A caller with no AWS link composing a password-less RDS store gets an honest skip whose reason names the remedy (connect Amazon in the account menu) - there is no password to silently fall back to, and the ambient-credential fallback the S3 `build_unavailable` note warns about cannot arise (an RDS connection has no ambient identity to borrow; it fails closed at connect).

## LAW 2

Authentication is per-caller end to end: each caller's OWN vaulted keys are redeemed (ADR 0024), and AWS's `rds-db:connect` IAM permission gates each caller against the configured DB user - an unauthorized caller is refused BY AWS, not by us.
The database-side identity is the configured DB user for every authorized caller - the same service-credential model every ADR 0010 self-serve store already has (Azure SQL with a typed password included), stated rather than hidden: in-database row security keyed on the DB principal does not distinguish DBSearch callers on this rail.
Store-level authorization (acl, LAW 2 default-deny) remains DBSearch-side, unchanged.

## Consequences

- The RDS panels drop `password`; `user` stays (the token is minted FOR a DB user, which must be granted `rds_iam` on postgres / `AWSAuthenticationPlugin` on mysql - the panel placeholder says so).
- `secret_fields.py` keeps `password` listed for the RDS kinds: a hand-written manifest may still carry one and it must stay guarded as a secret.
- The enterprise successor is unchanged from ADR 0024: `aws_sts` federation with no stored secret, its own card when a customer needs it.
