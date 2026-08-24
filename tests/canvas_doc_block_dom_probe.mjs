// #724 - drives the REAL canvas ask dock in a real DOM (jsdom) and reports what the owner
// would actually see below a router answer, as JSON on stdout. Read by
// tests/selftest_724_doc_block.py.
//
// WHY A PROBE AND NOT A STRING SEARCH. The defect this exists to catch is not "the wrong
// branch is in the file" - it is "a block of text is on screen underneath an answer it has
// nothing to do with". Those are different claims, and this repo has shipped tests that were
// green while the second was false (see tests/shared_surface_dom_probe.mjs for the same
// argument). The suppression rule especially cannot be asserted from source: whether the doc
// block renders depends on a value that only exists after two fetches resolve in order.
//
// So this mounts the surface `mountCanvas` mounts, stubs the wire (never the module), types a
// question into #qtext, clicks #qask, and reads the resulting DOM.
//
// It is NOT a browser: no layout and no paint. So it can say "this element is not in the
// document"; it CANNOT say "this element is invisible" - a distinction that has bitten before
// (a panel whose .hidden was true sat open on screen behind an author `display:`). Every
// assertion below is therefore about PRESENCE and TEXT, and the real-browser pass is still owed.
//
// #760, THE ONE EXCEPTION: pass a stylesheet as argv[4] and it is injected before the mount,
// which switches on jsdom's CASCADE (not its layout). `getComputedStyle` then answers author
// questions - "is this element's computed display transformable", "does its transform actually
// change when the disclosure opens" - against the REAL rendered element rather than a fixture.
// That is strictly less than a browser and strictly more than a grep, and it is exactly the gap
// #745 shipped a defect through twice: a rule can be present, well-formed, and still apply to
// nothing. Absent argv[4] the probe behaves exactly as before, so existing callers are unchanged.
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";

const [, , jsdomPath, canvasPath, scenario, cssPath] = process.argv;
const { JSDOM } = await import(pathToFileURL(jsdomPath).href);

const dom = new JSDOM(
  "<!doctype html><html><head>"
  + (cssPath ? `<style>${readFileSync(cssPath, "utf8")}</style>` : "")
  + "</head><body><div id='root'></div></body></html>",
  { url: "http://localhost/canvas" });
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

// ---- the fixtures -----------------------------------------------------------------------
//
// THE ROUTER HALF is the same in every scenario and is deliberately a GOOD answer with real
// evidence: the bug is about what gets appended UNDERNEATH a correct answer, so an empty
// router response could not express it. Two footnotes, so the numbering collision has room
// to happen - [1] and [2] are taken before the document half renders anything.
const ROUTER_OK = {
  // The trailing " ." is not a typo: it is the shape the model emits and the shape the
  // server's dangling-marker sweep leaves behind, and it is what stranded a full stop on prod.
  answer: "Revenue for EMEA was 4.2M in Q2 [1], up from 3.8M in Q1 [2] .",
  // #715: `candidates` is every visible store the router scored. Two of these sit inside the
  // near-tie window of the winner and one does not, so the disclosure has something to include
  // AND something to leave out - a rule that only fires or only skips proves nothing.
  routing: { query_type: "analytical", method: "prefilter", confidence: 0.72,
             reason: "matched revenue columns",
             stores: [{ store_id: "azure_sql-1", business_unit: "finance", score: 0.584 }],
             candidates: [{ store_id: "azure_sql-1", business_unit: "finance", score: 0.584 },
                          { store_id: "storefront", business_unit: "retail", score: 0.549 },
                          { store_id: "support-tickets", business_unit: "support", score: 0.548 },
                          { store_id: "hr-wiki", business_unit: "people", score: 0.201 }],
             sub_queries: [] },
  outcomes: [{ store_id: "azure_sql-1", business_unit: "finance", status: "ok", count: 2 }],
  citations: [],
  footnotes: [
    // #729: `location` is the STORE and `object` is what inside it answered. The two footnotes
    // share a location on purpose - that is the case that rendered as two identical cards - and
    // the snippets carry every value shape the humanizer must get right: a measure with
    // decimals, an IDENTIFIER that must NOT be grouped, a midnight timestamp, and a real one.
    { n: 1, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL", origin: "Azure SQL · srv.database.windows.net / sales · table SalesOrderHeader",
      location: "srv.database.windows.net / sales", object: "table SalesOrderHeader",
      snippet: "customer_id=29485, TotalDue=43962.7901, due=2008-06-01 00:00:00",
      sql: "SELECT ...", rerun_token: "t1" },
    { n: 2, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL", origin: "Azure SQL · srv.database.windows.net / sales · table SalesOrderDetail",
      location: "srv.database.windows.net / sales", object: "table SalesOrderDetail",
      snippet: "qty=3, price=1200.50, shipped=2008-06-04 14:30:00",
      sql: "SELECT ...", rerun_token: "t2" },
  ],
};

// THE DOCUMENT HALF. `declines` is the shape the owner hit on prod: retrieval returned two
// documents that have nothing to do with the question, the model said so, and `referenced` is
// therefore empty while `citations` is not. That gap between the two lists IS the bug.
const DOCS = {
  declines: {
    answer: "I do not have that information.",
    citations: [{ doc: "d-fnd", title: "F&N directorship.pdf", uri: "https://sp/fnd.pdf" },
                { doc: "d-hr", title: "hr-leave-policy.txt", uri: "upload://hr-leave-policy.txt" }],
    referenced: [],
    retrieved_docs: ["d-fnd", "d-hr"],
    corpus: { authorized_docs: 12 },
  },
  answers: {
    answer: "Parental leave is 【1†L4-L6】 sixteen weeks for primary carers [2].",
    citations: [{ doc: "d-hr", title: "hr-leave-policy.txt", uri: "upload://hr-leave-policy.txt" },
                { doc: "d-hb", title: "handbook.pdf", uri: "https://sp/handbook.pdf" }],
    referenced: [1, 2],
    retrieved_docs: ["d-hr", "d-hb"],
    corpus: { authorized_docs: 12 },
  },
  // Half and half: the model pointed at the first document only. The second was READ and must
  // still be listed - but labelled, not dressed as support the answer never claimed.
  partial: {
    answer: "Parental leave is sixteen weeks [1].",
    citations: [{ doc: "d-hr", title: "hr-leave-policy.txt", uri: "upload://hr-leave-policy.txt" },
                { doc: "d-hb", title: "handbook.pdf", uri: "https://sp/handbook.pdf" }],
    referenced: [1],
    retrieved_docs: ["d-hr", "d-hb"],
    corpus: { authorized_docs: 12 },
  },
  // #255's existing rule, kept honest by this probe: nothing retrieved at all → no panel.
  empty: { answer: "", citations: [], referenced: [], retrieved_docs: [], corpus: { authorized_docs: 12 } },
};

