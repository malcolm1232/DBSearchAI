# ADR 0025 — Ask routes to every composed store

Status: ACCEPTED (design agreed with owner 260813; implemented #689/#851, 2026-08-19 - see the Addendum)

## Context

`/ask` is the first item in the nav and the surface named after the product's core verb.
Today it answers from the document index alone: `ask.js` → `chatStream` → `/chat/stream` → `ConversationService.ask_stream` → `QueryService` (the document plane).
The router — and with it every composed cloud store — is invisible to it by construction (#689, found live: `/canvas` answers "total amount by region" from Azure SQL while `/ask` says "I do not have that information", minutes apart, same account).

The owner has now settled #689's open product question: **Ask is not document-only. Ask routes.**

Two facts shape the design:

1. **`/router/ask` already subsumes documents.**
   Since #255 the document bridge is consulted on every `/router/ask`, so the router path answers BOTH planes.
   The canvas's client-side split (router fetch + `/search` fetch) is historical, not a pattern to copy.
   A naive "ask.js also calls `/router/ask`" renders two competing answers over overlapping evidence every turn.
2. **Ask is conversational; the router is per-workspace.**
   `ConversationService` is constructed once at edition build over the edition-level `QueryService`; it owns turn recording, transcripts, shares (#600/#602/#620) and streaming.
   The router catalog lives in `router_api`'s `_State`/`WorkspacePool`, rebuilt per compose, resolved per caller.
   There is no server-side merged ask path today — that boundary crossing IS this ADR.

## Decision

**`/chat/stream` delegates to the router, server-side.**
The conversation surface keeps everything it owns (turn recording, transcripts, shares, streaming shell); what changes is the service that produces a turn's answer.

- A pool-aware adapter (`RouterBackedQueryService` or equivalent seam on `ConversationService`) resolves the CALLER's workspace scope per call — the same resolution `/router/ask` performs — and produces the routed, multi-store answer with its citations, proofs and outcomes.
- The document plane needs no separate leg: the bridge already rides on every router ask (#255). One ask, one merged answer, no duplication.
- When NO workspace is composed (fresh user, nothing connected), the router path degrades to the document bridge alone — which is today's behaviour, so the empty-state copy stays truthful.
- LAW 2 unchanged: scope resolution is by session identity; `/chat/stream` under a real login already refuses the dev header (#183), and the router's gate #1 (invisible store == nonexistent) applies to conversational asks exactly as to canvas asks.

## Consequences

- **Transcripts must carry router provenance.** A turn answered from Azure SQL persists its citations + `proof.sql` + rerun token the way document turns persist doc citations (#620/#633 did this for docs; the transcript schema gains the sql/record proof shapes). Reopened threads re-render proof pills through the same tail builder.
- **ask.js gains the proof renderers** (🗄 pills, Verify data / re-run, origin lines) — the canvas components are the donor, moved to a shared module rather than duplicated.
- **Streaming**: `/router/ask` is one-shot JSON; first slice streams the synthesized answer text as it arrives from the synthesizer and attaches citations at `done` (exactly the current `chatStream` contract — `done.citations`), so the client protocol does not change shape.
- The `/search` endpoint and the edition `QueryService` remain for the canvas document panel and admin surfaces; nothing is deleted in this ADR.

## Slices (implementation order)

1. **Server**: pool-aware seam + `/chat/stream` delegating a turn's answer to the router scope; `done` payload carries router citations/outcomes. Feature-flagged (`DBSEARCH_ASK_ROUTES=1`) until slice 3 lands.
2. **Transcripts**: persist + replay router proofs on conversation turns.
3. **ask.js**: render proof pills + origins via the shared components; empty-state copy already truthful.
4. **Verification**: the #713 matrix's final Chrome pass asks the SAME per-database questions through `/ask` and expects the SAME tallies the canvas produced — the matrix is the acceptance suite for this ADR.

## Alternatives rejected

- **Client-side dual-fetch in ask.js (canvas-style)**: renders two answers per turn (router already includes docs), duplicates merge logic in a second surface, and leaves transcripts recording only half the evidence a user saw.
- **Copy-only fix ("Ask is document search")**: rejected by the owner — Ask is the product's verb, and a user who connected three databases on the canvas reasonably expects the Ask box to see them.

## Addendum (2026-08-19, implementation) - three things this ADR got wrong or left open

Recorded here rather than in a commit message, because an ADR that describes a design the code
does not follow teaches the next reader to distrust the right document.

### 1. "`/router/ask` already subsumes documents" is true of the CANVAS, not of the server

The Context section above reasons from #255 to the conclusion that the router path answers both
planes, so delegating needs no separate document leg. #255's fix is **client-side**: `canvas.js`
fires `askSharePoint(q)` at `/search` on every ask and paints the result underneath the
router's. Server-side the router only ever sees stores a manifest COMPOSED, and the edition's
uploaded documents are in nobody's manifest.

Delegating naively would therefore have given `/ask` every database and taken away the one
plane it can answer from today - #689's own defect, pointing the other way.

**What was built instead:** the ask seam overlays the caller's document index as a first-class
`IndexedStore` in the ask scope only (`server/ask_router.py`), so ONE router ask answers both
planes through one synthesizer. This is #255's own stated "real fix (b): compose doc sources as
first-class router stores", scoped to this seam - `/router/ask`, the canvas and every composed
manifest are untouched. Gate #1 holds (the node is visible to its owner alone), the store
underneath is the same permission-trimmed `QueryService` core `/search` uses, and the caller's
ADR 0012 partition reaches it verbatim (#439).

### 2. Shared and anonymous threads stay on the document plane

Not addressed above, and it has to be. A conversation share widens the reader's DOCUMENT scope
for one conversation (ADR 0020's conv-scoped grants), and every mechanism around it - the
readable prefix, the per-turn live-grant re-check, `turns_withheld` - is defined over documents.
The router workspace is the caller's own and knows none of it. So `/chat/stream` does not
delegate for a shared thread's continuation. Widening what a share reaches is a product
decision, not a side effect of a flag.

### 3. "Transcripts must carry router provenance" needed a rule for SHARING, and it is consent

The Consequences section says a routed turn persists its proof. It does not say what happens
when that turn is shared, and the invariant the share machinery rests on - *you may read a turn
exactly when you may retrieve everything it drew on* - has no answer for a router proof:
DBSearch owns its own index's ACLs and can mint a grant, but it does not own the customer's
warehouse permissions and cannot grant them.

The first implementation stopped a shared transcript at the first routed turn. **The owner
rejected that** (#850), and the reason generalises: sharing exists to reach people who do NOT
have access - an HR thread handed to an onboarding hire is the canonical case - so any rule
keyed on the READER's access denies precisely the person the feature is for. The same objection
kills "the reader must be able to see the store".

**The rule is the GRANTOR's consent** (#851), expressed in the checklist she already uses for
documents: sources appear beside them, ticked by default, narrow-only. A consented turn travels
with a FROZEN RECORD of its evidence - origin, query, and the rows as they stood - and never a
rerun token. A share passes on evidence, never access.

Asked whether a time-boxed credential could let the recipient re-run live instead: no, and not
because of duration. The connection is the grantor's, so DBSearch would present her credential
to a system with its own access control and its own audit trail and tell it she is asking when
she is not. Time-boxing bounds how long, not who. #850 records the faithful alternative - a
named share-reader principal the customer permissions deliberately - if live access for
outsiders is ever wanted.

### Also worth knowing

- **`ask_stream` did not exist.** `RouterQueryService` grew one, sharing `_ask` with `ask()` so
  routing, the budget, the compound dispatch and the #474 rescue stay one body. Only the FIRST
  synthesis streams: every post-pass can replace the answer wholesale, so what was streamed is a
  draft and `RouterResult.answer` is the record (the #257 contract, now load-bearing).
- **The delegate declines per caller**, measured against gate #1 rather than "is a catalog
  object present": on a dev-header rig every caller shares one workspace (#368), so a colleague
  who connected nothing would otherwise have had their answers re-piped through the router's
  synthesizer.
- **#849** was found by this work: `QueryService.content_titles` returned NO titles when handed
  a `ReadScope`, so a document store lost its whole content routing signal, silently.
