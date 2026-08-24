// #689 / ADR 0025 slice 3 - drives the REAL Ask surface in a real DOM (jsdom) and reports what
// a signed-in person would actually see under a ROUTED answer. Read by
// tests/selftest_689_ask_proofs_dom.py.
//
// WHY A PROBE AND NOT A STRING SEARCH. The claim is "a routed answer explains itself on /ask
// the way it does on /canvas, and does not explain itself twice". Both halves are about what
// is ON SCREEN under an answer, and this repo has shipped tests that were green while exactly
// that was false - #611 pinned a disclosure sentence in a file nobody rendered, four times
// over. So this mounts what `mountAsk` mounts, stubs the wire (never the module), types a
// question, and reads the DOM that comes back.
//
// It is NOT a browser: no layout, no paint. It can say "this element is not in the document";
// it cannot say "this element is invisible" - the #772/`el.hidden` distinction. Every
// assertion below is about PRESENCE, TEXT and ATTRIBUTES, and the real-browser pass on prod is
// still owed.
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
// jsdom implements no layout, so it has no scrollIntoView. Stubbed rather than guarded around
// in the product: a surface that had to ask whether it can scroll would be carrying a test's
// limitation in shipped code.
window.HTMLElement.prototype.scrollIntoView = function scrollIntoView() {};
if (!window.crypto) window.crypto = {};
window.crypto.randomUUID = () => "c-probe-689";
Object.defineProperty(globalThis, "crypto", { value: window.crypto, configurable: true });

// ---- the fixtures -------------------------------------------------------------------------
//
// THE ROUTED DONE EVENT, in the shape server/ask_router.py's delegate actually emits: the
// router's own footnotes, and a `documents` footnote beside a SQL one - because the caller's
// documents are a store in the ask scope, which is the whole reason the pill must NOT also
// render. A fixture with SQL evidence alone could not show the double-provenance defect.
const ROUTED = {
  type: "done",
  answer: "APAC billed 205,000.00 [1], and staff receive 25 days of leave [2].",
  citations: [
    { store_id: "azure_sql-1", kind: "row", sql: "SELECT region, SUM(amount)",
      proof: { kind: "sql", store_id: "azure_sql-1", sql: "SELECT region, SUM(amount)" } },
    { store_id: "documents", kind: "chunk", doc: "hol-1", title: "Holiday Policy" },
  ],
  retrieved_docs: ["hol-1"],
  disclosure: "Asked but holds no data of this kind - not used: bigquery-1.",
  footnotes: [
    { n: 1, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL",
      origin: "Azure SQL · srv.database.windows.net / sales · table regional_totals",
      location: "srv.database.windows.net / sales", object: "table regional_totals",
      snippet: "region=apac, total_amount=205000.50", column_types: { total_amount: "num" },
      uri: "", sql: "SELECT region, SUM(amount)", rerun_token: "tok-1" },
    { n: 2, store_id: "documents", kind: "document", system: "Documents",
      origin: "Documents · indexed in DBSearch · hol-1",
      location: "indexed in DBSearch", object: "hol-1",
      snippet: "Staff receive 25 days of paid annual leave.", column_types: {},
      uri: "upload://holiday.txt", sql: "", rerun_token: "" },
  ],
  corpus: { indexed: true, authorized_docs: 4 },
};

// THE DOCUMENT-ONLY DONE EVENT - the flag-off path, and every deployment that has connected
// nothing. It must be BYTE-IDENTICAL to before this card: the pill, not the rail.
const DOCS_ONLY = {
  type: "done",
  answer: "Staff receive 25 days of paid annual leave [1].",
  citations: [{ doc: "hol-1", title: "Holiday Policy", uri: "upload://holiday.txt" }],
  retrieved_docs: ["hol-1"],
  corpus: { indexed: true, authorized_docs: 4 },
};

