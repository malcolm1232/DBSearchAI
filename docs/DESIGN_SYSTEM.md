# DBSearch.AI design system

The canonical UI reference, covering all three front-end surfaces.
Written 2026-07-29, at the end of the pass that replaced the old dark "Vault" theme.

Supersedes `site/design-system/MASTER.md`, which documents the dark palette and is no longer true of anything.

---

## 1. Where the UI lives

Two codebases, one visual system.
Knowing which file owns what is most of the work.

| Surface | URL | Code | Styling |
|---|---|---|---|
| Marketing site | `/`, `/product`, `/pricing`, `/security`, `/self-host`, `/demo`, `/privacy`, `/terms` | `site/` (Next.js, static export) | `site/app/globals.css` (Tailwind v4 `@theme inline`) |
| App shell | `/app`, `/ask`, `/draft`, `/admin`, `/developer`, `/canvas` | `src/dbsearch/server/static/` | `css/tokens.css` + `css/app.css` |

The app shell is ONE document (`index.html`) with one surface per route, mounted by
`js/router.js` from `js/surfaces/*.js`.
Moving between any two of them is a `pushState` and a re-render, never a page load.

**Connectors used to be the exception, and it was a defect** (#643).
`/canvas` served its own document, `canvas.html`, with its own inline stylesheet, its own
topbar and its own identity chip - so walking from Ask to Connectors was a 56KB document
load, the topbar changed completely, and the rail changed width because the canvas redefined
seven of `tokens.css`'s variables.
It is now `js/surfaces/canvas.js` + `css/canvas.css`, every selector scoped under
`.canvas-surface` because that file and `app.css` share 17 class names from their years apart.

**A new surface adds a file under `js/surfaces/`, a route in `router.js`, and a path in
BOTH `SHELL_PATHS` lists. It never adds a document.**

Shared by every surface:

- `css/rail.css` + `js/ui/rail.js` - the navigation. One definition.
- `js/ui/account.js` - identity and per-provider connection state. One definition.
- `js/ui/errors.js` - typed error rendering.
- `fonts/instrument-serif-latin.woff2` - the display face.

The marketing site is served by the FastAPI box itself, from `site/out/`, mounted last in `app.py`.
It is not hosted by a third party, deliberately: see rule 9.

---

## 2. The reference

The visual direction came from a personal-portfolio design (Behance gallery 157670567, "GEOSM Creative").
It was **translated, not copied**.

That distinction matters, and it is the first thing to preserve.
The reference is a portfolio: warm, artsy, soft.
DBSearch sells zero-egress search to firms that cannot afford a leak.
Copy the reference literally and the site reads "design agency", not "infrastructure you trust with your tenant".

What we took: the paper canvas, the whitespace, the serif display, hairline rules, one inverted band.
What we kept: the technical proof (cited answers, permission trimming, the self-host command), rendered light and refined rather than dark and dense.

---

## 3. Colour

### The palette

| Role | Light | Dark | Notes |
|---|---|---|---|
| Page background | `#FAF9F7` | `#141416` | Warm paper. Deliberately not pure white, which glares. |
| Raised surface | `#FFFFFF` | `#1C1C20` | Cards, the answer card. |
| Quiet fill | `#F2F0EC` | `#232328` | Code chips, nested panels. |
| Ink (text) | `#16161A` | `#F5F4F1` | Near-black. Pure `#000` is harsh on paper. |
| Muted text | `#6B6B73` | `#9A9AA2` | Body copy and micro-labels. |
| Hairline | `#E4E2DD` | `#2E2E33` | Rules and dividers. |
| Signal green | `#15803D` | `#4ADE80` | Citations, verified, live dots. |
| Bright green | `#22C55E` | - | **Decoration only.** 2.6:1 on paper, never text. |
| Muted on ink | `#9A9AA2` | - | Text on the inverted band, where muted would be dark on dark. |
| Destructive | `#B91C1C` | `#F87171` | Errors. |

The dark theme is **warm** dark, not blue-black, so switching themes does not feel like switching products.

### Contrast, measured

Against paper `#FAF9F7`: ink 17.15:1, muted 5.02:1, green 4.77:1, destructive 6.15:1.
On the ink band `#16161A`: paper text 17.15:1, muted-on-ink 6.46:1.

These are **asserted by a test**, not eyeballed.
`site/tests/design-tokens.test.ts` parses `globals.css`, computes WCAG relative luminance via `site/lib/contrast.ts`, and fails the suite if any text-bearing pair drops below 4.5:1.
Add a colour, add it to that test.

### The rule that matters most

**Green is a signal. Ink is the action.**

The old marketing site used the accent for kickers, step numbers, icons, checkmarks, citation superscripts, buttons and shell prompts.
Because it marked everything, it signalled nothing.

Now:

- `--accent` / `--color-accent` marks **verified, cited, live**. Nothing else.
- The primary action is an **ink pill**. In the app that is `--action`; on the site it is `bg-fg`.
- On the inverted band both invert: a paper pill with ink text.

If you find yourself filling a button with green, you are undoing this.
The token comment in `css/tokens.css` says so, on purpose.

---

## 4. Type

| Role | Face | Usage |
|---|---|---|
| Display | **Instrument Serif** 400 | `h1` and `h2` only |
| Body and UI | **Inter** | everything else |
| Technical | **JetBrains Mono** | micro-labels, code, data |

`h3` and below stay sans **on purpose**.
Serif at feature-title size pushes the page towards editorial magazine and away from technical product.

### Scale

```
h1    clamp(2.5rem, 3.6vw, 3.5rem)   lh 1.08   tracking -0.02em   serif
h2    clamp(2rem, 3.2vw, 2.75rem)    lh 1.10                      serif
h3    1.125rem                       Inter 500                    sans
body  1rem / 1.65                    Inter, muted
mono  0.6875rem  uppercase  0.14em   JetBrains Mono, muted
```

`h1` is sized to the hero's column, not to the viewport.
At the original 4.25rem cap the second sentence broke four ways and orphaned a word on its own line.

### Micro-labels

11px, uppercase, `0.14em` tracking, in **muted ink, not accent**.
They are metadata, and the contrast between a large light serif and a tiny caps label is what does the "designed" work.
Because they are 11px they must clear 4.5:1, which is why they use muted (5.02:1) rather than anything fainter.

Inside the app's dense chrome they drop to **10.5px**, same tracking and same muted.
The account dropdown carries two of them a divider apart (`SIGNED IN WITH …` and `CONNECTED SOURCES`); at 11px the longer of the two read as a heading rather than a caption and the pair looked like two ranks.
Contrast is a ratio and does not move with size, so muted still clears 4.5:1 at 10.5px.
This is not licence to shrink micro-labels on the site, which is airier and keeps 11px.

### Why the font is vendored

Instrument Serif is served from `/static/fonts/instrument-serif-latin.woff2`, never from `fonts.googleapis.com`.

A Google Fonts request ships every visitor's IP and user-agent to a third party on page load.
This product's entire promise is that nothing leaves the customer's cloud.
15KB on our own box is the cheapest possible way to not contradict ourselves.

The canvas declares its own `@font-face` inline rather than importing `tokens.css`, because that file also defines `--bg`/`--panel`/`--text` and would collide with the canvas's own token names.

---

## 5. Layout rules

**Hairlines and whitespace, not card chrome.**
The old home page had eleven bordered cards.
Containment is now done by rules and air.
If a section needs a border to feel separate, the spacing is wrong.

**One dominant object per screen.**
The hero has a single calm centrepiece, like the reference's portrait.

**Spacing rhythm:** sections are `py-20 lg:py-32`, container `max-w-6xl px-6`.
Sections sit flush, so all the space is padding; adjacent sections give ~256px between content.
The app is denser than the site on purpose: a working tool as airy as a landing page wastes an operator's screen.

**Measure.** Four columns of body text at `lg` gave 22 characters per line. Two columns is the floor for readable prose.

**A list of rows that must align is ONE grid, and every row must fill it.**
Rows of `label · value · optional action` do not line up if each row is its own flex container: each one lands wherever its own label ended.
The fix is one grid on the container with the rows set to `display: contents`, so all of them share the same tracks.
The trap that comes with it: auto-placement no longer knows where a row ends, so a row that emits fewer cells than the others pulls the next row's first cell up into the hole and the whole list folds into a snake.
Every row emits the same number of cells, with an empty span standing in for the absent one, and a test asserts the count rather than the appearance (`selftest_630`).
Put the flexible track on the **label**, not on the value or the action: a flexible value track gets squeezed and wraps, and a flexible action track leaves a dead column whenever no row has an action.

**One ornament.** A single asterisk, borrowed from the reference. Not a set of decorations.

---

## 6. Navigation

**Navigation never moves.**
This is the rule that was learned the hard way, twice in one session.

There is exactly one definition: `css/rail.css` + `js/ui/rail.js`.
Same items, same order, same position, same look.
The `NAV` array in `rail.js` is the single source of truth.

**Every destination in `NAV` must be a path the router can render in-document** - in
`SHELL_PATHS` *and* in `ROUTES`.
Miss either and that one click becomes a full page load while every other click stays a tab
switch, which is what Connectors did until #643.
`selftest_560_one_navigation_scheme.py` asserts both halves.

Design properties worth preserving:

- **Self-contained.** The rail reads no host token; everything it needs is declared on `.navrail`. This is what let it be the shared definition while there were two front-ends, and it costs nothing to keep.
- **Namespaced `navrail-*`.** The canvas surface owns `.rail` and `.rail-note` for its source panel.
- **Collapses to a 60px icon strip**, persisted in `localStorage`. No surface sets a collapse default (#556) - the user's own choice wins everywhere.

Icons are **inline hairline SVGs**, never emoji.
Emoji render as flat glyphs on Windows and coloured blobs on macOS, so the nav looked like a different product on every OS.
They set `stroke: currentColor` so the icon brightens with its label instead of staying a fixed tint.

---

## 7. Errors

**A status code is not an explanation.**

All error rendering goes through `js/ui/errors.js`.
It maps a thrown fetch error to a title, a body, and where possible the action that resolves it:

- 401 - "You are not signed in", with a Sign in action.
- 403 - explains the demo boundary.
- 429 - asks the user to wait.
- 5xx - states explicitly that it is **not** a permissions problem, so the user does not conclude they lack access.

This existed in four places before it existed in one.
Chat, connectors, Ask and the canvas each printed `Error: chat failed: 401` at users.
The canvas still does; that is `#412`.

---

## 8. Honesty rules

These are product rules, not visual ones, and they constrain the UI.

**Never invent metrics.**
No customer names, logos, counts or testimonials.
The reference's "6 years / 683 projects" stat row has no honest equivalent, so that slot carries real trust facts or stays empty.
A fabricated-customer strip was deleted from this site once already.

**Never assert a state you have not verified.**
The shell rendered a hardcoded "Signed in" label without checking for a token, so anonymous visitors were told they were signed in and then got a 401.
`/config` now reports `signed_in` and the UI has all three branches.

**Never show internal vocabulary.**
The canvas subtitle read "live - composes the real /router catalog (#109)", leaking a card number and the router's private terminology onto a user-facing page.

**An absent value is not an answer.**
This is enforced in the synthesis prompt (`src/dbsearch/ports/prompts.py`), because a model rendering a missing value as "None" made a correct permission refusal look like a bug.

**No em dashes**, anywhere, including prompts and UI copy. House style.

---

## 9. Deployment rules for UI changes

The UI is served by the FastAPI box behind Cloudflare, and that combination has a specific trap.

**Measured behaviour of the edge:**

```
GET /ask               -> no-cache        passed through
GET /version           -> no-store        passed through
GET /static/js/main.js -> max-age=14400   REWRITTEN by the CDN
```

It rewrites only what it classifies as a static asset.

**Therefore:**

1. Code assets (`.js`, `.css`) are served `no-store`, which survives that rewrite. Fonts keep revalidation.
2. The shell versions every asset URL: `/static/...?v=__DBS_BUILD__`, substituted server-side.
3. `_build_id` hashes the shell **and every js/css file**, not the shell alone. A CSS-only change must move the id, or the versioned URL is identical and the whole mechanism is silently useless.
4. `main.js` imports `rail.js` **dynamically** so that URL can carry the build id. A static specifier cannot.

**The rule this encodes:** never move markup out of the HTML into a separately-cached JS file without versioned URLs.
Doing exactly that deleted the entire left navigation for anyone with a warm cache.

---

## 10. Verification discipline

What "done" means for a UI change here:

- `npx vitest run` (site) and `python3 tests/selftest_ui_static.py` + `tests/selftest_nav_shell.py` (app).
- `npm run lint`, `npm run build`, `npx tsc --noEmit` for the site.
- `node --check` on every ES module touched. A text-slice deletion once left a stray brace and blanked every shell surface.
- A **real browser** on the deployed URL, not just the local one.
- Check the **deployed** HTML, not the local copy. A localhost fallback baked into a static export shipped links pointing at the visitor's own machine, and only grepping the live page caught it.
- At 1440 and 390: no horizontal scroll, `scrollWidth == clientWidth`, tap targets at least 24px.

Tests assert the **shared definition**, not per-page markup.
The nav selftests check `rail.js` contents rather than rendered markup, because the old assertions would have passed while two copies drifted.

And they must assert the **property**, not the mechanism that currently delivers it.
`selftest_634` once asserted "`/canvas` must NOT be in `SHELL_PATHS`" - correct when written, and it passed happily while the owner was looking at the reload it described.
A test written around today's architecture becomes a guard on that architecture.

---

## 11. How to continue a UI pass

A working order, learned from this one:

1. **Look at the live thing first**, as an anonymous visitor. Most of the real defects this session were found by loading the page and clicking, not by reading code.
2. **Check for an existing definition before writing a new one.** Ask whether the thing you are about to style exists twice already. It usually does.
3. **Tokens before components.** Every component here uses semantic keys, so a palette change is one file. Keep it that way; never hardcode a hex outside `globals.css` / `tokens.css`. The single permitted exception is `app/opengraph-image.tsx`, which Satori renders outside Tailwind, and it is annotated as such.
4. **Card the work before starting**, and decompose if it is more than one atomic unit.
5. **Fix the honesty bug before the styling bug.** A page that lies is worse than a page that is ugly.
6. **Deploy, then verify in a real browser with a warm cache**, which is what a returning user has.

### Known debt

| Card | What |
|---|---|
| `#412` | The canvas still prints raw backend errors; it does not import `ui/errors.js`. |
| `#399` | Ask/Draft/Connectors/Admin/Developer inherit the palette but keep their old composition. |
| `#404` | The marketing site's interactive Alice-vs-Bob demo, removed when the site became a static export. |

### The one-line summary

Paper canvas, ink action, green only where it means "verified", serif for headlines and mono for metadata, hairlines instead of boxes, and exactly one definition of anything that appears on more than one screen.