// A router that found NOTHING. Needed because the #256 retraction - the line that rewrites the
// router's abstention into "the answer below comes from your documents" - only fires when the
// router produced no footnotes, so no scenario above can reach it. Mutation testing is what
// surfaced that: swapping the retraction's gate back to the SHOWN set left every test green.
// The falsifiers, as real evidence rows. `2024.06` and `1200.50` are the same shape, which is
// why no narrower grouping rule can tell them apart and why the rule was removed rather than
// tightened.
const ROUTER_VERSIONS = {
  answer: "The build in question is 2024.1.0 [1].",
  routing: { query_type: "lookup", method: "prefilter", confidence: 0.8, reason: "matched builds",
             stores: [{ store_id: "azure_sql-1", business_unit: "eng", score: 0.8 }],
             candidates: [], sub_queries: [] },
  outcomes: [{ store_id: "azure_sql-1", business_unit: "eng", status: "ok", count: 2 }],
  citations: [],
  footnotes: [
    { n: 1, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL", origin: "Azure SQL · srv.database.windows.net / builds · table releases",
      location: "srv.database.windows.net / builds", object: "table releases",
      snippet: "app_version=2024.1.0, period=2024.06, po=PO-12345.67, TotalDue=43962.7901",
      sql: "SELECT ...", rerun_token: "v1" },
    { n: 2, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL", origin: "Azure SQL · srv.database.windows.net / builds · table shipments",
      location: "srv.database.windows.net / builds", object: "table shipments",
      snippet: "a=2008-06-01 00:00:00+05:30, b=2008-06-02 00:00:00 UTC, c=2008-06-03 00:00:00",
      sql: "SELECT ...", rerun_token: "v2" },
  ],
};

// #729(a). The SAME evidence as ROUTER_VERSIONS above, with the column types the server now
// derives from the declared schema. Deliberately the falsifiers' own snippet: the whole claim
// is that the type - and only the type - tells `period=2024.06` from `price=1200.50`, so the
// fixture that killed the shape-based rule is the one that has to prove the type-based one.
//
// The map is PARTIAL on purpose. `app_version` and `po` have no entry (text columns), `total`
// has none (an alias the schema cannot resolve), and the untyped `versions` scenario above
// still runs beside this one - so "no type" is tested as a real state and not as an oversight.
//
// `customer_id: "int"` is a class the SERVER NEVER SENDS - `value_classes` emits only num/date/
// ts, and selftest_729_column_types asserts that integers stay unclassified. It is here as the
// renderer's forward-compatibility case: a class this canvas does not recognise must fall
// through to the raw value, because a browser can be a deploy behind the API it is talking to.
const ROUTER_TYPED = {
  answer: "The June figure is 1200.50 [1], booked on 2008-06-03 [2].",
  routing: { query_type: "lookup", method: "prefilter", confidence: 0.8, reason: "matched builds",
             stores: [{ store_id: "azure_sql-1", business_unit: "eng", score: 0.8 }],
             candidates: [], sub_queries: [] },
  outcomes: [{ store_id: "azure_sql-1", business_unit: "eng", status: "ok", count: 2 }],
  citations: [],
  footnotes: [
    { n: 1, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL", origin: "Azure SQL · srv.database.windows.net / builds · table releases",
      location: "srv.database.windows.net / builds", object: "table releases",
      snippet: "app_version=2024.1.0, period=2024.06, po=PO-12345.67, price=1200.50, "
             + "customer_id=29485, TotalDue=43962.7901, total=98765.4",
      column_types: { period: "date", price: "num", customer_id: "int", TotalDue: "num" },
      sql: "SELECT ...", rerun_token: "y1" },
    // The correctness fix the blind rule could not make: `booked` is a DATE column, so its
    // midnight is a driver artefact and goes; `seen_at` is a TIMESTAMP, where midnight is a
    // real instant the old rule deleted.
    { n: 2, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL", origin: "Azure SQL · srv.database.windows.net / builds · table shipments",
      location: "srv.database.windows.net / builds", object: "table shipments",
      snippet: "booked=2008-06-03 00:00:00, seen_at=2008-06-04 00:00:00",
      column_types: { booked: "date", seen_at: "ts" },
      sql: "SELECT ...", rerun_token: "y2" },
  ],
};

// #729(b). EVERY fixture above hardcodes `sub_queries: []`, so no DOM test in this file had
// ever rendered the compound path - and the compound path was the one that still printed
// routing telemetry above the answer. A whole shape of ask was untested, which is precisely
// why the two live verification asks (both simple) reported the rule as held.
//
// Two sub-questions, one of which reached NO store: the "no accessible source" arm has to be
// on screen too, and a fixture where every sub-query succeeds cannot show it.
const ROUTER_COMPOUND = {
  answer: "EMEA billed 125,000.00 [1]; the Krakow office has no headcount row [2].",
  routing: { query_type: "compound", method: "decompose", confidence: 0.66,
             reason: "decomposed into 2 sub-queries",
             stores: [{ store_id: "azure_sql-1", business_unit: "finance", score: 0.6 }],
             candidates: [],
             sub_queries: [
               { question: "what did EMEA bill?", query_type: "analytical",
                 stores: [{ store_id: "azure_sql-1" }] },
               { question: "what is the Krakow headcount?", query_type: "lookup", stores: [] },
             ] },
  outcomes: [{ store_id: "azure_sql-1", business_unit: "finance", status: "ok", count: 1,
               sub_question: "what did EMEA bill?" }],
  citations: [],
  // MIXED KINDS on purpose (verifier finding D4, seen live): a compound ask routes one half to
  // a document store and the other to a database, so the router's own rail holds both. The
  // summary used to assert "from your databases" over exactly this, with a Folder card four
  // lines below it. An all-SQL fixture cannot express that.
  footnotes: [
    { n: 1, store_id: "folder-1", kind: "document", system: "Folder", origin: "Folder · folder-1 · leave-policy.txt",
      location: "folder-1", object: "leave-policy.txt",
      snippet: "Primary carers receive 18 weeks of fully paid parental leave.", uri: "" },
    { n: 2, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL", origin: "Azure SQL · srv.database.windows.net / sales · table Billing",
      location: "srv.database.windows.net / sales", object: "table Billing",
      snippet: "region=EMEA, billed=125000.00", sql: "SELECT ...", rerun_token: "c1" },
  ],
  disclosure: "Not covered: 'what is the Krakow headcount?' — no source you can access returned results.",
};

