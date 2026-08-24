// #951 - a canvas mount that is TORN DOWN BEFORE its manifest arrives must never write the row.
//
// THE DEFECT, off prod logs (account acct_e438..., 260824): the user's gdrive + sharepoint_link
// nodes vanished from Connectors while Admin still listed their documents. The workspace ROW had
// been overwritten with stores:[]; the warm in-process catalog still held the stores, which is
// why the documents survived the nodes.
//
// The window is narrow and entirely in the client:
//   canvas.js wire-up     sets state=[] SYNCHRONOUSLY
//   bootCanvas            sets booting=false as soon as AUTH resolves
//   loadLiveUser          THEN does an async GET /router/manifest for the real state
//   unmountCanvas         flushes a row save FIRST, before alive=false (#818, deliberate:
//                         "the keepalive PUT rides out the teardown")
//   pushRowSave           has no alive gate, no booting gate, and lastRowSave is still null
//                         (markRowClean only runs after hydration), so the dirty-check cannot
//                         suppress it either
// => unmount inside [auth resolved .. manifest hydrated] PUTs {stores: []} over a good row.
//
// #731 ("stores:[] is AUTHORITATIVE empty") and #818 ("the save must survive an unmount") are
// each correct alone. Together they let a mount that has loaded NOTHING claim authority.
//
// Scenarios:
//   unmount_before_hydrate - hold /router/manifest open, unmount, THEN resolve it. No PUT may
//                            carry stores:[] - the mount never learned what the row held.
//   normal_delete_all      - the CONTROL that stops the fix being over-broad: a hydrated mount
//                            whose user really deleted every node must STILL be able to persist
//                            an empty row, which is #731's whole contract.
//
// Read by tests/selftest_951_unmount_before_hydrate.py; reports JSON on stdout.
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
const _ls = new Map();
const fakeStorage = {
  getItem: (k) => (_ls.has(k) ? _ls.get(k) : null),
  setItem: (k, v) => _ls.set(k, String(v)),
  removeItem: (k) => _ls.delete(k), clear: () => _ls.clear(),
};
Object.defineProperty(window, "localStorage", { value: fakeStorage, configurable: true });
Object.defineProperty(globalThis, "localStorage", { value: fakeStorage, configurable: true });

// ---- the stored row: TWO real connector stores, exactly the owner's shape ------------------
let serverStores = [
  { id: "gdrive-1", kind: "gdrive", business_unit: "", acl: ["oid-me"], mode: "index",
    config: { link: "https://drive.google.com/drive/folders/abc" } },
  { id: "sharepoint_link-1", kind: "sharepoint_link", business_unit: "", acl: ["oid-me"],
    mode: "index", config: { link: "https://acme.sharepoint.com/:f:/g/abc" } },
];
const AUTH = { enabled: true, google_enabled: true, aws_enabled: true, signed_in: true,
               name: "Me", email: "me@example.com", oid: "oid-me", idp: "local",
               has_org: false, linked: [] };

const rowPuts = [];            // every PUT /router/manifest body, in order
let holdManifest = null;       // when set, GET /router/manifest parks on this promise

const J = (body, status = 200) =>
  Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });

globalThis.fetch = (path, opts = {}) => {
  const p = String(path);
  const m = (opts.method || "GET").toUpperCase();
  if (p.startsWith("/auth/me")) return J(AUTH);
  if (m === "PUT" && p.startsWith("/router/manifest")) {
    let body = {};
    try { body = JSON.parse(opts.body || "{}"); } catch { /* ignore */ }
    rowPuts.push({ manifest: body.manifest || {}, keepalive: opts.keepalive === true });
    return J({ saved: true, stores: ((body.manifest || {}).stores || []).length });
  }
  if (p.startsWith("/router/manifest")) {
    const serve = () => ({ ok: true, status: 200,
      json: () => Promise.resolve({ manifest: { tenant: "acme", stores: serverStores } }) });
    // the whole point of the probe: keep the hydrate in flight while the surface is torn down
    if (holdManifest) return holdManifest.then(serve);
    return Promise.resolve(serve());
  }
  if (p.startsWith("/router/compose")) return J({ stores: [], skipped: [] });
  if (p.startsWith("/admin/documents")) return J([]);
  if (p.startsWith("/admin/principals")) return J({ available: false, principals: [], reason: "n/a" });
  if (p.startsWith("/admin/sources")) return J([]);
  if (p.startsWith("/connectors/sharepoint/status")) return J({ configured: false, connected: [] });
  if (p.startsWith("/config")) return J({ env: [], operator: false });
  if (p.startsWith("/billing/status")) return J({});
  return J({});
};

const mod = await import(pathToFileURL(canvasPath).href);
const settle = async (n = 60) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };
const root = document.getElementById("root");

const out = { scenario };
const emptyPuts = () => rowPuts.filter((p) => (p.manifest.stores || []).length === 0);

if (scenario === "unmount_before_hydrate") {
  // park the hydrate, mount, let auth resolve (booting=false), then tear the surface down
  let release;
  holdManifest = new Promise((r) => { release = r; });
  const teardown = mod.mountCanvas(root);
  await settle(40);                       // auth resolves; the manifest GET is still in flight
  out.putsBeforeUnmount = rowPuts.length;
  teardown();                             // <- the unmount that flushes the row
  await settle(20);
  release();                              // the hydrate finally lands, on a dead surface
  await settle(60);
  out.totalPuts = rowPuts.length;
  out.emptyPuts = emptyPuts().length;
  out.putBodies = rowPuts.map((p) => ({ n: (p.manifest.stores || []).length, keepalive: p.keepalive }));
} else if (scenario === "normal_delete_all") {
  // THE CONTROL (#731): a mount that DID hydrate, whose user deletes every node, must still
  // be able to write an empty row. A fix that simply refuses empty writes breaks this.
  const teardown = mod.mountCanvas(root);
  await settle(80);                       // full hydrate
  out.hydratedNodes = document.querySelectorAll(".node").length;
  rowPuts.length = 0;                     // only measure what the deletes write
  for (const id of ["gdrive-1", "sharepoint_link-1"]) {
    const el = [...document.querySelectorAll(".node")]
      .find((e) => e.querySelector(".nid")?.textContent.trim() === id);
    if (!el) continue;
    el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await settle(10);
    const del = document.getElementById("delNode");
    if (del) { del.dispatchEvent(new window.MouseEvent("click", { bubbles: true })); await settle(20); }
  }
  document.dispatchEvent(new window.KeyboardEvent("keydown",
    { key: "s", ctrlKey: true, bubbles: true, cancelable: true }));
  await settle(40);
  teardown();
  await settle(20);
  out.nodesAfter = document.querySelectorAll(".node").length;
  out.emptyPuts = emptyPuts().length;
  out.totalPuts = rowPuts.length;
} else {
  throw new Error(`unknown scenario ${scenario}`);
}

console.log(JSON.stringify(out, null, 1));
process.exit(0);