// #859: THE ROUTED ANSWER THAT REFERENCES ONLY SOME OF WHAT IT RETRIEVED. Since #856 the
// caller's documents are consulted on EVERY routed turn, so a revenue question now retrieves
// HR policies that answer nothing - and rendering them under the answer claims a provenance
// the answer never asserted (#724's rule, and canvas.js's own comment about a revenue answer
// that ended with an HR leave policy in its Sources list). `referenced` is what the server
// says the final answer actually points at.
const UNREFERENCED = {
  type: "done",
  answer: "APAC billed 205,000.00 [1] and EMEA billed 125,000.00 [3].",
  citations: [
    { store_id: "azure_sql-1", kind: "row", sql: "SELECT region, SUM(amount)",
      proof: { kind: "sql", store_id: "azure_sql-1", sql: "SELECT region, SUM(amount)" } },
    { store_id: "documents", kind: "chunk", doc: "hol-1", title: "Holiday Policy" },
    { store_id: "azure_sql-1", kind: "row", sql: "SELECT region, SUM(amount)",
      proof: { kind: "sql", store_id: "azure_sql-1", sql: "SELECT region, SUM(amount)" } },
    { store_id: "documents", kind: "chunk", doc: "dir-1", title: "Directorship guidelines" },
  ],
  retrieved_docs: ["hol-1", "dir-1"],
  referenced: [1, 3],
  footnotes: [
    { n: 1, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL",
      origin: "Azure SQL · srv / sales · table regional_totals",
      location: "srv / sales", object: "table regional_totals",
      snippet: "region=apac, total_amount=205000.00", column_types: {},
      uri: "", sql: "SELECT region, SUM(amount)", rerun_token: "tok-1" },
    { n: 2, store_id: "documents", kind: "document", system: "Documents",
      origin: "Documents · indexed in DBSearch · hol-1",
      location: "indexed in DBSearch", object: "hol-1",
      snippet: "Staff receive 25 days of paid annual leave.", column_types: {},
      uri: "upload://holiday.txt", sql: "", rerun_token: "" },
    { n: 3, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL",
      origin: "Azure SQL · srv / sales · table regional_totals",
      location: "srv / sales", object: "table regional_totals",
      snippet: "region=emea, total_amount=125000.00", column_types: {},
      uri: "", sql: "SELECT region, SUM(amount)", rerun_token: "tok-3" },
    { n: 4, store_id: "documents", kind: "document", system: "Documents",
      origin: "Documents · indexed in DBSearch · dir-1",
      location: "indexed in DBSearch", object: "dir-1",
      snippet: "External board appointments require approval.", column_types: {},
      uri: "upload://dir.txt", sql: "", rerun_token: "" },
  ],
  corpus: { indexed: true, authorized_docs: 4 },
};

// The CONTROL for it: the same four footnotes with NO referenced set - an answer that wrote no
// resolvable marker at all. Trimming to "referenced" there would empty the rail and leave the
// reader with nothing to check, so everything must still render.
const UNMARKED = { ...UNREFERENCED, answer: "The regional totals are set out below.",
                   referenced: [] };

const DONE = scenario === "docs_only" ? DOCS_ONLY
  : scenario === "unreferenced" ? UNREFERENCED
  : scenario === "unmarked" ? UNMARKED
  : ROUTED;

// ---- the wire -------------------------------------------------------------------------------
// Stub `fetch`, never the module. `/chat/stream` is served as a real SSE body so the surface's
// own parser runs - a stub that called `onDone` directly would skip the code under test.
const seen = [];
let rerunBody = null;
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

globalThis.fetch = (path, opts) => {
  const p = String(path);
  seen.push(p);
  if (p.startsWith("/auth/me")) {
    return J({ enabled: true, signed_in: true, name: "Owner", email: "owner@example.com",
               google_enabled: false, linked: [] });
  }
  if (p.startsWith("/chat/stream")) {
    // Tokens first, then done - the real contract, and the ONLY way to catch a surface that
    // renders its accumulator instead of `done.answer` (#257). The streamed draft deliberately
    // differs from the final answer AND carries an instruction marker the server strips.
    return sse([
      { type: "token", text: "APAC billed 205,000.00 [1]" },
      { type: "token", text: " [coverage]" },
      DONE,
    ]);
  }
  if (p.startsWith("/router/rerun")) {
    try { rerunBody = JSON.parse(opts.body); } catch { rerunBody = null; }
    return J({ cols: ["region", "total"], rows: [["apac", 205000]], count: 1, capped: false });
  }
  if (p.startsWith("/ask/suggestions")) return J({ prompts: [] });
  if (p.startsWith("/conversations/mine")) return J([]);
  if (p.startsWith("/conversations/shared-with-me")) return J([]);
  if (p.startsWith("/config")) return J({ env: [] });
  return J({});
};

