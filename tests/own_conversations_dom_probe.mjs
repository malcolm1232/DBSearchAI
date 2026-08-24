// Drives the REAL Ask surface in a real DOM (jsdom) and reports what an OWNER would find in
// the "Your conversations" list, as JSON on stdout. Used by
// tests/selftest_602_owner_reopens_conversation.py.
//
// WHY THIS EXISTS. Every other assertion available to a python selftest is a string search over
// a file or a served asset, and this branch has already shipped four tests that were green
// while the page in question was never rendered to anybody. A string in a module is not a row
// on a screen. This mounts the surface the router actually mounts, clicks a row the way a
// person clicks it, and reads the resulting thread, the Share button, and the URL the share
// modal then posts to - so "the reopened thread is shareable" is answered by the request that
// actually goes out, not by a name in the source.
//
// It is NOT a browser: no layout, no paint, no CSS. A real-browser pass is still owed.
import { pathToFileURL } from "node:url";

const [, , jsdomPath, askPath] = process.argv;
const { JSDOM } = await import(pathToFileURL(jsdomPath).href);

const dom = new JSDOM("<!doctype html><html><body><div id='root'></div></body></html>",
                      { url: "http://localhost/ask" });
const { window } = dom;
for (const k of ["document", "window", "location", "HTMLElement", "Node", "Event",
                 "CustomEvent", "getComputedStyle"]) {
  Object.defineProperty(globalThis, k, { value: window[k], configurable: true, writable: true });
}
// jsdom implements no scrolling at all, and `submit()` calls it on the answer block.
window.Element.prototype.scrollIntoView = function () {};
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async () => {} } },
  configurable: true, writable: true,
});

// Two threads, exactly the shape the card is about: the newest first, and an older one with
// more than one turn in it.
// The third row is FIX ROUND 1's case: a thread somebody shared WITH her that she has replied
// in, so the store lists it under her own oid while `own: false` says whose it really is.
const MINE = { conversations: [
  { conv_id: "c-602-new", first_question: "how much leave carries over in Lisbon?",
    turns: 1, last_asked_at: "2026-08-10T11:00:00Z", own: true, grantor_oid: null },
  { conv_id: "c-602-old", first_question: "how many weeks of severance in Hamburg?",
    turns: 2, last_asked_at: "2026-08-09T09:00:00Z", own: true, grantor_oid: null },
  { conv_id: "c-602-recv", first_question: "RECVQ and for part timers?",
    turns: 1, last_asked_at: "2026-08-08T09:00:00Z", own: false, grantor_oid: "acct_bob" },
  { conv_id: "c-861-gap", first_question: "GAPQ what is the total amount by region?",
    turns: 1, last_asked_at: "2026-08-07T09:00:00Z", own: true, grantor_oid: null },
] };
const TRANSCRIPTS = {
  "c-602-old": { own: true, turns: [
    { seq: 0, question: "how many weeks of severance in Hamburg?",
      answer: "It pays out over 62 weeks of salary.", own: true },
    { seq: 1, question: "and for part timers?", answer: "Pro rata.", own: true },
  ] },
  "c-602-new": { own: true, turns: [
    { seq: 0, question: "NEWTHREAD how much leave carries over in Lisbon?",
      answer: "It carries over 26 days.", own: true },
  ] },
  // #861: A ROUTED turn whose stored citations INTERLEAVE proof rows with document rows.
  // The answer's markers are [1] and [4], written against the full stored list; the reopened
  // rail can only render the two proof rows, and the question is what NUMBERS they carry.
  // Before the fix they were renumbered [1] and [2], so [4] opened the row for BERLIN while
  // the answer said London - #855's "a row that has moved is a lie", one surface out.
  "c-861-gap": { own: true, corpus: null, turns: [
    { seq: 0, question: "what is the total amount by region?",
      answer: "- London: 92 [1]  - Berlin: 78 [4]", own: true,
      citations: [
        { store_id: "bq", kind: "row", origin: "BigQuery · dbsearch_e2e · table sales",
          snippet: "region=London, total_amount=92", sql: "SELECT 1", rerun_token: "tok-1" },
        { store_id: "docs", doc: "upload-hr-leave", title: "HR leave policy",
          snippet: "26 days" },
        { store_id: "docs", doc: "upload-group-policy", title: "Group policy",
          snippet: "expenses" },
        { store_id: "bq", kind: "row", origin: "BigQuery · dbsearch_e2e · table sales",
          snippet: "region=Berlin, total_amount=78", sql: "SELECT 2", rerun_token: "tok-4" },
      ] },
  ] },
  // A received thread: the grantor's half (own: false) plus her own reply.
  "c-602-recv": { own: false, turns: [
    { seq: 0, question: "GRANTORQ what is the Hamburg severance?",
      answer: "It pays out over 62 weeks.", own: false },
    { seq: 0, question: "RECVQ and for part timers?", answer: "Pro rata.", own: true },
  ] },
};
const CONV_DOCS = { documents: [{ id: "d-hamburg", title: "Severance policy", shareable: true }],
                    turns: 2 };

