// #937 - what /ask TELLS a caller about whether they have anything to search, when their
// content lives in a connector store rather than in the uploaded-document index.
//
// WHY A PROBE AND NOT A STRING SEARCH. The claim is "the page does not tell a connected user
// that nothing is indexed". Both halves are sentences ON SCREEN - one on first paint, one at
// the top of the Sources panel - and the second sits directly above the sources it denies.
// Only a mounted surface can show that adjacency.
//
// THE SCENARIOS ARE A MATCHED SET, and the set is the point - see SCENARIOS below for what
// each one isolates. The shortest version: a fix that simply DELETES the copy passes the
// connected case and fails the empty one, and a fix that reads the wrong counter passes both.
import { pathToFileURL } from "node:url";

const [, , jsdomPath, askPath, scenario] = process.argv;
const { JSDOM } = await import(pathToFileURL(jsdomPath).href);

const dom = new JSDOM(
  "<!doctype html><html><head></head><body><div id='root'></div></body></html>",
  { url: "http://localhost/ask" });
const { window } = dom;
for (const k of ["document", "window", "location", "HTMLElement", "Node", "Event",
                 "CustomEvent", "getComputedStyle", "MouseEvent", "KeyboardEvent",
                 "MutationObserver", "ResizeObserver", "DOMParser", "FormData"]) {
  Object.defineProperty(globalThis, k, { value: window[k], configurable: true, writable: true });
}
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async () => {} } }, configurable: true, writable: true,
});
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.localStorage.clear();
window.HTMLElement.prototype.scrollIntoView = function scrollIntoView() {};
if (!window.crypto) window.crypto = {};
window.crypto.randomUUID = () => "c-probe-937";
Object.defineProperty(globalThis, "crypto", { value: window.crypto, configurable: true });

// ---- the fixture ---------------------------------------------------------------------------
//
// THE DONE EVENT AS PROD ACTUALLY SENDS IT for a gdrive-backed answer, measured on
// dbsearch.ai 260823 as the reporting user: three citations from one connector store, and a
// `corpus` block reporting indexed:false. Those facts arrive TOGETHER and contradict each other -
// the counters describe the uploaded-document index, and a connector store builds its own
// (router/providers/connector.py: `index = InMemoryIndex(obj)`). The fixture must carry BOTH
// or it cannot show the defect.
const CITES = [
  { doc: "drive-doc-1", title: "notes.txt", uri: "" },
  { doc: "drive-doc-1", title: "notes.txt", uri: "" },
  { doc: "drive-doc-1", title: "notes.txt", uri: "" },
];

// EVERY SCENARIO EXISTS TO MAKE ONE CLAUSE FAIL ON ITS OWN. The first cut of this probe had
// only `connected` and `empty`, and in both of them `indexed:false` and `authorized_docs:0`
// were true together - so a guard reading either one passed, and three separate mutations
// survived. A fixture that satisfies both halves of a condition at once proves neither.
//
//   connected : corpus empty, source composed        -> both sentences must go
//   empty     : corpus empty, nothing composed       -> the boot sentence must stay (CONTROL)
//   unshared  : indexed:TRUE, authorized_docs:0      -> isolates the authorized_docs clause;
//               documents exist and none are this caller's, yet three sources are on screen
//   norows    : nothing retrieved, nothing composed  -> isolates the `retrieved &&` clause;
//               with no source anywhere, the denial is CORRECT and must survive
//   dryrun    : nothing retrieved, a source COMPOSED  -> the round-2 case, found on prod after
//               this card's first deploy: the question matched nothing, so the retrieval half
//               of the fix never fires, and the caller was told to connect what they had
//   unknown   : connected_sources null (unmeasured)  -> isolates `!== 0` from `> 0`; a
//               workspace store we could not read must not be rendered as "you have nothing"
const SCENARIOS = {
  connected: { corpus: { indexed: false, authorized_docs: 0 }, cites: CITES, composed: 1 },
  empty:     { corpus: { indexed: false, authorized_docs: 0 }, cites: CITES, composed: 0 },
  unshared:  { corpus: { indexed: true,  authorized_docs: 0 }, cites: CITES, composed: 1 },
  norows:    { corpus: { indexed: false, authorized_docs: 0 }, cites: [],    composed: 0 },
  dryrun:    { corpus: { indexed: false, authorized_docs: 0 }, cites: [],    composed: 1 },
  unknown:   { corpus: { indexed: false, authorized_docs: 0 }, cites: CITES, composed: null },
};
const S = SCENARIOS[scenario] || SCENARIOS.connected;

