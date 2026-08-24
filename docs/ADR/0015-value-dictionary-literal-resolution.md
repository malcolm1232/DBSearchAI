# ADR 0015 - In-tenant value dictionary and server-side literal resolution

Date: 2026-08-03 · Status: approved (design review with owner, 2026-08-03) · Cards #479, #462 · Builds on ADR 0007 (pushdown federation), the #476 zero-row honesty check

## Context

Capability E of the real-data pack (#473) - questions whose WHERE literal is the user's wording rather than the stored encoding - scores 0 of 5.
The four diagnostic failures split evenly into two classes:

| item | user said | stored as | class |
| --- | --- | --- | --- |
| E-001 | "debit cards" | `debit_card` | separator / case |
| E-004 | "bed, bath and table" | `bed_bath_table` | separator / case |
| E-002 | "science fiction" | `Sci-Fi` | synonym / abbreviation |
| E-003 | "Dominican Republic" | `D.R.` | synonym / abbreviation |

The generator cannot fix this from inside the prompt.
By LAW 1, no customer value may enter the prompt (the codebase already enforces this twice: `schema_index.py` "never sample values", `structured.py:_schema_fingerprint` "names ONLY"), so the model is structurally blind to how a value is cased, separated, or abbreviated.
The #230 rule (`LOWER(col) = LOWER('value')`) reaches case only - the shallowest quarter of the problem.

The owner's framing (2026-08-03): "there usually is a centralised DB for explaining what each DB and each column means, and each unique var in column means".
That is a semantic layer.
This ADR records which parts of it the LAWs allow, and the design of the part being built now.

## The LAW envelope (decided before the design)

**"Centralised" must mean per-tenant.**
One dictionary across customers is a LAW 1 violation (value sets are customer content, and content never reaches the control plane) and a LAW 5 violation (a shared index is a cross-tenant path).
Everything below lives and runs inside one tenant.

**Value sets are content, so they can never enter a prompt.**
The dictionary is therefore not a prompt input.
It powers a server-side resolver: the model writes the literal exactly as the user said it, and the server maps that wording onto the stored encoding after generation, in-tenant.

**The dictionary is a disclosure surface (LAW 2).**
The distinct values of a column are data.
Candidates are read under the caller's own `AccessContext`, so the dictionary is a per-identity view by construction - a value the caller cannot read is not resolvable, default-deny, with no separate trimming logic to get wrong.
This is the reason the dictionary is read lazily rather than precomputed as a shared superset.

**Authorship decision: auto-profiled, in-tenant only.**
Owner picked this over a human glossary and over a hybrid.
Zero user effort, works on any connected DB immediately; the cost - it can never enter a prompt - is absorbed by the resolver design above.
A human-authored glossary of store/table/column meanings (which IS metadata and CAN enter prompts) remains open as a separate, later card; `table_text` already plumbs an unpopulated `comment` field for it.

**Matching decision: normalize, then embed in-tenant.**
Owner picked this over normalization-only (which reaches only the separator/case half) and over ask-the-user (an extra turn, against the fewest-steps objective).
The embedding leg runs on the in-tenant embedder, so nothing derived from values leaves the tenant.

## Design

Resolution fires only on a proven miss.
The #476 check already identifies the exact moment a WHERE literal is known to be wrong - a single-cell zero/NULL aggregate whose predicate matches no rows on its own - and that is the only trigger.
A query that works is never touched; a resolution that fails leaves exactly today's behaviour (an honest decline).
The change is purely additive.

Three units, one seam each:

### 1. `candidate_values(access, table, column) -> list | None`

Reads `SELECT DISTINCT <column> FROM <table>` under the caller's `AccessContext`, capped at 200 values.
Over the cap, or non-text, returns `None`: the column is an identifier-like column, it has no meaningful dictionary, and profiling it would materialise PII into a second place (the cardinality ceiling is a LAW 1 protection as much as a correctness one).
Results are cached per `(store, table, column, row_policy)` with a bounded LRU, same standing as `memoized_sql_generator`'s cache: derived, reconstructible, LAW 6 clean.

### 2. `resolve_literal(written, candidates, embedder) -> str | None`  (pure)

The ladder, first hit wins:

1. exact match
2. case-fold match
3. normalized match: lowercase, punctuation stripped, `_ - .` collapsed to single spaces, trailing plural `s` tolerated - `"debit cards"` == `debit_card`
4. embedding match over the candidates with the in-tenant embedder - `"Dominican Republic"` -> `D.R.`

The embedding leg accepts only on high absolute similarity AND a clear margin over the runner-up.
Ambiguity returns `None` and the answer declines - resolving to the wrong real value would be a confident falsehood, the exact failure class this work removes.
The leg is disabled entirely for the hashing stub embedder: hash cosine over short strings is noise and would manufacture confident matches.

### 3. Repair and disclosure (in `FederatedSqlStore`)

`_filter_matched_nothing` (the #476 probe) grows from returning a boolean to returning the failing predicate itself; the column name and the written literal are parsed from that predicate (`col = 'literal'` and its `LOWER(col) = 'literal'` variant - anything else does not resolve).
When the literal resolves, rewrite that one predicate, re-execute once, and stamp the substitution into the evidence provenance so the answer discloses it: "read 'Dominican Republic' as D.R.".
An unsaid substitution is its own honesty problem.
One repair per query; a repaired query that still returns an empty aggregate declines as today.

## LAW audit

| LAW | verdict |
| --- | --- |
| 1 residency | Values and their embeddings never leave the tenant and never enter a prompt; the model still writes the user's wording. |
| 2 permission | Candidates read under the caller's own AccessContext; unreadable values are unresolvable. Default-deny. |
| 4 async | Nothing new in the happy path; resolution only fires on a miss and is bounded by the cap. |
| 5 isolation | Cache keyed per store inside the tenant; no cross-tenant dictionary exists anywhere. |
| 6 stateless | The cache is derived and bounded; state of record stays in the source DB. |
| 7 portable | All reads go through the existing `SqlEnginePort`. |
| 8 observable | The repair is stamped into provenance and the audit record (metadata: column, resolved-from, resolved-to). |

## Scope

**In (v1):** the three units, wired at the #476 seam; selftest for each unit; E2E gate = `golden_pack_real` capability E, 0/5 today, must move.
**Out:** human-authored glossary and its canvas UI; precomputed profiling pipeline; cross-store dictionaries; using the dictionary for routing (the #467 hr/home-runs angle - real, separate card); learning synonyms from user confirmations.

**Known limit, stated up front:** resolution triggers only when a predicate matches zero rows.
A literal that is wrong but still matches some rows (a hedged `LIKE '%paid%search%'` catching the wrong subset) never trips the trigger and is out of scope here.

## Verification

1. Unit: `resolve_literal` ladder incl. ambiguity-declines and stub-embedder-off; `candidate_values` cap and ACL threading; repair single-shot.
2. Suite green (159 selftests).
3. E2E: real pack, live ollama rig, before/after on capability E; confidently-wrong count must not rise anywhere else.
4. The answer text for a repaired query must disclose the substitution.

## Amendment 1 (2026-08-04, owner-approved) - what the first measurement changed

The v1 resolver shipped (d781dc2) and the post-resolver runs (491a/b/c) still scored
capability E at 1/5.
The per-item evidence (`real_pack_answers_491.json`) showed the four failures were not
the ladder's cases at all, and two of this ADR's own assumptions needed amending.

**1. The contains-LIKE is now a resolvable shape.**
The real E-002 SQL is `LOWER(genres) LIKE '%science fiction%'` - the v1 parser refused
every LIKE, so the repair never fired.
The `'%x%'` contains shape (and its LOWER variants) now resolves: wrappers are stripped,
the inner text rides the ladder, and the rewrite puts the wildcards back so the repaired
predicate still means "contains".
A pattern whose wildcards carry meaning - a prefix/suffix anchor, an inner `%` or `_` -
still refuses: substituting into those would change the question.

**2. A multi-value column is dictionaried by TOKEN.**
`genres` stores pipe-delimited combos ("Action|Adventure|Sci-Fi"); a combo dictionary can
never contain "Sci-Fi".
`column_values` now splits on `|` and applies MAX_DICTIONARY_VALUES to the tokens; a new
`_SCAN_LIMIT` (2000) bounds the raw read so an identifier-like column is still never
materialised.

**3. The "never enter a prompt" rule is scoped to its actual reason, with ONE sanctioned
exception.**
E-003's class is unreachable without a language model: embeddings provably cannot resolve
abbreviations (measured: nomic-embed-text ranks CURACAO above `D.R.` for "dominican
republic", 0.622 vs 0.595 - without the margin guard this feature would confidently
answer Curacao).
The rule's reason was always residency, not prompts per se - so a final ladder rung may
send the written literal plus an embedder-shortlisted candidate list (<=10) to a model
whose adapter declares `in_tenant = True` (LlamaLlm's self-host endpoint, AzureOpenAILlm's
customer-subscription deployment; base default False, GroqLlm explicitly re-pinned False
because it inherits LlamaLlm).
The gate lives in `dictionary._llm_pick`, absent-means-no; the reply must be a VERBATIM
member of the shortlist or the answer declines; the main NL2SQL prompt remains value-free
unconditionally, and the selftests pinning that are untouched.

Still out of scope, unchanged: the human-authored glossary (#480), precomputed profiling,
cross-store dictionaries, dictionary-assisted routing.
