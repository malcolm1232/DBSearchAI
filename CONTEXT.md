# CONTEXT.md — DBSearch.AI product north-star (read at session start)

**Purpose of this file:** keep every session pointed at the same target. `SKILL.md` holds
the LAWs and architecture; **this file holds the product intent** — *what we are building
and why*, and the line between the **product** and the **demo scaffolding** we've been
using to test mechanics. When a session finishes standing something up, ask: *does this
move us toward the product, or does it deepen a demo shortcut?*

> **The one-line shift (2026-07-08):** we are done proving mechanics with demos. From here,
> work must move DBSearch toward the **real product**: a customer signs into **their own
> tenant** and queries **their own data, as themselves**. Demo shortcuts that fake this are
> tech debt to be replaced, not foundations to build on.

---

## 1. The product — "John connects his data" (the tenant story)

DBSearch.AI is **permission-faithful enterprise search / RAG** for a firm's own knowledge
(first customer: a consulting / professional-services firm). The product model:

1. **John (the customer) does NOT upload data to us.** He **connects his own sources**
   (SharePoint, OneDrive, Azure SQL, Postgres, …) that live **in his own tenant**.
2. **John signs in with his own Microsoft/Entra identity**; a Global Admin grants consent.
   DBSearch runs **inside his cloud** and reads his data **in place** — nothing is copied
   out, nothing phones home. **There is no central DBSearch database.**
3. **Every query runs on-behalf-of (OBO) the person asking.** The **source itself** enforces
   its permissions against that real identity — Alice sees only what Alice's ACLs allow;
   Bob is denied. DBSearch never holds his credentials and never sees data a user can't see.

**"Tenant"** = the identity + data boundary (John's Entra directory: his users, groups,
SharePoint, the app registration). The pitch: *"data residency isn't a policy we promise —
it's a gate in the code."* Everything touching customer data runs in **their** tenant; only
a small whitelist of telemetry may ever leave, enforced by a test that tries to smuggle a
document snippet out and asserts it is blocked. Air-gap mode is a config flag.

**Moat** (from SKILL.md §0): (1) permission-faithful retrieval, (2) connector breadth &
quality. Protect these above all. GTM: **open-core self-host** (`docker compose up`, free)
as hero, then **Managed Azure** and **Enterprise** tiers.

## 2. Product vs demo — what's real vs a shortcut

We have been testing **mechanics** (routing, provenance, re-run, connectors) with shortcuts
that **violate the product story**. Those shortcuts are the work ahead, not the product.