// ---- drive it -------------------------------------------------------------------------------
const { mountAsk } = await import(pathToFileURL(askPath).href);
mountAsk(document.getElementById("root"));

const settle = () => new Promise((r) => setTimeout(r, 0));
for (let i = 0; i < 40; i++) await settle();

const input = document.querySelector("#ask-input") || document.querySelector("textarea");
input.value = "what is the total amount by region";
const form = input.closest("form");
if (form) form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
else input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
for (let i = 0; i < 60; i++) await settle();

const bot = [...document.querySelectorAll(".msg-bot")].pop();
const txt = (e) => (e ? e.textContent.trim().replace(/\s+/g, " ") : null);
const rail = bot && bot.querySelector(".ask-proofs");
// MEASURED BEFORE ANYTHING IS CLICKED. The marker click below OPENS the rail, so reading this
// afterwards would report the state the probe itself created - the shape of measurement error
// that makes a rig unable to show the bug it exists for.
const railClosedOnArrival = rail ? !rail.hasAttribute("open") : null;

// Click the [1] marker: it must OPEN the rail and highlight the source it names.
let openedByMarker = null;
let highlighted = null;
const marker = bot && bot.querySelector("button.cite-ref");
if (rail && marker) {
  rail.open = false;
  marker.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  openedByMarker = rail.open;
  highlighted = [...rail.querySelectorAll(".src.hl")].map((d) => d.id);
}

// Click "Verify data": it must reach /router/rerun with the token the server issued, and paint
// the returned rows. A button that renders and does nothing is the failure this catches.
let verified = null;
const verify = rail && rail.querySelector(".sverify");
if (verify) {
  verify.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  for (let i = 0; i < 20; i++) await settle();
  verified = txt(rail.querySelector(".sout"));
}

console.log(JSON.stringify({
  scenario: scenario || "routed",
  answer: txt(bot && bot.querySelector(".msg-body")),
  // The two provenance surfaces. Exactly one of them may be present on any one answer.
  has_rail: !!rail,
  has_pill: !!(bot && bot.querySelector(".sources-pill")),
  rail_summary: txt(rail && rail.querySelector("summary")),
  rail_closed_by_default: railClosedOnArrival,
  // #755: two "SOURCES - WHERE THIS ANSWER CAME FROM" headings on one screen was a real defect.
  headings: [...(bot ? bot.querySelectorAll(".shdr") : [])].map(txt),
  source_rows: [...(rail ? rail.querySelectorAll(".src") : [])].map((d) => ({
    num: txt(d.querySelector(".snum")), sys: txt(d.querySelector(".ssys")),
    tag: txt(d.querySelector(".stag")), loc: txt(d.querySelector(".sloc")),
    snippet: txt(d.querySelector(".osnip")),
    actions: [...d.querySelectorAll(".sbtn")].map(txt),
  })),
  disclosure: txt(bot && bot.querySelector(".authorized-note")),
  // A marker is resolvable only if the id it points at exists - checked in the DOM, because
  // that is what a click actually does.
  dangling_markers: [...(bot ? bot.querySelectorAll("button.cite-ref") : [])]
    .filter((b) => !(rail && rail.querySelector("#fn" + b.getAttribute("data-cite"))))
    .map((b) => b.textContent.trim()),
  opened_by_marker: openedByMarker,
  highlighted,
  verified,
  rerun_body: rerunBody,
  has_feedback: !!(bot && bot.querySelector(".feedback")),
  streamed_calls: seen.filter((p) => p.startsWith("/chat/stream") || p.startsWith("/router/ask")),
}, null, 1));
