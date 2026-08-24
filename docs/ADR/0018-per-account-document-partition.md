# ADR 0018 - Per-account document partition for non-Entra logins

Date: 2026-08-07 · Status: accepted · Card #573 · Builds on ADR 0011 (multi-tenant sign-in), ADR 0012 (document-plane tenant partition), ADR 0013 (identity model)

## Context

ADR 0011 s5 made a session without a verified Entra `tid` partition to `""`, and ADR 0012 kept that rule when the document plane became tenant-scoped.
At the time the only no-tid identity was an unlinked Google session, and the document plane was Entra-only, so refusing it was the safe, narrow choice.

The product now promises upload-and-persist to non-Microsoft accounts: Google today, and a removable local email/password login next.
`""` matches no chunk, so a no-tid session can sign in but can never ingest or retrieve anything.
That makes the alternate login decorative rather than usable.

## Decision

A verified real-login session whose `tid` is empty partitions to `acct:<oid>` instead of `""`.
The `oid` here is the session's account key (ADR 0013), so the partition is private to that one account.

The other branches of `resolve_tenant` are unchanged:

- Home tid: still canonicalizes to the deployment constant.
- Foreign tid: still passes through as-is.
- Anonymous, or a session with neither `tid` nor `oid`: still `""`, fail-closed.
- A `demo:`-namespaced principal never gets an `acct:` partition, even with no tid - it stays `""`, matching the existing demo-scope rule.

`acct:` is reserved as a prefix and is never GUID-shaped and never equal to any deployment constant, so it cannot collide with a home or foreign tenant partition - the same non-collision property ADR 0012 relies on for the deployment constant.

## Consequences

LAW 5 (tenant isolation) is tightened, not loosened: per-account partitioning is strictly narrower than per-tenant partitioning, since an account is always a subset of one tenant, never the reverse.
Two non-Entra accounts on the same box now get two distinct, mutually invisible partitions instead of both being locked out of everything.

`_require_home_tenant` and `_require_partitioned_tenant` (`app.py`) are untouched.
SharePoint ingest still requires a real Entra tenant consent flow, which a non-Entra account cannot obtain, so a non-Entra account still cannot SharePoint-ingest - this ADR only makes upload and retrieval work for the partition that account already gets.

Card #576 (Task 6, the account-deletion sweep) can reconstruct a non-Entra account's document partition from its account id alone, with no lookup table: `acct:<account_id>` is a pure function of the id.

This flips an existing pinned contract, and that is stated here rather than left implicit.
Before this change, `tests/selftest_doc_plane_tenant_gate.py::test_missing_tid_is_still_refused` pinned "a session with no tid cannot ingest, 403" as the correct behavior - and it was correct, given that a no-tid session's only possible ingest target was the shared `""` bucket.
That test has been rewritten to `test_missing_tid_gets_own_account_partition`: a session with no Entra tid could not ingest before this ADR, and can now, into its own `acct:<oid>` partition.
The rewritten test also carries the isolation proof (two no-tid identities, same wide ACL, neither can read the other's document) that makes the flip safe rather than merely convenient.

Verified by `tests/selftest_573_acct_partition.py`: a no-tid session's `resolve_tenant` call returns `acct:<oid>`; two different no-tid accounts get two different partitions; home-tid and foreign-tid sessions are unchanged; anonymous still fails closed; and an end-to-end pass through `/admin/upload` and `/search` shows the uploader can read its own document back, a second non-Entra account cannot, and the home-tenant corpus does not see it either.
