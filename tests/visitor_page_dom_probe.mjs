// Drives the REAL link-visitor page in a real DOM (jsdom) and reports what a visitor would
// find, as JSON on stdout. Used by tests/selftest_605_visitor_surface.py.
//
// WHY THIS EXISTS. Everything a python selftest can assert on its own is a string search over
// bytes. That is a large step up from grepping a file - it is the step this whole task exists
// to take - but it still cannot answer "is the grantor's half read only", "does the visitor's
// own answer land BELOW it", or "what does a visitor see when the link's question cap fires".
// Those are questions about a DOM after a fetch resolved, so they are answered by building
// one.
//
// THE DOCUMENT IS THE SERVED ONE. The python side writes the exact bytes `GET /c/{token}`
// returned into a file and passes the path here, so this probe cannot pass against a page the
// server does not actually hand out.
//
// It is NOT a browser: no layout, no paint, no CSS. A real-browser pass is still owed.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [, , jsdomPath, htmlPath, visitorJsPath, scenario] = process.argv;
const { JSDOM } = await import(pathToFileURL(jsdomPath).href);

const TOKEN = "TOKEN-probe";
const dom = new JSDOM(readFileSync(htmlPath, "utf8"),
                      { url: `http://localhost/c/${TOKEN}` });
const { window } = dom;
for (const k of ["document", "window", "location", "HTMLElement", "Node", "Event",
                 "CustomEvent", "FormData", "getComputedStyle", "localStorage"]) {
  Object.defineProperty(globalThis, k, { value: window[k], configurable: true, writable: true });
}

// The grantor's shared prefix (own:false) then this visitor's own fork (own:true) - exactly
// the shape `/c/{token}/transcript` returns, `disclosure: true` and all.
const TRANSCRIPT = {
  own: false,
  disclosure: true,
  turns: [
    { seq: 0, question: "how much leave carries over?",
      answer: "It carries over 26 days of unused leave.", own: false },
    { seq: 0, question: "does that apply to contractors?",
      answer: "The policy does not say.", own: true },
  ],
};

const asked = [];

function reply(body, status = 200, headers = {}) {
  return {
    ok: status < 400, status,
    headers: { get: (h) => headers[h.toLowerCase()] ?? null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

globalThis.fetch = async (url, opts = {}) => {
  if (url.endsWith("/transcript")) {
    if (scenario === "dead") return reply({ detail: "not found" }, 404);
    return reply(TRANSCRIPT);
  }
  if (url.endsWith("/chat")) {
    asked.push({ url, body: JSON.parse(opts.body || "{}") });
    if (scenario === "capped") {
      // The real refusal: 429 with Retry-After, exactly as link_access.py raises it.
      return reply({ detail: "too many questions on this link, try again shortly" }, 429,
                   { "retry-after": "1800" });
    }
    if (scenario === "revoked") return reply({ detail: "not found" }, 404);
    return reply({
      answer: "Contractors are out of scope.",
      citations: [{ doc_id: "doc-a", title: "Leave policy", uri: "upload://doc-a.txt",
                    snippet: "carries over 26 days" }],
      retrieved_docs: ["doc-a"],
      corpus: { indexed: 4, authorized_docs: 1 },
      conv_id: "c-probe",
    });
  }
  return reply({ detail: "the visitor page reached a route it must never touch: " + url }, 500);
};

const tick = () => new Promise((r) => setTimeout(r, 0));

await import(pathToFileURL(visitorJsPath).href);
await tick(); await tick(); await tick();

const doc = window.document;
const q = (s) => doc.querySelector(s);
const thread = q("#visitor-thread");
const form = q("#visitor-form");
const input = q("#visitor-input");
const disclosure = q(".link-disclosure");

const report = { scenario };

// Where the disclosure is RELATIVE TO THE INPUT, in the live DOM. DOCUMENT_POSITION_FOLLOWING
// means the input comes after the disclosure in document order - which is what "directly
// above the input" means once there is no layout to measure.
report.disclosure_text = disclosure ? disclosure.textContent.trim() : null;
report.disclosure_precedes_input = !!(disclosure && input &&
  (disclosure.compareDocumentPosition(input) & window.Node.DOCUMENT_POSITION_FOLLOWING) !== 0);
report.disclosure_after_thread = !!(disclosure && thread &&
  (thread.compareDocumentPosition(disclosure) & window.Node.DOCUMENT_POSITION_FOLLOWING) !== 0);
report.disclosure_is_visible = !!(disclosure && !disclosure.hidden &&
  disclosure.getAttribute("hidden") === null &&
  disclosure.getAttribute("aria-hidden") === null);

// Every control on the whole page. A visitor must be offered a question box and an Ask
// button, and NOTHING else: no model select, no rail link, no sign-out, no new-conversation.
const controls = [...doc.querySelectorAll("input, select, textarea, button, a[href]")];
report.controls = controls.map((n) => ({
  tag: n.tagName.toLowerCase(), type: n.getAttribute("type") || "",
  id: n.id || "", text: (n.textContent || "").trim().slice(0, 30),
  href: n.getAttribute("href") || "",
}));

// The transcript, and which half each turn belongs to.
const turns = [...thread.querySelectorAll("section")];
report.turns = turns.map((s) => ({
  own: s.getAttribute("data-own"),
  shared_marker: !!s.querySelector(".doc-audience-shared"),
  // A read-only half means there is nothing inside it to press. Counted, not grepped.
  controls: s.querySelectorAll("input, select, textarea, button, a[href]").length,
  text: (s.textContent || "").replace(/\s+/g, " ").trim().slice(0, 90),
}));
report.thread_text = (thread.textContent || "").replace(/\s+/g, " ").trim();
report.form_hidden_on_load = !!form.hidden;

if (scenario === "ask" || scenario === "capped" || scenario === "revoked") {
  input.value = "does that apply to contractors?";
  form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await tick(); await tick(); await tick();
  const after = [...thread.querySelectorAll("section, .empty")];
  report.asked = asked;
  report.after_text = (thread.textContent || "").replace(/\s+/g, " ").trim();
  report.last_block_text = after.length
    ? (after[after.length - 1].textContent || "").replace(/\s+/g, " ").trim() : "";
  report.form_hidden_after = !!form.hidden;
  // #629: the citation apparatus is a pill that OPENS a panel, so the source title is no
  // longer sitting in the answer block. Report both halves: that the visitor is offered the
  // affordance at all, and what she actually gets when she uses it.
  {
    const last = after.length ? after[after.length - 1] : null;
    const pill = last ? last.querySelector(".sources-pill") : null;
    report.sources_pill_text = pill ? (pill.textContent || "").trim() : "";
    if (pill) {
      pill.dispatchEvent(new window.Event("click", { bubbles: true }));
      await tick(); await tick();
    }
    const panel = window.document.querySelector(".sources-panel");
    report.sources_panel_open = !!(panel && !panel.hidden);
    report.sources_panel_text = panel
      ? (panel.textContent || "").replace(/\s+/g, " ").trim() : "";
  }
  // The visitor's own answer must land BELOW the grantor's half, not above it and not
  // interleaved: the shared prefix is the thing that was handed over, the fork is what this
  // visitor added to it.
  const sections = [...thread.querySelectorAll("section")];
  report.own_flags_in_order = sections.map((s) => s.getAttribute("data-own"));
}

process.stdout.write(JSON.stringify(report, null, 1));
