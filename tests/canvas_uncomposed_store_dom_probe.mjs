// #941 - a source that is SAVED but never COMPOSED must say so, and pressing Test connection
// must not leave it that way.
//
// FOUND ON PROD 260823. The owner re-added their Drive folder, saw a green dot, "1 connected ·
// all permissioned" and "Connection healthy - a record round-tripped", and got no data for
// twenty minutes. The store was in the manifest and absent from the catalog: adding writes the
// row (#818), composing is a separate button, and NOTHING on the node distinguished the two.
//
// THE CONFLATION, in one line: `testConn` and `composeUp` both write `node.status="connected"`.
// One of them means "the endpoint answered a probe" and the other means "this store is in the
// catalog and holds data". A single field cannot carry both, and the probe is the one that
// runs first.
//
// Scenarios:
//   uncomposed  - the store is in the row, compose SKIPS it. Everything on screen must say
//                 draft: no green dot, not counted as connected, and the panel must not
//                 read "Connected · live probe ok".
//   composed    - THE CONTROL. Same store, compose RETURNS it. Everything must read connected
//                 exactly as before, or the fix has simply broken the healthy case.
//   tested      - THE PROD SEQUENCE, and the one that matters: compose skips the store, THEN
//                 the user presses Test connection. Today the probe succeeds and flips the
//                 node to green - so the one surface that was telling the truth (draft) is
//                 overwritten by the one that only knows about reachability.
//   autocompose - press Test connection on a healthy store and watch the wire: a compose must
//                 follow, so the user never has to know the button exists.
//   derived     - an uploads node (#917) is NEVER composed and must never be accused of being
//                 a draft; it isolates the `!node.derived` clause, which nothing else can.
//   probefail   - the probe REFUSES. No compose may follow: submitting a crawl we already know
//                 fails would bury the remediation under a compose error. Isolates the
//                 `v.status !== "failed"` clause.
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

// THE STORE AS PROD HELD IT: in the row, correct link, correct acl. Nothing about this entry
// is wrong - which is exactly why nothing on screen looked wrong.
const STORE = {
  id: "gdrive-1", kind: "gdrive", title: "gdrive-1", business_unit: "unassigned",
  acl: ["oid-owner"], description: "",
  config: { link: "https://drive.google.com/drive/folders/EXAMPLE", description: "" },
};

// Whether /router/compose CLAIMS this store. `uncomposed` is the prod state: the compose that
// ran did not carry it, because the row was written afterwards.
const COMPOSE_RETURNS = !["uncomposed", "tested", "derived", "probefail"].includes(scenario);

const J = (body, status = 200) =>
  Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });

const wire = [];
globalThis.fetch = (path, opts = {}) => {
  const p = String(path);
  const m = (opts.method || "GET").toUpperCase();
  wire.push(m + " " + p.split("?")[0]);
  if (p.startsWith("/auth/me")) return J(AUTH);
  if (m === "PUT" && p.startsWith("/router/manifest")) return J({ saved: true, stores: 1 });
  if (p.startsWith("/router/manifest"))
    return J({ manifest: { tenant: "acme", stores: [STORE], layout: { "gdrive-1": [900, 470] } } });
  if (p.startsWith("/router/compose")) {
    return COMPOSE_RETURNS
      ? J({ stores: [{ store_id: "gdrive-1", freshness: "ingested@2026-08-23T05:06:21Z",
                       warnings: [] }], skipped: [] })
      : J({ stores: [], skipped: [] });
  }
  // The health probe is GENUINELY FINE in every scenario, and that is the trap: the folder is
  // reachable, so a surface reading the probe concludes the store works.
  if (p.startsWith("/router/health")) {
    return scenario === "probefail"
      ? J({ status: "failed", summary: "could not reach the folder",
            remediation: "check the link is shared with anyone who has it",
            stages: [{ name: "probe", ok: false, ms: 120, note: "403" }] })
      : J({ status: "healthy", summary: "Connection healthy - a record round-tripped.",
            stages: [{ name: "probe", ok: true, ms: 698, note: "reachable; schema read" },
                     { name: "exercise", ok: true, ms: 0, note: "content is retrievable" }] });
  }
  if (p.startsWith("/router/demo")) return J({ manifest: { tenant: "demo", stores: [] } });
  // #917 uploads node: present ONLY in the `derived` scenario, where it is the thing that
  // must NOT be called a draft.
  if (p.startsWith("/admin/documents"))
    return J(scenario === "derived"
      ? [{ doc_id: "d1", uri: "upload://notes.txt", title: "notes.txt" }] : []);
  if (p.startsWith("/admin/sources")) return J([]);
  if (p.startsWith("/admin/principals")) return J([]);
  if (p.startsWith("/connectors/sharepoint/status")) return J({ configured: false, connected: [] });
  if (p.startsWith("/config")) return J({ env: [] });
  return J({});
};

const mod = await import(pathToFileURL(canvasPath).href);
const settle = async (n = 80) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };

mod.mountCanvas(document.getElementById("root"));
await settle();

const flat = (e) => (e ? e.textContent.replace(/\s+/g, " ").trim() : "");
const nodeEl = () => [...document.querySelectorAll(".node")]
  .find((n) => flat(n.querySelector(".nid")) === "gdrive-1"
            || /gdrive-1/.test(flat(n.querySelector(".nhead"))));

// Open the panel the way a person does - click the node - so the probe line under test is the
// one they actually read.
const n0 = nodeEl();
if (n0) { n0.dispatchEvent(new window.MouseEvent("click", { bubbles: true })); await settle(20); }

const readState = () => {
  const el = nodeEl();
  const dot = el && el.querySelector(".status");
  return {
    node_present: !!el,
    // The CLASS, not the text: the dot has no text, and a guard reading text would get
    // greener as the surface got less honest.
    dot_class: dot ? dot.getAttribute("class") : null,
    dot_title: dot ? dot.getAttribute("title") : null,
    node_text: flat(el),
    statusbar: flat(document.getElementById("statusbar")),
    probe_line: flat(document.getElementById("probe")),
  };
};

const before = readState();

// ---- the auto-compose half -------------------------------------------------------------
let composesAfterTest = 0;
if (["autocompose", "tested", "probefail"].includes(scenario)) {
  wire.length = 0;
  const btn = document.getElementById("testConn");
  if (btn) btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle(80);
  composesAfterTest = wire.filter((w) => w === "POST /router/compose").length;
}

console.log(JSON.stringify({
  scenario,
  ...before,
  after: ["autocompose", "tested", "probefail"].includes(scenario) ? readState() : null,
  test_button_present: !!document.getElementById("testConn"),
  composes_after_test: composesAfterTest,
  wire_after_test: ["autocompose", "tested", "probefail"].includes(scenario) ? wire : null,
  // The uploads node, read the same way as the store node - by what is on its card.
  upload_node: (() => {
    const el = [...document.querySelectorAll(".node")]
      .find((n) => /upload|Your documents/i.test(flat(n.querySelector(".nhead"))));
    const dot = el && el.querySelector(".status");
    return el ? { text: flat(el), dot_class: dot ? dot.getAttribute("class") : null } : null;
  })(),
}, null, 1));
process.exit(0);