// #747, the case seen LIVE: the router covered BOTH halves, and the bridge was still handed the
// whole question - so it answered the documentary half a SECOND time (the duplicate) and declined
// the other one ("Freight cost for each region - I do not have that information"), printed a
// screen below the figures the router had just retrieved. Both outcomes carry rows, so nothing
// here is uncovered and the bridge must not be asked at all.
const ROUTER_COMPOUND_COVERED = {
  answer: "Parental leave is sixteen weeks [1]; EMEA billed 125,000.00 [2].",
  routing: { query_type: "compound", method: "decompose", confidence: 0.71,
             reason: "decomposed into 2 sub-queries",
             stores: [{ store_id: "azure_sql-1", business_unit: "finance", score: 0.6 }],
             candidates: [],
             sub_queries: [
               { question: "what is the parental leave policy?", query_type: "semantic",
                 stores: [{ store_id: "folder-1" }] },
               { question: "what did EMEA bill?", query_type: "analytical",
                 stores: [{ store_id: "azure_sql-1" }] },
             ] },
  outcomes: [
    { store_id: "folder-1", business_unit: "people", status: "ok", count: 1,
      sub_question: "what is the parental leave policy?" },
    { store_id: "azure_sql-1", business_unit: "finance", status: "ok", count: 1,
      sub_question: "what did EMEA bill?" },
  ],
  citations: [],
  footnotes: [
    { n: 1, store_id: "folder-1", kind: "document", system: "Folder", origin: "Folder · folder-1 · leave-policy.txt",
      location: "folder-1", object: "leave-policy.txt",
      snippet: "Primary carers receive 18 weeks of fully paid parental leave.", uri: "" },
    { n: 2, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL", origin: "Azure SQL · srv.database.windows.net / sales · table Billing",
      location: "srv.database.windows.net / sales", object: "table Billing",
      snippet: "region=EMEA, billed=125000.00", sql: "SELECT ...", rerun_token: "cc1" },
  ],
};

// #751: the answer string is the REAL one captured off dbsearch.ai, byte for byte - five
// newlines, one blank line, three bullet lines - because a fixture I invent would encode what I
// think the model emits rather than what it does. The trailing double spaces are the model's too.
// The <script> is spliced in to keep proving the injection argument now that fmtAnswer emits
// block tags: esc() runs first, so this must arrive as visible text and never as an element.
const ROUTER_STRUCTURED = {
  answer: "The parental‑leave policy provides primary carers with 18 weeks of fully paid "
        + "parental leave【1】. <script>alert(1)</script>  \n\nFreight costs by region are:  \n"
        + "- EMEA: 43 962.7901  \n- APAC: 1 200.5000  \n- AMER: 875.2500【2】.",
  routing: { query_type: "analytical", method: "prefilter", confidence: 0.7, reason: "matched costs",
             stores: [{ store_id: "azure_sql-1", business_unit: "finance", score: 0.7 }],
             candidates: [], sub_queries: [] },
  outcomes: [{ store_id: "azure_sql-1", business_unit: "finance", status: "ok", count: 3 }],
  citations: [],
  footnotes: [
    { n: 1, store_id: "folder-1", kind: "document", system: "Folder", origin: "Folder · folder-1 · leave-policy.txt",
      location: "folder-1", object: "leave-policy.txt", snippet: "18 weeks", uri: "" },
    { n: 2, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL", origin: "Azure SQL · srv.database.windows.net / sales · table Billing",
      location: "srv.database.windows.net / sales", object: "table Billing",
      snippet: "region=EMEA, billed=125000.00", sql: "SELECT ...", rerun_token: "s1" },
  ],
};

// #751 round 2. The three shapes the verifier found the block renderer gets wrong, each of which
// the model demonstrably writes:
//   - a CONTINUATION line under a bullet (an indented wrap), which fragmented one list into two
//     lists with a paragraph wedged between them;
//   - `**bold**` spanning a blank line, where `[^*]+` crosses the newlines the blockifier then
//     splits on, emitting <p><strong>a</p><p>b</strong></p> - unbalanced HTML from model text.
const ROUTER_STRUCTURED_EDGE = {
  answer: "Freight costs by region:\n"
        + "- EMEA: 43 962.79\n"
        + "  including the fuel surcharge\n"
        + "- APAC: 1 200.50\n"
        + "\n"
        + "**Note that these figures\n\nexclude tax** [1].",
  routing: { query_type: "analytical", method: "prefilter", confidence: 0.7, reason: "matched costs",
             stores: [{ store_id: "azure_sql-1", business_unit: "finance", score: 0.7 }],
             candidates: [], sub_queries: [] },
  outcomes: [{ store_id: "azure_sql-1", business_unit: "finance", status: "ok", count: 3 }],
  citations: [],
  footnotes: [
    { n: 1, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL",
      origin: "Azure SQL · srv.database.windows.net / sales · table Billing",
      location: "srv.database.windows.net / sales", object: "table Billing",
      snippet: "region=EMEA, billed=125000.00", sql: "SELECT ...", rerun_token: "e1" },
  ],
};