const DONE = {
  type: "done",
  answer: S.cites.length ? "Wave 1 is pure canvas rendering fixes [1]."
                         : "I do not have that information.",
  citations: S.cites,
  retrieved_docs: S.cites.length ? ["drive-doc-1"] : [],
  // The server ships `connected_sources` INSIDE the corpus block too (app.py `_corpus_block`),
  // because the answer surfaces need the second plane for the same reason the empty state does.
  corpus: { ...S.corpus, connected_sources: S.composed },
};

// What /ask/suggestions says on first paint. `connected_sources` is the fact the page was
// missing: how many sources this caller has composed, which is a different plane from the
// document count beside it.
const SUGGESTIONS = { known: true, indexed: S.corpus.indexed,
                      authorized_docs: S.corpus.authorized_docs,
                      connected_sources: S.composed, examples: [] };

// ---- the wire ------------------------------------------------------------------------------
// Stub `fetch`, never the module.
const J = (body) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });

function sse(events) {
  const text = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
  const bytes = new TextEncoder().encode(text);
  let sent = false;
  return Promise.resolve({
    ok: true, status: 200,
    body: { getReader: () => ({
      read: () => {
        if (sent) return Promise.resolve({ done: true, value: undefined });
        sent = true;
        return Promise.resolve({ done: false, value: bytes });
      },
    }) },
  });
}

globalThis.fetch = (path) => {
  const p = String(path);
  if (p.startsWith("/auth/me")) {
    return J({ enabled: true, signed_in: true, name: "Reporter",
               email: "reporter@example.com", google_enabled: true, linked: [] });
  }
  if (p.startsWith("/chat/stream")) return sse([DONE]);
  if (p.startsWith("/ask/suggestions")) return J(SUGGESTIONS);
  if (p.startsWith("/conversations/mine")) return J({ conversations: [] });
  if (p.startsWith("/conversations/shared-with-me")) return J({ conversations: [] });
  if (p.startsWith("/config")) return J({ env: [] });
  return J({});
};

// ---- drive it ------------------------------------------------------------------------------
const { mountAsk } = await import(pathToFileURL(askPath).href);
mountAsk(document.getElementById("root"));

const settle = () => new Promise((r) => setTimeout(r, 0));
for (let i = 0; i < 40; i++) await settle();

const flat = (e) => (e ? e.textContent.replace(/\s+/g, " ").trim() : "");

// MEASURED BEFORE THE QUESTION IS ASKED. The empty state is a first-paint sentence; reading it
// after an answer has replaced the transcript would report a state the probe itself cleared.
const bootBanner = flat(document.querySelector(".ask-note"));

const input = document.querySelector("#ask-input") || document.querySelector("textarea");
input.value = "what is Waves1 to 3 Aug 2026 ?";
const form = input.closest("form");
if (form) form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
else input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
for (let i = 0; i < 60; i++) await settle();

// Open the Sources panel the way a person does - by pressing the pill. The contradiction
// lives INSIDE the panel, so a probe that never opened it could not see it.
const pill = document.querySelector("button.sources-pill");
if (pill) pill.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
for (let i = 0; i < 30; i++) await settle();

const panel = document.querySelector(".sources-panel-body");
const panelNote = flat(panel && panel.querySelector(".authorized-note"));
const sourceRows = panel ? panel.querySelectorAll(".source-card, .sources-group > *").length : 0;

// With no citations there is no pill and no panel: ask.js renders the note INLINE under the
// answer instead (its `else` branch). That is the path the `norows` scenario exercises, and
// reading only the panel would have made this probe blind to it.
const bot = [...document.querySelectorAll(".msg-bot")].pop();
const answerNote = flat(bot && bot.querySelector(".authorized-note"));

console.log(JSON.stringify({
  scenario: scenario || "connected",
  boot_banner: bootBanner,
  panel_note: panelNote,
  answer_note: answerNote,
  // The note a person actually reads for this scenario: the panel's when there is a panel,
  // the inline one when there is not. Asserting against a single field keeps the tests from
  // silently passing because they looked at the empty one.
  shown_note: panel ? panelNote : answerNote,
  panel_present: !!panel,
  source_rows: sourceRows,
  pill_label: flat(pill),
}));