const fetched = [];          // every URL, in order
const posted = [];           // {url, body} for every POST /shares
let mineCalls = 0;

globalThis.fetch = async (url, opts = {}) => {
  fetched.push(url);
  const j = (body, status = 200) => ({
    ok: status < 400, status,
    json: async () => body, text: async () => JSON.stringify(body),
  });
  if (url.startsWith("/ask/suggestions")) {
    return j({ known: true, indexed: true, authorized_docs: 1, examples: [] });
  }
  if (url === "/conversations/mine") { mineCalls += 1; return j(MINE); }
  if (url.startsWith("/conversations/shared-with-me")) return j({ shares: [] });
  if (url.endsWith("/transcript")) {
    const id = url.split("/")[2];
    return TRANSCRIPTS[id] ? j(TRANSCRIPTS[id]) : j({ detail: "no such conversation" }, 404);
  }
  if (url.endsWith("/shareable")) return j(CONV_DOCS);
  if (url.endsWith("/shares") && (opts.method || "GET") === "GET") return j({ shares: [] });
  if (url.endsWith("/shares") && opts.method === "POST") {
    const body = JSON.parse(opts.body);
    posted.push({ url, body });
    const out = { share_id: `s-${posted.length}`, audience: body.audience || "people",
                  documents: 1, turns_withheld: 0, created_at: "2026-08-10T10:00:00Z",
                  expires_at: null, opens: 0, live: true,
                  grantee_oid: body.audience === "link" ? "link:s" : "acct_new" };
    if (body.audience === "link") out.url = "/c/TOKEN-abc123";
    return j(out);
  }
  if (url === "/chat/stream") {
    // The SSE shape chatStream() reads: one token, then a done event. The corpus is authorized
    // for an ordinary thread, and EMPTY for the received one - which is what a revoke looks
    // like on the next question (#600): the grant is gone, the denominator is zero, and the
    // model's output must be replaced rather than shown.
    const revoked = JSON.parse(opts.body || "{}").conv_id === "c-602-recv";
    const corpus = revoked ? '{"authorized_docs":0,"indexed":1}' : '{"authorized_docs":1,"indexed":1}';
    const chunks = [
      'data: {"type":"token","text":"It pays out over 62 weeks."}\n\n',
      'data: {"type":"done","answer":"It pays out over 62 weeks.","citations":[],'
      + `"retrieved_docs":["d-hamburg"],"corpus":${corpus}}\n\n`,
    ].map((s) => new TextEncoder().encode(s));
    let i = 0;
    return { ok: true, status: 200,
             body: { getReader: () => ({
               read: async () => (i < chunks.length
                 ? { value: chunks[i++], done: false } : { value: undefined, done: true }) }) } };
  }
  return j({}, 404);
};

const tick = () => new Promise((r) => setTimeout(r, 0));
const ticks = async (n = 4) => { for (let i = 0; i < n; i++) await tick(); };
const { mountAsk } = await import(pathToFileURL(askPath).href);

const root = window.document.getElementById("root");
mountAsk(root);
await ticks();

const doc = window.document;
const thread = doc.getElementById("thread");
const shareBtn = doc.getElementById("share-conversation");
const backdrop = doc.getElementById("share-modal");
const listBox = doc.getElementById("your-conversations");
// #631: rows are .rail-thread buttons in the rail slot now, not .source-card divs in the
// reading column. The probe follows the surface; what it asserts is unchanged.
const rowsOf = () => [...(listBox ? listBox.querySelectorAll(".rail-thread") : [])];

const report = {
  // #861: DERIVED, not hardcoded downstream. The selftest asserted "one row per
  // conversation" as `len(rows) == 3`, so adding a fixture conversation broke a test whose
  // claim was still perfectly true. The fixture is the only thing that knows its own size.
  mine_count: MINE.conversations.length,
  list_present: !!listBox && rowsOf().length > 0,
  list_heading: listBox ? (listBox.querySelector(".navrail-group")?.textContent || "") : "",
  rows: rowsOf().map((r) => ({
    title: r.querySelector(".rail-thread-title")?.textContent || "",
    // The visible meta is the count alone (248px); the full sentence lives in the tooltip
    // and the accessible name, which is where this now reads it from.
    meta: r.getAttribute("aria-label") || "",
    count: r.querySelector(".rail-thread-meta")?.textContent || "",
    text: r.textContent.trim(),
  })),
};

// ---- the click: reopen the OLDER thread -----------------------------------------------------
const oldRow = rowsOf().find((r) => r.textContent.includes("severance"));
oldRow.click();
await ticks();
report.clicked_fetched = fetched.filter((u) => u.endsWith("/transcript")).pop() || "";
report.thread_after_click = thread.textContent;
report.share_visible_after_click = shareBtn.style.display !== "none";