// #751 round 2, the third shape: an answer with NO structure at all except a trailing newline.
// The comment on `_blockify` claims such an answer "returns byte-identical", and the newline
// test is what decides that - so a single trailing newline, which models emit constantly, sent a
// plain sentence down the block path and wrapped it in a <p> it never had before.
const ROUTER_TRAILING_NEWLINE = {
  answer: "EMEA billed 125,000.00 in Q2 [1].\n",
  routing: { query_type: "analytical", method: "prefilter", confidence: 0.7, reason: "matched",
             stores: [{ store_id: "azure_sql-1", business_unit: "finance", score: 0.7 }],
             candidates: [], sub_queries: [] },
  outcomes: [{ store_id: "azure_sql-1", business_unit: "finance", status: "ok", count: 1 }],
  citations: [],
  footnotes: [
    { n: 1, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL",
      origin: "Azure SQL · srv.database.windows.net / sales · table Billing",
      location: "srv.database.windows.net / sales", object: "table Billing",
      snippet: "region=EMEA, billed=125000.00", sql: "SELECT ...", rerun_token: "n1" },
  ],
};

// #764: a NUMBERED list, an ATX heading, and an unordered list in the same answer - the three
// shapes _blockify handled as run-on soup because the one production answer I measured happened
// to use "- " bullets. The ordered and unordered runs are adjacent on purpose: they must not be
// merged into one list, and the change of kind is the only signal that they are two.
const ROUTER_STRUCTURED_NUMBERED = {
  answer: "## Freight costs by region\n"
        + "1. EMEA: 43 962.79\n"
        + "2. APAC: 1 200.50\n"
        + "- AMER: 875.25 [1]\n"
        + "\n"
        + "### Method\n"
        + "Totals are gross of tax.",
  routing: { query_type: "analytical", method: "prefilter", confidence: 0.7, reason: "matched costs",
             stores: [{ store_id: "azure_sql-1", business_unit: "finance", score: 0.7 }],
             candidates: [], sub_queries: [] },
  outcomes: [{ store_id: "azure_sql-1", business_unit: "finance", status: "ok", count: 3 }],
  citations: [],
  footnotes: [
    { n: 1, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL",
      origin: "Azure SQL \u00b7 srv.database.windows.net / sales \u00b7 table Billing",
      location: "srv.database.windows.net / sales", object: "table Billing",
      snippet: "region=EMEA, billed=125000.00", sql: "SELECT ...", rerun_token: "z1" },
  ],
};

// #753: the ONE case where an outcome row still has to quote its sub-question. Both halves went
// to the SAME store, so the two rows are otherwise byte-identical and the store name cannot tell
// the reader which result belongs to which question.
const ROUTER_COMPOUND_SAME_STORE = {
  answer: "EMEA billed 125,000.00 [1]; APAC billed 80,000.00 [2].",
  routing: { query_type: "compound", method: "decompose", confidence: 0.7, reason: "decomposed into 2 sub-queries",
             stores: [{ store_id: "azure_sql-1", business_unit: "finance", score: 0.7 }],
             candidates: [],
             sub_queries: [
               { question: "what did EMEA bill?", query_type: "analytical",
                 stores: [{ store_id: "azure_sql-1" }] },
               { question: "what did APAC bill?", query_type: "analytical",
                 stores: [{ store_id: "azure_sql-1" }] },
             ] },
  outcomes: [
    { store_id: "azure_sql-1", business_unit: "finance", status: "ok", count: 1,
      sub_question: "what did EMEA bill?" },
    { store_id: "azure_sql-1", business_unit: "finance", status: "ok", count: 1,
      sub_question: "what did APAC bill?" },
  ],
  citations: [],
  footnotes: [
    { n: 1, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL", origin: "Azure SQL · srv / sales · table Billing",
      location: "srv / sales", object: "table Billing", snippet: "region=EMEA, billed=125000.00",
      sql: "SELECT ...", rerun_token: "ss1" },
    { n: 2, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL", origin: "Azure SQL · srv / sales · table Billing",
      location: "srv / sales", object: "table Billing", snippet: "region=APAC, billed=80000.00",
      sql: "SELECT ...", rerun_token: "ss2" },
  ],
};

const ROUTER_NONE = {
  answer: "No visible source holds this kind of data.",
  routing: { query_type: "lookup", method: "prefilter", confidence: 0.31, reason: "no match",
             stores: [], sub_queries: [] },
  outcomes: [], citations: [], footnotes: [],
  disclosure: "azure_sql-1 declined this question",
};

// scenario -> [router fixture, document fixture]
// #793: NUMBERS THAT ARE NOT LIST MARKERS, plus a run that does not start at 1.
// _NUMBERED was `\d+[.)]\s+` and the marker is DELETED when an item is pushed, so every one of
// these lines lost its number: "2008. Revenue was 4.2M" rendered as an <ol> item reading
// "Revenue was 4.2M" beside a counter saying "1.". The year was simply gone from the screen.
// The fix is TWO independent narrowings - at most two digits, and a run of at least two items -
// and this fixture is built so that EACH ONE IS SEPARATELY LOAD-BEARING. A first cut had every
// case saved by both at once, and the mutation matrix reported both narrowings as removable with
// no guard noticing: exactly the #788 shape, where every fixture exercises only one side.
//   * TWO ADJACENT YEARS   -> a genuine run of two, so ONLY the digit cap keeps them out of a list
//   * a LONE two-digit line -> passes the cap, so ONLY the run requirement keeps its marker
//   * a PAGE NUMBER with ")" -> the other marker spelling, saved by the cap
//   * a real list AT 12-13  -> must still BE a list, and must keep the start the model wrote
const ROUTER_NUMBERS_IN_PROSE = {
  answer: "History:\n"
        + "2008. Revenue was 4.2M [1]\n"
        + "2009. Revenue was 5.1M\n"
        + "\n"
        + "See page:\n"
        + "1997) the merger closed\n"
        + "\n"
        + "Final tally:\n"
        + "12. items were reconciled\n"
        + "\n"
        + "Remaining steps:\n"
        + "12. reconcile the ledger\n"
        + "13. archive the quarter",
  routing: { query_type: "analytical", method: "prefilter", confidence: 0.7, reason: "matched",
             stores: [{ store_id: "azure_sql-1", business_unit: "finance", score: 0.7 }],
             candidates: [], sub_queries: [] },
  outcomes: [{ store_id: "azure_sql-1", business_unit: "finance", status: "ok", count: 1 }],
  citations: [],
  footnotes: [
    { n: 1, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL",
      origin: "Azure SQL \u00b7 srv.database.windows.net / sales \u00b7 table Billing",
      location: "srv.database.windows.net / sales", object: "table Billing",
      snippet: "year=2008, revenue=4200000.00", sql: "SELECT ...", rerun_token: "y1" },
  ],
};

