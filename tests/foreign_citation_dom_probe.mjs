// #893 - the CLIENT half: what a reader sees while an answer is still streaming.
//
// The final answer goes through answerNodes, which turns 【9†L1-L4】 into a clickable [9].
// The STREAMED preview did not - it was written to the page as raw text, token by token, for
// the whole length of a generation. That is where "...has been confirmed【9†L1-L4】" was read
// off prod: a chunk index and a line range, in a format nothing on the page can resolve.
//
// Both halves are asserted here, because they are one promise: whatever the reader is looking
// at, at any moment, is in the product's own citation format or is not there at all.
//
// Read by tests/selftest_893_foreign_citation_tokens.py; reports JSON on stdout.
import { pathToFileURL } from "node:url";

const [, , jsdomPath, componentsPath] = process.argv;
const { JSDOM } = await import(pathToFileURL(jsdomPath).href);
const dom = new JSDOM("<!doctype html><html><body><div id='root'></div></body></html>",
                      { url: "http://localhost/ask" });
for (const k of ["document", "window", "Node", "HTMLElement", "Event", "CustomEvent"]) {
  Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
}
const { previewText, answerNodes } = await import(pathToFileURL(componentsPath).href);

const SENT = "\ue200cite\ue202turn0file0\ue201";
const CASES = {
  numeric:        "Your notice period is two months after your employment has been confirmed【9†L1-L4】.",
  bare_numeric:   "Two months after confirmation【2】.",
  filename:       "Two months after confirmation【handbook.pdf†L1-L4】.",
  chunk_name:     "Two months after confirmation【employment_terms】.",
  sentinel:       `Two months after confirmation${SENT}.`,
  plain:          "Two months after confirmation [1].",
  // the streaming-specific one: the closing bracket has not arrived yet
  partial:        "Two months after confirmation【9†L1-",
  partial_sent:   "Two months after confirmation\ue200cite\ue202turn0",
};

const out = { preview: {}, rendered: {} };
for (const [name, raw] of Object.entries(CASES)) {
  out.preview[name] = previewText(raw);
  const root = document.createElement("div");
  root.append(answerNodes(raw));
  out.rendered[name] = {
    text: root.textContent,
    citeRefs: [...root.querySelectorAll(".cite-ref")].map((b) => b.textContent),
  };
}

// A streamed answer arrives in pieces, and the preview must never flash a marker shape at any
// prefix of it. This walks EVERY prefix of the reported answer - the assertion a single
// end-state check cannot make.
const full = CASES.numeric;
out.everyPrefixClean = true;
out.firstDirtyPrefix = null;
for (let i = 1; i <= full.length; i++) {
  const p = previewText(full.slice(0, i));
  if (/[【】†\ue200-\ue206]/.test(p)) {
    out.everyPrefixClean = false;
    out.firstDirtyPrefix = { at: i, text: p.slice(-30) };
    break;
  }
}

console.log(JSON.stringify(out, null, 1));
process.exit(0);