// ---- and the share modal must belong to the thread that was reopened -----------------------
shareBtn.click();
await ticks();
const panel = backdrop.querySelector(".share-modal");
report.share_modal_present_after_click = !!panel;
const scopeUrl = fetched.filter((u) => u.endsWith("/shareable")).pop() || "";
report.share_modal_conv_id = scopeUrl.split("/")[2] || "";
panel.querySelector(".share-input").value = "carol@x.com";
panel.querySelector(".share-add").click();
await ticks();
report.shared_posted_to = posted.length ? posted[posted.length - 1].url : "";
panel.querySelector(".share-modal-close").click();
await tick();

// ---- the guarded teardown: a row is a THIRD navigation ---------------------------------------
//
// The two that already existed ("New conversation" and opening a thread somebody shared with
// you) both used to destroy an uncopied one-time link with no warning, and the token is
// returned by the API exactly once. This drives the same sequence through a conversation row.
shareBtn.style.display = "";
shareBtn.click();
await ticks();
const p2 = backdrop.querySelector(".share-modal");
const linkRadio = [...p2.querySelectorAll('input[type="radio"]')].find((r) => r.value === "link");
linkRadio.checked = true;
linkRadio.dispatchEvent(new window.Event("change"));
p2.querySelector(".share-add").click();
await ticks();
if (!p2.querySelector(".share-link-url")) throw new Error("no uncopied link view to guard");

const newRow = rowsOf().find((r) => r.textContent.includes("leave carries over"));
newRow.click();
await ticks();
report.guard_modal_closed_on_first_click = backdrop.innerHTML === "";
report.guard_navigated_on_first_click = thread.textContent.includes("NEWTHREAD");
report.guard_note = p2.querySelector(".share-guard-note")?.textContent || "";
newRow.click();
await ticks();
report.guard_modal_closed_on_second_click = backdrop.innerHTML === "";
report.guard_navigated_on_second_click = thread.textContent.includes("NEWTHREAD");

// ---- a brand-new thread gets a door too ------------------------------------------------------
const before = mineCalls;
const input = doc.getElementById("ask-input");
const ask = async (q) => {
  input.value = q;
  // #632: the composer is .chat-composer since Ask and Chat merged. Still a real
  // <form>, so submitting it is still what a user pressing Enter does.
  doc.querySelector(".chat-composer").dispatchEvent(
    new window.Event("submit", { bubbles: true, cancelable: true }));
  await ticks(8);
};
await ask("what is the Hamburg severance?");
report.answered = thread.textContent.includes("62 weeks");
report.list_reread_after_answer = mineCalls > before;

// ---- FIX ROUND 1: a RECEIVED thread reopened from this list is still a share ------------------
//
// She replied in a thread somebody shared with her, so the store lists it under her own oid and
// the row says `own: false`. Reopening it must set `sharedConv`, because that is what arms
// #600's revoke detection on her NEXT question. With it unset, a grantee whose share had been
// revoked was told "This conversation is no longer here. Start a new one" - owner-data words
// aimed at somebody whose SHARE ended, telling her she had lost data that was never hers.
{
  const recvRow = rowsOf().find((r) => r.textContent.includes("RECVQ"));
  report.received_row_present = !!recvRow;
  // #631: "shared with you" rides on the accessible name now, not a visible meta line -
  // the rail has room for the question and a count, and the fact that a thread is not hers
  // is exactly the sort of detail that must not be the thing an ellipsis eats.
  report.received_row_meta = recvRow ? (recvRow.getAttribute("aria-label") || "") : "";
  recvRow.click();
  await ticks();
  report.received_thread_text = thread.textContent;
  // The grantor's half must be labelled as his, never with her own name.
  report.received_labels_the_grantor = thread.textContent.includes("acct_bob");
  // ...and now the revoke, discovered on the next question rather than on load.
  await ask("and what about notice periods?");
  report.received_after_revoke_text = thread.textContent;
  report.received_says_share_ended = thread.textContent.includes("no longer active");
  report.received_says_owner_data_gone = thread.textContent.includes("no longer here");
}

// ---- #861: a reopened routed turn keeps each surviving row's OWN number ----------------------
//
// The markers are written against the full stored citation list. The rail can only render the
// PROOF rows, so the document rows between them disappear - and the number a survivor carries
// must still be its position in the list the answer was numbered against, never its position
// among the survivors. Renumbering makes [4] open the row for Berlin under an answer that says
// London: a dangling marker looks broken, a moved one looks sourced.
{
  const gapRow = rowsOf().find((r) => r.textContent.includes("GAPQ"));
  report.gap_row_present = !!gapRow;
  gapRow.click();
  await ticks(6);
  const nums = [...doc.querySelectorAll("#thread .snum")].map((n) => n.textContent.trim());
  report.gap_source_numbers = nums;
  report.gap_answer = thread.textContent.includes("[4]");
  // The row carrying [4] must be the one whose snippet says Berlin - the number and the
  // content have to agree, which is the whole claim. Reading the rendered card, not the input.
  const cards = [...doc.querySelectorAll("#thread .src")];
  report.gap_rows = cards.map((c) => ({
    num: (c.querySelector(".snum") || {}).textContent || "",
    text: c.textContent.replace(/\s+/g, " ").trim().slice(0, 60),
  }));
}

process.stdout.write(JSON.stringify(report, null, 1));