// #788: THE SIDES NO FIXTURE EVER EXERCISED. Every outcome in this file was status:"ok", every
// rail carried at least one sql citation, and every business_unit was a real name - so three
// product branches could be changed with nothing going red. All three shapes below are ones the
// LIVE product emits routinely: the 260818 prod pass rendered
// "✗ bigquery-1 · declined · … · this source holds no data of the kind the question asks about"
// on the first compound ask asked of it.
//   * a NON-OK outcome  -> must render ✗ and NO result count (the ✓ arm appends "· N results")
//   * an ALL-DOCUMENT rail -> must say "from your documents", never "from your databases" (#750/#728)
//   * business_unit "unassigned" -> is a PLACEHOLDER, never a label to print (router/decision.py)
const ROUTER_DECLINED_AND_DOCS = {
  answer: "Employees accrue 26 days of paid annual leave [1].",
  routing: { query_type: "semantic", method: "decompose", confidence: 0.5, reason: "matched leave",
             stores: [{ store_id: "folder-1", business_unit: "unassigned", score: 0.6 }],
             candidates: [], sub_queries: [] },
  outcomes: [
    { store_id: "folder-1", business_unit: "unassigned", status: "ok", count: 2 },
    // The one that matters: a store that did NOT succeed. `count` is deliberately non-zero so a
    // guard cannot pass by accident on "0 results" - the count must not be PRINTED at all.
    { store_id: "bigquery-1", business_unit: "unassigned", status: "declined", count: 7 },
    { store_id: "azure_sql-1", business_unit: "finance", status: "timeout", count: 3 },
  ],
  citations: [],
  footnotes: [
    { n: 1, store_id: "folder-1", kind: "document", system: "Folder",
      origin: "Folder \u00b7 folder-1 \u00b7 leave-policy.txt",
      location: "folder-1", object: "leave-policy.txt",
      snippet: "Full-time employees accrue 26 days of paid annual leave", rerun_token: "d1" },
  ],
};

// #788 (e) and (f): the two whitespace shapes models emit constantly and no fixture had.
//   * a PARAGRAPH BREAK WITH A SPACE IN IT ("\n \n"). Every fixture here used a bare "\n\n",
//     so `html.split(/\n\s*\n/)` and the emphasis rule's `(?!\n\s*\n)` could both be narrowed
//     to `\n\n` and stay green - while a real answer stopped splitting into paragraphs at all.
//   * an INDENTED LINE WITH NO LIST OPEN. Every continuation fixture followed a bullet, so the
//     `items.length &&` guard was unreachable. Without it the line writes to items[-1] - a
//     property `length` never sees - so flushList emits nothing and THE LINE DISAPPEARS.
const ROUTER_LOOSE_STRUCTURE = {
  answer: "Totals are provisional.\n \nThe **Q2 figures** are final [1].\n"
        + "   this indented line has no list open and must still reach the reader",
  routing: { query_type: "analytical", method: "prefilter", confidence: 0.6, reason: "matched",
             stores: [{ store_id: "azure_sql-1", business_unit: "finance", score: 0.6 }],
             candidates: [], sub_queries: [] },
  outcomes: [{ store_id: "azure_sql-1", business_unit: "finance", status: "ok", count: 1 }],
  citations: [],
  footnotes: [
    { n: 1, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL",
      origin: "Azure SQL \u00b7 srv.database.windows.net / sales \u00b7 table Billing",
      location: "srv.database.windows.net / sales", object: "table Billing",
      snippet: "q=2, revenue=125000.00", sql: "SELECT ...", rerun_token: "L1" },
  ],
};

// #786: DOES esc() STILL ESCAPE? At HEAD it does, and the outcome row calls it - there is no
// live vulnerability. But nothing asserted it: the only two tests mentioning `esc(` do substring
// arithmetic on the SOURCE TEXT ("the literal `esc(` appears inside this function"), and the one
// behavioural check exercises the `<` branch in element CONTENT. Delete the double quote from
// esc()'s character class and seven suites stay green while eleven attribute sinks become
// injectable - including the footnote `title=`, which is fed straight from the model's own text.
// The values below are what a hostile document (or a confused model) can put in a store label.
const ROUTER_HOSTILE_LABELS = {
  answer: "Revenue was 4.2M [1].",
  routing: { query_type: "analytical", method: "prefilter", confidence: 0.7, reason: "matched",
             stores: [{ store_id: 'folder-1<img src=x onerror=alert(1)>',
                        business_unit: "finance", score: 0.7 }],
             candidates: [], sub_queries: [] },
  // TWO different escapes are under test and they defend DIFFERENT sinks, so the fixture needs
  // both characters. `"` defends ATTRIBUTES (the uri -> href= below). `<` defends ELEMENT
  // CONTENT, which is what the outcome-row label is - a stray quote there is harmless, so a
  // fixture carrying only quotes cannot notice esc() being dropped from that line at all.
  outcomes: [{ store_id: 'folder-1<img src=x onerror=alert(1)>',
               business_unit: 'fin"ance<b onclick="alert(1)">', status: "ok", count: 1 }],
  citations: [],
  footnotes: [
    // kind:"document" WITH a uri is the shape that renders the `href=` sink (canvas.js:2064) -
    // the sql shape renders buttons instead and reaches no attribute at all.
    { n: 1, store_id: 'folder-1<img src=x onerror=alert(1)>', kind: "document", system: "Folder",
      origin: 'Folder \u00b7 folder-1" onerror="alert(1) \u00b7 leave-policy.txt',
      location: 'folder-1" onerror="alert(1)', object: "leave-policy.txt",
      uri: 'https://x.example/"><img src=x onerror=alert(1)>',
      snippet: "region=EMEA", rerun_token: "h1" },
  ],
};

