// #939 / #895 - what a connected node TELLS you about the files it holds.
//
// The gate's failing clause is "connect a node, verify it shows synced + doc count". On prod
// 260823 the node showed neither: a `syncing` pill frozen at compose time, no count, and no
// way to learn whether DBSNotes.txt had landed. The owner's question was literally "still no
// data, but idk if its ingesting or not".
//
// Scenarios:
//   ingested   - the crawl finished. The node must show a DOC COUNT and must NOT still be
//                showing the compose-time `syncing`, and the panel must name the files.
//   syncing    - the crawl is still running. Honest: `syncing` stays, and no count is invented
//                (a count taken mid-crawl is a number that will be wrong in a minute).
//   unreadable - #725: a file was listed and could not be fetched. It must be SAID, because a
//                list that silently omits it is a new way to mislead.
//   unknown    - the store cannot answer (a SQL store, or a listing that failed). Nothing is
//                claimed: no count, no empty-file-list, no "0 documents".
import { pathToFileURL } from "node:url";

const [, , jsdomPath, canvasPath, scenario] = process.argv;
const { JSDOM } = await import(pathToFileURL(jsdomPath).href);

const dom = new JSDOM(
  "<!doctype html><html><head></head><body><div id='root'></div></body></html>",
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
window.HTMLElement.prototype.setPointerCapture = () => {};
window.HTMLElement.prototype.releasePointerCapture = () => {};
window.HTMLElement.prototype.scrollIntoView = function scrollIntoView() {};
const _ls = new Map();
const fakeStorage = {
  getItem: (k) => (_ls.has(k) ? _ls.get(k) : null),
  setItem: (k, v) => _ls.set(k, String(v)),
  removeItem: (k) => _ls.delete(k),
  clear: () => _ls.clear(),
};
Object.defineProperty(window, "localStorage", { value: fakeStorage, configurable: true });
Object.defineProperty(globalThis, "localStorage", { value: fakeStorage, configurable: true });

const AUTH = { enabled: true, google_enabled: true, aws_enabled: true, signed_in: true,
               name: "Owner", email: "owner@example.com", oid: "oid-owner", linked: [] };

const STORE = {
  id: "gdrive-1", kind: "gdrive", title: "gdrive-1", business_unit: "unassigned",
  acl: ["oid-owner"], description: "",
  config: { link: "https://drive.google.com/drive/folders/EXAMPLE", description: "" },
};

// THE COMPOSE RESPONSE IS DELIBERATELY STALE IN EVERY SCENARIO. This is the defect's shape:
// compose happens while the crawl is queued, so its freshness says `syncing` and stays on the
// node forever. If the fixture reported `ingested@` here, nothing would be being tested.
const COMPOSE = { stores: [{ store_id: "gdrive-1", freshness: "syncing@2026-08-23T08:50:00Z",
                             warnings: [] }], skipped: [] };

const DOCUMENTS = {
  ingested: { store_id: "gdrive-1", known: true, doc_count: 2, unreadable: 0,
              freshness: "ingested@2026-08-23T08:58:31Z",
              documents: [{ doc: "d1", title: "DBSNotes.txt", uri: "gdrive://d1" },
                          { doc: "d2", title: "handbook.pdf", uri: "gdrive://d2" }] },
  syncing: { store_id: "gdrive-1", known: true, doc_count: 0, unreadable: 0,
             freshness: "syncing@2026-08-23T08:50:00Z", documents: [] },
  unreadable: { store_id: "gdrive-1", known: true, doc_count: 1, unreadable: 2,
                freshness: "ingested@2026-08-23T08:58:31Z",
                documents: [{ doc: "d1", title: "DBSNotes.txt", uri: "gdrive://d1" }] },
  unknown: { store_id: "gdrive-1", known: false, doc_count: null, unreadable: 0,
             freshness: "", documents: [] },
}[scenario] || null;

const J = (body, status = 200) =>
  Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });

globalThis.fetch = (path, opts = {}) => {
  const p = String(path);
  const m = (opts.method || "GET").toUpperCase();
  if (p.startsWith("/auth/me")) return J(AUTH);
  if (m === "PUT" && p.startsWith("/router/manifest")) return J({ saved: true, stores: 1 });
  if (p.startsWith("/router/manifest"))
    return J({ manifest: { tenant: "acme", stores: [STORE], layout: { "gdrive-1": [900, 470] } } });
  if (p.startsWith("/router/compose")) return J(COMPOSE);
  if (/\/router\/stores\/[^/]+\/documents/.test(p)) {
    return DOCUMENTS ? J(DOCUMENTS) : J({ detail: "no such store" }, 404);
  }
  if (p.startsWith("/router/health"))
    return J({ status: "healthy", summary: "ok", stages: [] });
  if (p.startsWith("/router/demo")) return J({ manifest: { tenant: "demo", stores: [] } });
  if (p.startsWith("/admin/documents")) return J([]);
  if (p.startsWith("/admin/sources")) return J([]);
  if (p.startsWith("/admin/principals")) return J([]);
  if (p.startsWith("/connectors/sharepoint/status")) return J({ configured: false, connected: [] });
  if (p.startsWith("/config")) return J({ env: [] });
  return J({});
};

const mod = await import(pathToFileURL(canvasPath).href);
const settle = async (n = 100) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };

mod.mountCanvas(document.getElementById("root"));
await settle();

const flat = (e) => (e ? e.textContent.replace(/\s+/g, " ").trim() : "");
const nodeEl = () => [...document.querySelectorAll(".node")]
  .find((n) => /gdrive-1/.test(flat(n)));

// Open the panel the way a person does.
const n0 = nodeEl();
if (n0) { n0.dispatchEvent(new window.MouseEvent("click", { bubbles: true })); await settle(40); }

const panel = document.getElementById("panel") || document.querySelector(".panel");
const docsSection = document.getElementById("storeDocs");

console.log(JSON.stringify({
  scenario,
  node_text: flat(nodeEl()),
  // Every pill on the card, so a test can assert on what is SHOWN rather than on a class the
  // fix happens to add.
  node_pills: nodeEl() ? [...nodeEl().querySelectorAll(".pill")].map(flat) : [],
  panel_text: flat(panel),
  docs_section_present: !!docsSection,
  docs_section_text: flat(docsSection),
  doc_rows: docsSection ? [...docsSection.querySelectorAll(".docrow")].map(flat) : [],
  // #939 parity: the rows must be the UPLOADS panel's shell, not a parallel one that merely
  // resembles it. Asserted on the CLASSES the two share and on the row's action, because
  // "looks the same" is not a thing a DOM probe can see - what it can see is same-shell.
  row_classes: docsSection
    ? [...docsSection.querySelectorAll(".updoc-row")].map((r) => r.className) : [],
  row_titles: docsSection
    ? [...docsSection.querySelectorAll(".updoc-title")].map(flat) : [],
  row_actions: docsSection
    ? [...docsSection.querySelectorAll("a.btn, button.btn")].map((b) => ({
        text: flat(b), href: b.getAttribute("href") || "", target: b.getAttribute("target") || "" }))
    : [],
  list_is_updoc_list: !!(docsSection && docsSection.classList.contains("updoc-list")),
  // The uploads panel is the reference. Read it too, so the parity claim is measured against
  // the real thing rather than against a remembered description of it.
  uploads_row_classes: [...document.querySelectorAll(".updoc-row")].map((r) => r.className),
}, null, 1));
process.exit(0);
