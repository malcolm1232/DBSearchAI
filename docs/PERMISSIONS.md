# Permission-Faithful Retrieval (LAW 2)

> **This is the make-or-break of the whole product.** If DBSearch.AI ever shows a user a
> document they couldn't already open in SharePoint, that's a security incident and the deal
> is dead. Permission correctness outranks recall, latency, and cleverness. **Default-deny.**

## 1. The rule

> A query may only ever return, cite, cache, log, or summarize content the requesting user is
> **already authorized to read in the source system**, evaluated as a **mandatory filter** that
> **no feature, prompt, or admin toggle can bypass.**

## 2. How permissions get into the index (sync time)

- Every connector pulls, alongside each document, its **ACL**: the set of **principals**
  (Entra users + groups) allowed to read it. (Microsoft Graph exposes this per drive item /
  site / list.)
- We store an **allowed-principals** list per indexed chunk (denormalized onto the chunk so
  trimming is a single index-level filter, not a join).
- ACLs change. Incremental sync (LAW 3) re-pulls permission changes; **revocations propagate**
  via the same change feed, and removed docs get **tombstoned** (deleted from index), not left
  stale.

## 3. How permissions get enforced (query time)

```
1. Identify the user (their Entra object id) from an authenticated token. Never trust a
   client-supplied identity.
2. Expand user → transitive group memberships via Entra (Graph). Cache with a SHORT TTL
   (e.g. 5 min) per user — bounded staleness, documented.
3. Build the principal set P = { user_oid } ∪ { all group oids }.
4. Apply a MANDATORY index filter: return a chunk ONLY if (chunk.allowed_principals ∩ P) ≠ ∅.
   In Azure AI Search this is a security-trimming filter on the query — it is added by the
   query service, not the caller, and cannot be omitted.
5. (Optional, high-assurance) LATE-BINDING check: for the top-N survivors, re-verify live
   read permission against the source before display. Catches very recent revocations at the
   cost of latency. Make it configurable per tenant.
```

## 4. Non-obvious traps (each has bitten real enterprise-search products)

- **The LLM is a leak path.** The model must only ever receive chunks that already passed
  trimming. **Trim BEFORE retrieval feeds the prompt** — never "retrieve broadly then ask the
  model to be discreet." (Gate: LAW 2.)
- **Caches inherit permissions.** A cached answer/snippet is scoped to the **principal set that
  produced it**, never shared across users. Cache keys include the user/group context.
- **Citations leak metadata.** A citation to a doc the user can't open still leaks its
  existence/title. Citations come only from trimmed results.
- **Logs & telemetry leak content.** Per LAW 1 + LAW 2, query strings, snippets, and titles
  never go to the control plane or external logging. Counts only.
- **Chinese walls / need-to-know.** Consulting firms isolate client engagements. Respect
  source ACLs exactly — do not "helpfully" broaden. Default-deny means unknown = denied.
- **Group expansion gaps.** Nested/transitive groups must be fully expanded, or users see too
  little (annoying) — but erring toward **too little is safe; too much is a breach.**
- **External/guest & anonymous-link shares.** Model these explicitly; treat unknown share
  semantics as **deny** until correctly mapped.

## 5. Definition of done for anything touching search

- [ ] The trimming filter is applied by the **query service**, server-side, unconditionally.
- [ ] No code path returns un-trimmed results "for admins / for debugging / for now."
- [ ] LLM, rerankers, caches, and citations all consume **post-trim** data only.
- [ ] Revocation/deletion is reflected within one incremental-sync cycle (and instantly with
      late-binding enabled).
- [ ] A test exists proving user A cannot retrieve a doc only user B can see — run on every change.