// #784: A FIXTURE WHOSE SEGMENTS COLLIDE. #761's second guard ("an outcome row never prints the
// same segment twice") was load-bearing when written: the label was two concatenated strings,
// `lbl` + `obu`, and a business unit matching an origin segment rendered "... finance · finance
// ...". Six commits later #753 round 2 rebuilt the label as ONE array with a first-occurrence
// -wins filter, so the duplicate is never COMPOSED and the guard cannot go red for any input -
// product code shielding a guard from its own defect. The dedup is a genuine improvement and
// stays; the guard is re-pointed at the dedup itself, which needs an input that would collide.
// business_unit "finance" is deliberately also the second origin segment.
const ROUTER_COLLIDING_LABEL = {
  answer: "Revenue was 4.2M [1].",
  routing: { query_type: "analytical", method: "prefilter", confidence: 0.7, reason: "matched",
             stores: [{ store_id: "azure_sql-1", business_unit: "finance", score: 0.7 }],
             candidates: [], sub_queries: [] },
  outcomes: [{ store_id: "azure_sql-1", business_unit: "finance", status: "ok", count: 1 }],
  citations: [],
  footnotes: [
    { n: 1, store_id: "azure_sql-1", kind: "sql", system: "Azure SQL",
      origin: "Azure SQL \u00b7 finance \u00b7 table Billing",
      location: "finance", object: "table Billing",
      snippet: "region=EMEA", sql: "SELECT ...", rerun_token: "c1" },
  ],
};

const SCENARIOS = {
  declines: [ROUTER_OK, DOCS.declines],
  // #788: a declined AND a timed-out outcome, an all-document rail, and "unassigned".
  declined_and_docs: [ROUTER_DECLINED_AND_DOCS, DOCS.empty],
  // #788: "\n \n" as a paragraph break, and an indented line with no list open.
  loose_structure: [ROUTER_LOOSE_STRUCTURE, DOCS.empty],
  // #786: quotes and injection attempts in every model-derived label and uri.
  hostile_labels: [ROUTER_HOSTILE_LABELS, DOCS.empty],
  // #784: business_unit equals an origin segment, so the label MUST dedupe.
  colliding_label: [ROUTER_COLLIDING_LABEL, DOCS.empty],
  // #793: a year, a page number and a list starting at 12, in one answer.
  numbers_in_prose: [ROUTER_NUMBERS_IN_PROSE, DOCS.empty],
  answers: [ROUTER_OK, DOCS.answers],
  partial: [ROUTER_OK, DOCS.partial],
  empty: [ROUTER_OK, DOCS.empty],
  // Both halves came up empty - the case the retraction gate actually guards.
  silent_router: [ROUTER_NONE, DOCS.declines],
  // The router found nothing and the DOCUMENTS did answer: the retraction SHOULD fire here.
  silent_router_docs_answer: [ROUTER_NONE, DOCS.answers],
  // TWO ASKS IN ONE SESSION, the second with a router ERROR. #qresult is persistent and
  // innerHTML does not clear dataset, so ask 1's footnote count leaked into ask 2 and the
  // document half numbered its sources [3][4] with no [1][2] on screen. Every other scenario
  // here does exactly one ask, which is why the shipped probe could not express this.
  stale_offset: [ROUTER_OK, DOCS.answers],
  // Every value shape that falsified the first cut of the snippet formatter, carried by the
  // router's own evidence: version strings, a period key, a hyphenated purchase order, a zoned
  // instant, and a zone NAME. All must arrive on screen exactly as they left the database.
  versions: [ROUTER_VERSIONS, DOCS.empty],
  // #729(b): the compound shape, paired with a document half that declines so the only thing
  // that can precede the answer is the sub-question block this scenario exists to place.
  compound: [ROUTER_COMPOUND, DOCS.declines],
  // #747: the router answered EVERY sub-question. Paired with a doc half that ANSWERS, because
  // that is the live shape - the bridge had something to say and said it about a question that
  // was already answered. If it is asked at all here, the duplicate is back.
  compound_covered: [ROUTER_COMPOUND_COVERED, DOCS.answers],
  // #751: the model's own markdown, rendered.
  structured: [ROUTER_STRUCTURED, DOCS.empty],
  // #751 round 2: the three shapes the block renderer got wrong.
  structured_edge: [ROUTER_STRUCTURED_EDGE, DOCS.empty],
  trailing_newline: [ROUTER_TRAILING_NEWLINE, DOCS.empty],
  structured_numbered: [ROUTER_STRUCTURED_NUMBERED, DOCS.empty],
  compound_same_store: [ROUTER_COMPOUND_SAME_STORE, DOCS.empty],
  // #759: the Route ADVISOR on a compound question - a different button, a different endpoint
  // and a third renderer of `reason`, which is why #753's server-side edit degraded it without
  // any test in this file noticing. Driven below by clicking #qroute instead of #qask.
  route_compound: [ROUTER_COMPOUND, DOCS.empty],
  // #729(a): the same values as `versions`, now with the server's column types beside them.
  // Run as a PAIR - one scenario proves the formatting, the other proves its absence.
  typed: [ROUTER_TYPED, DOCS.empty],
};

// #761: dump every footnote fixture and exit, so a python guard can check each one against the
// SERVER function that builds the field (`router_api._origin_str`) instead of against my memory
// of it. All fourteen of these carried `system · business_unit` while the server has only ever
// emitted `system · location · object`, and the canvas's outcome row - which takes the first two
// segments as the store label and then appends the business unit - printed the business unit
// TWICE for a year of fixtures with no assertion noticing.
if (scenario === "__fixtures__") {
  const named = { ROUTER_OK, ROUTER_VERSIONS, ROUTER_TYPED, ROUTER_COMPOUND,
                  ROUTER_STRUCTURED_EDGE, ROUTER_TRAILING_NEWLINE, ROUTER_STRUCTURED_NUMBERED,
                  ROUTER_COMPOUND_COVERED, ROUTER_COMPOUND_SAME_STORE, ROUTER_STRUCTURED,
                  ROUTER_NONE };
  console.log(JSON.stringify(Object.entries(named).flatMap(([fixture, r]) =>
    (r.footnotes || []).map((f) => ({ fixture, ...f }))), null, 1));
  process.exit(0);
}

// scenario -> does the SECOND /router/ask fail? (only the two-ask scenario asks twice)
const TWO_ASKS = scenario === "stale_offset";
// #759: drive the Route ADVISOR (#qroute -> POST /router/route) instead of the ask dock.
const ROUTE_MODE = scenario.startsWith("route_");

const picked = SCENARIOS[scenario];
if (!picked) throw new Error(`unknown scenario ${scenario}`);
const [router, doc] = picked;