| Concern | ❌ Demo shortcut (today) | ✅ Product (target) |
|---|---|---|
| Identity | Canvas auto-selects `alice` via `X-DBSearch-User` **dev header** — no login | John **signs in** (Entra/Microsoft), like the SharePoint flow (#171). No auto-identity. |
| DB auth | Server holds a **shared service credential** (`dbsadmin` + password in env) — everyone queries as one admin | **OBO / query-as-user**: each query runs with the signed-in user's delegated token (#156, #131) |
| Isolation | One shared test DB in *our* subscription | Runs in **the customer's tenant**, against **their** data, per-tenant isolated (LAW 5) |
| Access | Anonymous request without a real login can reach data | No login → **no data** (401 / deny). Source enforces ACLs against the real identity (LAW 2) |
| Sign-in for sources | Azure SQL wired with static env creds | Connect + consent per source, like SharePoint's Entra OAuth (#148/#171) |

**Rule:** a feature is **product-conformant** only when John's data access requires *his*
sign-in and runs as *his* identity. Until then it is a demo, and `/e2edbs` must say so.

## 3. What "product-conformant" means (the invariants /e2edbs enforces)

`/e2edbs` verifies these product invariants (see `~/.claude/skills/e2edbs/`). It reports a
**PRODUCT CONFORMANCE** section: ✓ where the product story holds, ⚠ **DEMO-GAP** where a
shortcut still stands (with the tracking card), so a green mechanics run never hides that
the product story isn't met yet.

1. **No anonymous data access** — a request with no identity is denied (401), never served.
2. **Identity scopes data** — two identities get different results per their ACLs (Alice vs
   Bob); a user never learns a store exists that they can't see (gate #1).
3. **Query-as-user, not shared credential** — data-plane queries run under the caller's
   delegated identity, not a shared service account. *(Azure SQL: mechanism landed (#156);
   live proof = `/e2edbs --product --live-entra`.)*
4. **Sign-in required for a real tenant source** — connecting a source and querying it
   requires the user's own auth/consent, not baked-in env creds. *(SharePoint: #171/#148;
   Azure SQL: mechanism landed (#156); live proof = `/e2edbs --product --live-entra`.)*
5. **Legible, grounded provenance** — every answer shows resolvable sources with a
   pinpointable origin and the data backing each claim (#165/#175/#176 — DONE).

## 3.5 How to VERIFY — a claim is not "done" until the LIVE app says so (owner directive, 2026-08-17, said twice)

### DONE MEANS PROD. Local proves the MECHANISM; only prod proves the WIRING.

A fix can work perfectly on a laptop and be dead on prod, because what differs is not the logic:
prod serves **rsynced** static assets through **Cloudflare**, runs a **real Entra login**, and has a
**different store catalogue**. None of that is exercised by a green suite, a jsdom probe, or even a
real browser driven against localhost. So the ladder is three steps and must not be collapsed:

1. **Local** (suite, Playwright, `scripts/mutate_guards.py`) — reproduce the defect, measure the
   mechanism, prove the guard goes red. Iterate here; it is cheap. **This never closes a card.**
2. **Push and deploy**, then **byte-verify what is actually SERVED** (see the traps below).
3. **PROD, on the live site, as a real signed-in identity** — re-run every check that mattered,
   including the scroll census. **Only this closes a card.**

Anything established only at step 1 is reported as *"mechanism proved locally, prod pass owed"*,
never as done, and the card writeup names which step the evidence came from. A card whose code is
committed but whose prod pass has not run stays In Progress. This is the mechanism that stops
"shipped" and "verified" drifting apart, which is the entire shape of the 260817 reckoning.

### DRIVE THE UI. Not the endpoint, not the container.

The prod pass means **a real signed-in identity clicking the real surface**. An API call and a
`docker exec` reach the same code and prove less: they skip the wiring between the surface and the
endpoint, which is where "works locally, dead on prod" actually lives. On 2026-08-18 the UI drive
found two things nothing else had — a duplicated store name in the disclosure line (#799) and a
snippet cut mid-word (#748) — both below the fold, neither visible to any test.

When the UI genuinely cannot reach the condition (it needed a **non-operator** caller, and the
owner is an operator), do the container-level drive, **say so explicitly, and name what was NOT
composed**. Container evidence is real evidence. It is not a UI pass and must never be written up
as one.

### THE SCROLL CENSUS IS A MEASUREMENT YOU RUN, NOT AN IMPRESSION YOU FORM

`#qresult` is a scroll container. It has now measured **31%**, **25%**, **23%** and **42%** of its
own content on four separate live compound asks — so this is its NORMAL state, not an edge case.
**A screenshot of the top is a screenshot of a quarter of the answer**, and every time so far the
thing that mattered was below the fold: the mid-word snippet cut (#748) and the duplicated store
name (#799) are both down there. Run this BEFORE any screenshot and before any judgement, every
time:

```js
const out = document.getElementById("qresult");
out.querySelectorAll("details").forEach(d => d.open = true);   // open every disclosure FIRST
[...document.querySelectorAll("*")]
  .filter(e => e.scrollHeight > e.clientHeight + 2 && /auto|scroll/.test(getComputedStyle(e).overflowY))
  .map(e => ({ sel: e.id ? "#"+e.id : "."+String(e.className).split(" ")[0],
               visible: e.clientHeight, total: e.scrollHeight,
               pct: Math.round(100*e.clientHeight/e.scrollHeight)+"%" }));
```

If any `pct` is under 100 **you have not seen the answer**. Then step `scrollTop` in bands of about
half `clientHeight` and read each band — and prefer reading `innerText`/`innerHTML` over
screenshotting, since the DOM carries the full content regardless of scroll and costs a fraction as
much. Confirm the panel ENDS cleanly rather than stopping mid-element.

Two related reflexes: **disable CSS transitions before reading any animated property** (a mid-flight
transition in a throttled tab once read *inverted*), and prefer the **geometry** over the property
string — a marker going 6×18 → 18×6 is a rotation; `transform: rotate(1turn)` is not.

**Traps on the way to prod, all of which bit on 2026-08-18 and are cheap to avoid:**

- **`grep`-ing prod for the OLD string proves nothing** if a docstring quotes it. `grep -c "value or
  default_partition"` returned 1 on a correctly-fixed box because the new comment explains the old
  bug. Assert the **executable line**, or better, `docker exec` and call the function.
- **Disk is not what users get.** Check the asset over **HTTPS**, not over ssh: `curl -D-
  https://dbsearch.ai/static/js/...`. Confirm `cache-control` / `cf-cache-status` mean no stale CDN
  copy, and `shasum` it against the file your guards actually ran on.
- **The owner is an OPERATOR** (`is_operator` → True). Any check whose discriminating condition is
  "a non-operator caller" cannot be run from the owner's own session or key — it will pass for the
  wrong reason. Work out what the OLD code would return for your input before believing a green.
- **`#qresult` is a scroll container.** See the census above — this is the trap that has now bitten
  four times, and every time the finding was below the fold.
- **A reconnected Chrome lands SIGNED OUT, in demo mode.** Check `/auth/me` (`idp: entra`,
  `signed_in: true`) as the FIRST act of every browser pass and after every extension reconnect —
  demo stores cannot discriminate a fix from a break, and a full pass against demo mode is worth
  nothing. It happened again on 260817; the check is one fetch.
- **Container logs do not survive recreation.** `docker compose up -d api` makes a NEW container;
  the old one's logs are gone. If a prod incident matters, grep the api logs BEFORE the next deploy
  — this is exactly how #727's original 260813 evidence was lost.
- **A prod probe touching `user_auth.VAULT` must `import dbsearch.server.app` first.** The module
  singleton is created unbound; without the app import, `bind_store` never ran in your probe
  process and every identity answers a false `NotSignedIn` (a near-miss false root cause, 260817).
- **CI is its own environment, and "verified locally" can fail to generalise even for TOOLING.**
  `npm ci --prefix site` ran here with exit 0, producing the exact file, and failed on the runner:
  npm 11 tolerates an out-of-sync lockfile that npm 10 refuses. Push and watch the run; do not
  reason about what CI will do.
- **A static-asset change needs a REBUILD, not just an rsync.** The prod image COPYs `src/` — there
  is no bind mount — so editing `canvas.js`/`canvas.css` and rsyncing is not enough: `rsync -az
  --delete --exclude '__pycache__' src/ dbsprod:/opt/dbsearch/src/` **then** `docker compose -f
  docker-compose.yml -f docker-compose.prod.yml build api && … up -d api`. Then byte-verify over
  HTTPS (above). Prod Postgres is `psql -U postgres -d dbsearch` (no `dbsearch` role); `/healthz` is
  404, so use `/canvas` (200) for liveness.

### A green test suite is still NOT verification

It counts **files**, not tests — one file whose every assertion skipped scores a whole pass — so
always read the unit in the sentence: `264/264 files passed`. Since **#792** a missing node/jsdom
**fails** rather than skipping (it silently no-op'd 66 DOM checks on every clean clone and in CI
until then), and `DBSEARCH_ALLOW_DOM_SKIP=1` is the only way back to a skip — which the runner then
counts and prints as `[PARTIAL]`. jsdom lives in `tests/package.json`; run `npm ci --prefix tests`.
Unit tests, the jsdom probe and raw API calls remain **supporting evidence only**; label them as
such and never let them stand in for verification.

**`scripts/mutate_guards.py` is how "the guards hold" becomes a command rather than a sentence.** It
breaks the product on purpose and checks the owning guard goes red, starting every run with an
unmutated CONTROL so a mutation cannot read as caught for the wrong reason. Add an entry in the same
commit as the fix.

### HOW TO WRITE A GUARD THAT CAN ACTUALLY FAIL

Four rules, earned on 2026-08-18. **Three of them caught work I had already convinced myself was
finished**, and a green suite passed all three.

- **ONE MUTATION PER CLAUSE, NOT PER FIX.** If a fix has two parts, build the fixture so that
  removing *either part alone* goes red. #793 capped the list marker at two digits AND required a
  run of two items; every case was rescued by both, so each was individually removable with nothing
  red. Fixed by adding two adjacent years (only the cap saves them) and a lone two-digit line (only
  the run rule saves it). A fixture rescued by both clauses at once proves **neither** — it is one
  guard wearing the costume of two.
- **ASSERT THE DOM, NOT ITS TEXT.** An injected `<img onerror=…>` contributes ZERO characters to
  `textContent`, so a guard reading text gets **greener as the surface gets less safe**. Measured:
  with `esc()` removed the outcome row's `textContent` is byte-identical. Assert on `innerHTML`,
  on injected elements, on event-handler attributes.
- **A FIXTURE THAT CANNOT REACH THE SINK PROVES NOTHING.** The first #786 fixture used a `sql`
  footnote, which renders buttons and touches no attribute at all; it had to be `document` WITH a
  uri to reach the `href=` sink. Check that the code path you are guarding actually RAN.
- **A GUARD CAN BE KILLED BY AN IMPROVEMENT.** #761's dedup guard was load-bearing when written and
  went dead six commits later when #753 rebuilt the label as one array with a uniqueness filter —
  the duplicate stopped being *composed*, so no input could turn it red. Product code shielding a
  guard from its own defect. When you improve a surface, ask which guards just became unfalsifiable.

And the meta-rule for this section: **keep it true.** §3.5 spent a morning telling readers the DOM
guards silently no-op, hours after #792 fixed exactly that. A rule describing a defect that no
longer exists teaches the next reader to distrust the wrong thing.

**Every "verified / confirmed / works / done" claim about a rendered surface or a live endpoint MUST
be established by driving the LIVE product.** Pick the tool by need, not by habit — the three are not
interchangeable on cost:

- **Playwright** (`/pw`, `mcp__plugin_playwright_playwright__*`) — **the cheapest, and the default
  for scripted / repeatable measurement.** `browser_evaluate` returns JSON and `browser_snapshot`
  returns a text tree; take a screenshot only for a genuine pixel judgment. Most verification work
  (computed styles, a `fetch` recorder, DOM reads, scroll geometry) is measurement, so it belongs
  here. **What it can and cannot reach:** a LOCAL rig under real-login semantics is available today
  — `DBSEARCH_LOCAL_AUTH=1` with `DBSEARCH_SESSION_KEY` enables `/auth/signup` and `/auth/login`,
  which mint a **real session cookie**, so `real_login_enabled()` is true and `resolve_tenant` takes
  the verified-session branch. That is step 1, and it is plenty for measurement. It can **never** be
  step 3: prod is Entra, and there is no minted-cookie path for it (the `/pw` skill mints one for
  QuantifyMe only). Building that path is what would let Playwright do the prod pass too — #797.
- **Claude in Chrome** — use when you need (a) the **already-authenticated real session** (as today,
  signed in as the real Entra user — until the Playwright auth path exists this is the only way to
  drive `/canvas` as a real identity), or (b) genuine **pixel/visual judgment**. Its `computer`
  screenshots each cost an image (~1k+ tokens), so measure with JS and screenshot sparingly.
- **`/e2edbs` L4** — the project harness's **real-browser** level (L1 is a fast API drive; L4 drives
  `/canvas` via Claude-in-Chrome and tallies the rendered answer). Prefer it for a full-stack
  product-conformance claim, since it already knows the identity/compose/ask/tally flow.

When something genuinely cannot be reached in a browser (a cost-blocked cloud resource, a
`::-webkit-` pseudo-element jsdom cannot compute), **say so explicitly and name what was measured
instead** — do not let the weaker evidence pass as the claim.

**The connections trap — check on every browser pass** (the scroll trap is the SCROLL CENSUS
above). A store's connect/health outcome is a **UI-honesty** property: verify it by adding the
source through the real **Add a source** panel and pressing **Compose up**, *in the browser*, not by
hitting the API. A store can fail honestly, report nothing on one path (Compose up) yet report fully
on another (Test connection) — that disagreement is only visible on the page (#779/#781). **A prod
UI drive that adds or removes a test node MUST restore the workspace afterward**: remove the node →
Compose up → confirm `/router/catalog` is back to the original stores, so the owner's canvas is left
exactly as it was found.

The standing goal is to make this regime a **runnable asset** (skill + checklist) rather than steps
re-derived each session — tracked as **#795**.

## 4. Snapshot as of 2026-07-08 & the path

> **THIS SECTION IS A DATED SNAPSHOT, NOT CURRENT STATE.** It was headed "Current state"
> until 2026-08-20, which is misleading in a file §0 of every handover says to read FIRST -
> a heading that claims currency outranks a date in parentheses. **The board is current
> state; this is history.** Resolve any card named below with `card.py show <n>` before
> acting on it. Audited 2026-08-20 (#869) and corrected inline.

- **DONE (product-real):** provenance layer + legibility (#165/#175/#176); SharePoint live
  Entra sign-in code (#171 — **done 2026-07-30**; the "login click-test pending" note that
  stood here was stale); connector rail.
- **DEMO-GAP as it stood on 2026-07-08** (statuses re-checked 2026-08-20):
  - **#156** — prove Entra **OBO query-as-user** live for Azure SQL (the big one; makes the
    demo honest). *Still backlog.* **#131** — delegated-auth completion (pyodbc
    query-as-user). *Still blocked.*
  - ~~**#171** — finish the per-user Microsoft sign-in loop on the canvas (no auto-`alice`).~~
    **Done 2026-07-30.**
  - **#177** — proof/Sources UX must read as product, not internal jargon. *Done.*
  - **#172** — off-topic queries must abstain, not return an ungrounded SQL dump. *Still open.*
- **North for now:** replace the auto-identity + shared-credential shortcuts with **real
  per-user tenant sign-in + OBO**, so a session can demo John end-to-end **as the product**.

## 5. Where to read next

| To… | Read |
|---|---|
| Check the LAWs before building | `SKILL.md` §1–2 (Architecture-Correctness Gate + LAWs) |
| **Verify ANY claim — "done" means PROD, after push + deploy** | **§3.5 above** — local proves the mechanism, only prod proves the wiring; Claude in Chrome / `/e2edbs` L4 / Playwright; byte-verify the SERVED asset, run the scroll census |
| Verify a change end-to-end as the product | `~/.claude/skills/e2edbs/SKILL.md` (run `/e2edbs`) |
| **Deploy to prod** (rebuild, not just rsync) | §3.5 "Traps on the way to prod" + `docs/DEPLOY_CONVERSATION_SHARING.md` for the full sequence |
| Permissions / who-can-see-what | `docs/PERMISSIONS.md` (LAW 2) |
| Delegated auth / OBO design | `docs/ADR/` (ADR 0006 delegated auth) |
