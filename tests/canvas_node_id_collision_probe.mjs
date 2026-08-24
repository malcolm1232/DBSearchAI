// #953 - node ids must be allocated against what EXISTS, not counted.
//
// addNode picked `kind + "-" + (count-of-kind + 1)`. With gdrive-1 and gdrive-2 on the
// canvas, deleting gdrive-1 leaves count=1, so the NEXT add is named "gdrive-2" - a
// DUPLICATE of a live node. Two nodes now share one server store id: compose builds one
// store for both, and deleting either one purges (#947) the data the other still shows.
// This is the id-recycling half of the owner's 260824 incident ("when i add a gdrive node,
// an empty node appears. but when i delete, in my admin, gdrive disappears") - the entry
// door (the row wipe) was #951; the recycling is what let the fresh node inherit the old
// store's identity.
//
// Scenario (one): add gdrive twice -> gdrive-1, gdrive-2; delete gdrive-1; add again.
// The new node must take a FREE id (gdrive-1, the hole), never a live one - and no two
// nodes may ever share an id.
//
// Read by tests/selftest_953_node_id_collision.py; reports JSON on stdout.
import { pathToFileURL } from "node:url";

const [, , jsdomPath, canvasPath] = process.argv;
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

const AUTH = { enabled: true, google_enabled: true, aws_enabled: true, signed_in: true,
               name: "Me", email: "me@example.com", oid: "oid-me", idp: "local",
               has_org: false, linked: [] };
const J = (body, status = 200) =>
  Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
globalThis.fetch = (path, opts = {}) => {
  const p = String(path);
  const m = (opts.method || "GET").toUpperCase();
  if (p.startsWith("/auth/me")) return J(AUTH);
  if (m === "DELETE" && p.startsWith("/router/stores/")) return J({ deleted: true });
  if (m === "PUT" && p.startsWith("/router/manifest")) return J({ saved: true });
  if (p.startsWith("/router/manifest"))
    return J({ manifest: { tenant: "acme", stores: [] } });
  if (p.startsWith("/router/compose")) return J({ stores: [], skipped: [] });
  if (p.startsWith("/admin/documents")) return J([]);
  if (p.startsWith("/admin/principals")) return J({ available: false, principals: [], reason: "n/a" });
  if (p.startsWith("/admin/sources")) return J([]);
  if (p.startsWith("/connectors/sharepoint/status")) return J({ configured: false, connected: [] });
  if (p.startsWith("/config")) return J({ env: [], operator: false });
  return J({});
};

const mod = await import(pathToFileURL(canvasPath).href);
const settle = async (n = 60) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };
const root = document.getElementById("root");
mod.mountCanvas(root);
await settle();

const nodeIds = () => [...document.querySelectorAll(".node .nid")].map((e) => e.textContent.trim());
async function addGdrive() {
  const row = [...document.querySelectorAll("#rail .prov")]
    .find((r) => /Files & Links/.test(r.textContent));
  row.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle(5);
  document.querySelector('#provmenu .svc[data-kind="gdrive"]')
    .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle(10);
}
async function deleteNode(id) {
  const el = [...document.querySelectorAll(".node")]
    .find((e) => e.querySelector(".nid")?.textContent.trim() === id);
  el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle(10);
  document.getElementById("delNode")
    .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle(20);
}

const out = {};
await addGdrive();
await addGdrive();
out.afterTwoAdds = nodeIds().filter((i) => i.startsWith("gdrive"));
await deleteNode("gdrive-1");
out.afterDelete = nodeIds().filter((i) => i.startsWith("gdrive"));
await addGdrive();
const ids = nodeIds().filter((i) => i.startsWith("gdrive"));
out.afterReAdd = ids;
out.hasDuplicate = new Set(ids).size !== ids.length;

console.log(JSON.stringify(out, null, 1));
process.exit(0);