// ---- the wire ----------------------------------------------------------------------------
// Stub `fetch`, never the module. Everything the boot path asks for gets a minimal honest
// answer; the two calls this probe is about get the fixtures above.
const seen = [];
const searchAsked = [];
let askCount = 0;
const J = (body) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
globalThis.fetch = (path, opts) => {
  const p = String(path);
  seen.push(p);
  if (p.startsWith("/auth/me")) {
    return J({ enabled: true, signed_in: true, name: "Owner", email: "owner@example.com",
               google_enabled: false, linked: [] });
  }
  // #759: the Route advisor's own endpoint. It returns the full RoutingDecision - the SAME
  // object nested at `routing` in an ask response - which is the point of the card: sub_queries
  // was already on this wire and the panel was throwing it away.
  if (p.startsWith("/router/route")) return J(router.routing);
  if (p.startsWith("/router/ask")) {
    askCount += 1;
    // The second ask's router call fails outright — the path that leaves `fnCount` unwritten,
    // and therefore the path that exposes whatever the PREVIOUS ask left behind.
    if (TWO_ASKS && askCount === 2) {
      return Promise.resolve({ ok: false, status: 502,
                               json: () => Promise.resolve({ detail: "azure_sql-1 unreachable" }) });
    }
    return J(router);
  }
  // #747: WHAT the bridge is asked matters now, not just whether it was. Record the question so
  // a test can prove the router-covered half is never put to the documents.
  if (p.startsWith("/search")) {
    try { searchAsked.push(JSON.parse(opts.body).question); } catch { searchAsked.push(null); }
    return J(doc);
  }
  if (p.startsWith("/router/manifest")) {
    return J({ business_units: [{ name: "finance", sources: [] }],
               stores: [{ store_id: "azure_sql-1", kind: "azure_sql", business_unit: "finance" }] });
  }
  if (p.startsWith("/admin/sources")) return J([]);
  if (p.startsWith("/config")) return J({ env: [] });
  return J({});
};

// ---- drive it ------------------------------------------------------------------------------
const { mountCanvas } = await import(pathToFileURL(canvasPath).href);
mountCanvas(document.getElementById("root"));

const settle = () => new Promise((r) => setTimeout(r, 0));
for (let i = 0; i < 40; i++) await settle();      // let boot's fetch chain drain

document.getElementById("qtext").value = "what was revenue for EMEA in Q2";
// #759: the Route advisor is a SEPARATE button driving a SEPARATE endpoint into the same
// #qresult. Clicking #qask could never reach it, which is how a regression in it went unseen.
document.getElementById(ROUTE_MODE ? "qroute" : "qask").click();
for (let i = 0; i < 40; i++) await settle();      // /router/ask then /search, in order

if (TWO_ASKS) {
  // A SECOND ask through the same #qresult. Nothing is reset between them by the page, which
  // is the whole point: what survives here is what a real user carries from question to
  // question without ever reloading.
  document.getElementById("qtext").value = "what is our parental leave policy";
  document.getElementById("qask").click();
  for (let i = 0; i < 60; i++) await settle();
}

const out = document.getElementById("qresult");
const qsp = document.getElementById("qsp");
const txt = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : null);

// Numbers as the READER meets them: every [n] marker in the prose, and every [n] heading the
// Sources rails print - across BOTH halves, because the collision only exists between them.
const markers = [...out.querySelectorAll("sup.fnref")].map((s) => s.textContent.trim());
const railNums = [...out.querySelectorAll(".src .snum")].map((s) => s.textContent.trim());

// #760: every <details> the surface actually rendered, and - when a stylesheet was injected -
// what the CASCADE does to its own marker glyph as it opens.
//
// The glyph is found by SEARCHING THE RENDERED SUMMARY for a marker character rather than by
// looking up a class name, for the same reason the python guard does: a class name is what I
// remembered writing, and the character is what the reader sees. Read closed first, then open,
// then restore - because the property that matters is not "a transform is declared", it is
// "the declared transform is different once it opens", and only one of those can be faked by a
// rule that applies to no element.
const MARKER_GLYPHS = /[▸▾▶►▼▴◂]/;
const styleOf = (el) => {
  if (!el || !cssPath) return null;
  const cs = window.getComputedStyle(el);
  return { display: cs.display, transform: cs.transform || "none", listStyle: cs.listStyle };
};
const disclosures = [...out.querySelectorAll("details")].map((d) => {
  const sum = d.querySelector("summary");
  // The innermost element carrying the glyph: <span class="tri">, not the <summary> around it.
  const glyph = sum
    ? [...sum.querySelectorAll("*")].reverse().find((e) => MARKER_GLYPHS.test(e.textContent))
    : null;
  const was = d.open;
  d.open = false; const closed = styleOf(glyph);
  d.open = true; const open = styleOf(glyph);
  d.open = was;
  return {
    details_class: d.className || null,
    // Whether the SUMMARY draws a marker of its own, which is what makes suppressing the
    // native one mandatory - and makes replacing what it did mandatory too.
    own_glyph: !!glyph,
    glyph_class: glyph ? (glyph.className || null) : null,
    glyph_text: glyph ? glyph.textContent.trim() : null,
    summary_style: styleOf(sum),
    glyph_closed: closed,
    glyph_open: open,
  };
});

console.log(JSON.stringify({
  scenario,
  doc_panel_present: !!qsp,
  doc_panel_text: txt(qsp),
  // The Sources HEADING is the provenance claim. Its presence inside the document panel is
  // the thing that must not survive a decline.
  doc_sources_heading: qsp ? !!qsp.querySelector(".shdr") : false,
  doc_source_rows: qsp ? [...qsp.querySelectorAll(".src")].map((d) => ({
    num: txt(d.querySelector(".snum")),
    tag: txt(d.querySelector(".stag")),
    loc: txt(d.querySelector(".sloc")),
    unused: d.classList.contains("src--unused"),
  })) : [],
  router_answer_text: txt(out.querySelector(".qanswer")),
  // #715 / #729: the routing story around the answer.
  also_matched: txt(out.querySelector(".qalso")),
  trace_summary: txt(out.querySelector(".tracefoot summary")),
  trace_text: txt(out.querySelector(".tracefoot")),
  // #729(b): WHERE the per-sub-question routing lines live. Reported from both sides, because
  // "moved into the trace" and "deleted" are indistinguishable if you only count the trace.
  // Identified by the italic sub-question they carry, which is what separates them from the
  // per-outcome rows beside them (those quote a sub_question as plain text) and from the
  // document half's "↳ searching documents…" placeholder.
  sub_lines_in_trace: [...out.querySelectorAll(".tracefoot .qmeta")]
    .filter((d) => d.querySelector("i")).map((d) => txt(d)),
  sub_lines_before_answer: [...out.querySelectorAll(".qmeta")]
    .filter((d) => !d.closest(".tracefoot") && d.querySelector("i")).map((d) => txt(d)),
  // #761: the PER-STORE OUTCOME rows, identified by the ✓/✗ they open with - which is what
  // separates them from the telemetry line and the sub-question lines sharing their class.
  // Reported as composed text rather than as fields, because the defect (the business unit
  // printed twice) exists in neither input and only in the string they compose into.
  // #786: the MARKUP of the trace rows, not just their text. `outcome_rows` below is
  // textContent, and an injected `<img onerror=...>` contributes ZERO characters to
  // textContent - so dropping esc() makes that string look CLEANER, not dirtier, and every
  // guard reading it gets greener as the surface gets less safe. Anything asserting on
  // escaping has to read this field instead.
  trace_html: [...out.querySelectorAll(".tracefoot .qmeta")].map((d) => d.innerHTML).join("\n"),
  // #786: THE PROPERTY THAT ACTUALLY MATTERS. esc()'s double-quote branch defends ATTRIBUTE
  // sinks - href=, title=, data-pin=, value= - not element content, where a stray `"` is
  // harmless. So scan the whole rendered panel for what a broken esc() would produce: an
  // element nobody rendered on purpose, or any event-handler attribute at all.
  injected_elements: [...out.querySelectorAll("img, script, iframe, object, embed, svg")]
    .map((e) => e.tagName.toLowerCase()),
  event_handler_attrs: [...out.querySelectorAll("*")].flatMap((e) =>
    [...e.attributes].map((a) => a.name).filter((n) => n.startsWith("on"))),
  // Every attribute value the panel carries that came from model/server text, so a guard can
  // assert the hostile string arrived as DATA rather than as markup.
  attr_values: [...out.querySelectorAll("*")]
    .flatMap((e) => [...e.attributes].map((a) => ({
      tag: e.tagName.toLowerCase(), name: a.name, value: a.value })))
    .filter((a) => a.name !== "class"),
  outcome_rows: [...out.querySelectorAll(".tracefoot .qmeta")]
    .map((d) => txt(d)).filter((t) => t && /^[✓✗]/.test(t)),
  // #753: the store each of those rows is FOR, straight off the fixture and in the same order.
  // Without it a test can only check that a row looks plausible; with it a test can check the
  // row names the store it reports on - which is the join the ↳ lines above depend on.
  outcome_store_ids: (router.outcomes || []).map((o) => o.store_id),
  // #759: the Route advisor's panel, read the same way - the header line that renders `reason`,
  // the per-sub-question lines it must ALSO render now, and the candidate rows (which are the
  // union across sub-questions and carry no question mapping of their own).
  route_header: txt(out.querySelector(".qroutehdr")),
  route_sub_lines: [...out.querySelectorAll(".qmeta")]
    .filter((d) => !d.closest(".tracefoot") && d.querySelector("i")).map((d) => txt(d)),
  route_candidates: [...out.querySelectorAll(".qcand")].map((d) => txt(d)),
  // #729: the telemetry must no longer be the first thing on screen. Read as the first
  // non-empty block INSIDE #qresult, which is what a reader's eye actually lands on.
  first_block_text: txt([...out.children].find((c) => txt(c))),
  // #729: a full stop left floating a space clear of the sentence.
  stranded_punctuation: /<\/sup>\s+[.,;:!?)]/.test(out.innerHTML),
  // Where the document line sits among #qresult's own children - the "nothing contributed"
  // line belongs beside the routing notes, not a scroll below the source cards.
  child_order: [...out.children].map((c) => c.className || c.tagName.toLowerCase()),
  // #747: every question the DOCUMENT BRIDGE was asked, in order. Reported rather than a
  // was-it-called boolean, because the defect was not that it ran - it was WHAT it ran on.
  bridge_asked: searchAsked,
  // #751: the SHAPE of the rendered answer. Element names rather than text, because the defect
  // was that structure the model sent did not become structure on screen.
  answer_blocks: out.querySelector('.qanswer')
    ? [...out.querySelector('.qanswer').children].map((c) => c.tagName.toLowerCase()) : [],
  answer_list_items: [...out.querySelectorAll('.qanswer li')].map((e) => txt(e)),
  answer_has_element_from_model: !!(out.querySelector('.qanswer')
    && out.querySelector('.qanswer').querySelector('script')),
  // #751 round 2: the RAW markup, because unbalanced tags are invisible to any structural read -
  // the parser silently repairs <p><strong>a</p><p>b</strong></p> into something well-formed, so
  // reading back tag names can never show the defect that produced it.
  answer_html: out.querySelector('.qanswer') ? out.querySelector('.qanswer').innerHTML : null,

  // #729: the router's own Sources rail - what each citation says it points at, and how its
  // evidence renders.
  router_source_rows: [...out.querySelectorAll(".sources .src")]
    .filter((d) => !d.closest("#qsp"))
    .map((d) => ({ num: txt(d.querySelector(".snum")), loc: txt(d.querySelector(".sloc")),
                   snippet: txt(d.querySelector(".osnip")) })),
  markers,
  rail_numbers: railNums,
  // #760: null styles unless a stylesheet was passed as argv[4].
  css_loaded: !!cssPath,
  disclosures,
  // A marker is only resolvable if the id it points at exists. Checked in the DOM rather than
  // by comparing numbers, because that is what a click actually does.
  dangling_markers: [...out.querySelectorAll("sup.fnref")].filter((s) => {
    const id = s.dataset.dfn ? "dfn" + s.dataset.dfn : "fn" + s.dataset.fn;
    return !out.querySelector("#" + id);
  }).map((s) => s.textContent.trim()),
  // #724: nested <sup> was the latent fmtAnswer defect - one marker, two click handlers.
  nested_markers: out.querySelectorAll("sup.fnref sup.fnref").length,
  searched: seen.filter((p) => p.startsWith("/search") || p.startsWith("/router/ask")),
}, null, 1));
