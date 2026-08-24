// #731 - drives the REAL canvas delete paths in a real DOM (jsdom), then does what no
// existing probe ever did: UNMOUNTS and REMOUNTS the surface, because the defect only
// exists across a remount. Deleting a node looked immediate, but the stored manifest
// resurrected it on the next page load - and boot's composeUp() then re-committed the
// resurrected set. A probe that never remounts is structurally blind to all of that.
// Read by tests/selftest_731_canvas_delete_dom.py; reports JSON on stdout.
//
// The wire stub is a tiny SERVER: `serverStores` is the stored manifest's stores array,
// DELETE /router/stores/{id} edits it (or refuses, per scenario), /router/manifest serves
// it - as an ARRAY even when empty, which is the #731 hydration contract ("empty is a
// state"). So the remount sees exactly what a real reload would see.
//
// Scenarios:
//   panel_delete - select the node, click the panel's #delNode; remount; stays gone
//   menu_delete  - right-click the node, click the context menu's Delete; same contract
//   delete_all   - delete BOTH nodes; remount; the canvas stays EMPTY (no localStorage
//                  shadow, no demo resurrect) - the delete-all latent bug
//   refusal      - the server 500s the DELETE; the node must COME BACK (never show a
//                  deletion the server refused) with a toast
//   undo         - after a delete, the undo toast's button re-inserts the node and
//                  re-composes it (the ordinary re-add path)
//
// NOT a browser: no layout, no paint, no real navigation. The keepalive clause is asserted
// as the option PASSED to fetch (the real navigate-away race needs the prod pass).
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
// Hand-rolled localStorage (the rail_slot_dom_probe pattern): without --localstorage-file
// jsdom's own localStorage is INERT - writes vanish, reads answer null - and this probe's
// whole delete_all/dev_empty_restore discrimination lives in what localStorage holds at
// remount. A fake that actually stores is load-bearing here, not a convenience.
const _ls = new Map();
const fakeStorage = {
  getItem: (k) => (_ls.has(k) ? _ls.get(k) : null),
  setItem: (k, v) => _ls.set(k, String(v)),
  removeItem: (k) => _ls.delete(k),
  clear: () => _ls.clear(),
};
Object.defineProperty(window, "localStorage", { value: fakeStorage, configurable: true });
Object.defineProperty(globalThis, "localStorage", { value: fakeStorage, configurable: true });

// ---- the server ---------------------------------------------------------------------------
const ENTRY = (id, kind) => ({ id, kind, business_unit: "ops", acl: ["oid-owner"],
                               config: { description: id } });
let serverStores = [ENTRY("csv-1", "csv"), ENTRY("csv-2", "csv")];
const deletes = [];       // {id, keepalive}
const composes = [];      // manifests POSTed to /router/compose
const refuseDeletes = scenario === "refusal";

const J = (body, status = 200) =>
  Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
let authBody = { enabled: true, signed_in: true, name: "Owner", email: "owner@example.com",
                 oid: "oid-owner", google_enabled: false, linked: [] };
globalThis.fetch = (path, opts = {}) => {
  const p = String(path);
  const m = (opts.method || "GET").toUpperCase();
  if (p.startsWith("/auth/me")) return J(authBody);
  if (p.startsWith("/router/demo")) {
    return J({ manifest: { tenant: "acme", stores: [ENTRY("demo-1", "csv")] },
               tenant: "acme" });
  }
  if (m === "DELETE" && p.startsWith("/router/stores/")) {
    const id = decodeURIComponent(p.split("/").pop());
    deletes.push({ id, keepalive: opts.keepalive === true });
    if (refuseDeletes) return J({ detail: "boom" }, 500);
    const entry = serverStores.find((s) => s.id === id) || null;
    serverStores = serverStores.filter((s) => s.id !== id);
    return J({ store_id: id, deleted: entry !== null, entry });
  }
  if (p.startsWith("/router/manifest")) {
    // ALWAYS an array - `stores: []` is authoritative empty, the #731 contract
    return J({ manifest: { tenant: "acme", stores: serverStores } });
  }
  if (p.startsWith("/router/compose")) {
    try { composes.push(JSON.parse(opts.body).manifest); } catch { composes.push(null); }
    return J({ stores: serverStores.map((s) => ({ store_id: s.id, freshness: "live" })),
               skipped: [] });
  }
  if (p.startsWith("/admin/sources")) return J([]);
  if (p.startsWith("/config")) return J({ env: [] });
  return J({});
};

// ---- drive it -----------------------------------------------------------------------------
const mod = await import(pathToFileURL(canvasPath).href);
const settle = async (n = 60) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };

const root = document.getElementById("root");
let teardown = mod.mountCanvas(root);
await settle();

const nodeIds = () => [...document.querySelectorAll(".node .nid")].map((e) => e.textContent.trim());
const nodeEl = (id) => [...document.querySelectorAll(".node")]
  .find((el) => el.querySelector(".nid")?.textContent.trim() === id);

function deleteViaPanel(id) {
  nodeEl(id).dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const btn = document.getElementById("delNode");
  if (!btn) throw new Error("the panel's Remove button never rendered");
  btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
}

function deleteViaMenu(id) {
  nodeEl(id).dispatchEvent(new window.MouseEvent("contextmenu", { bubbles: true }));
  const item = [...document.querySelectorAll("button, .ctx-item, [class*=ctx] *")]
    .find((el) => el.textContent.trim() === "Delete");
  if (!item) throw new Error("the context menu's Delete item never rendered");
  item.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
}

const out = { scenario };

if (scenario === "panel_delete" || scenario === "menu_delete") {
  (scenario === "panel_delete" ? deleteViaPanel : deleteViaMenu)("csv-1");
  await settle();
  out.afterDelete = nodeIds();
  out.deletes = deletes.slice();
  teardown && teardown();
  root.innerHTML = "";                       // the router does this on every route change
  teardown = mod.mountCanvas(root);
  await settle();
  out.afterRemount = nodeIds();
} else if (scenario === "delete_all") {
  deleteViaPanel("csv-1"); await settle();
  deleteViaPanel("csv-2"); await settle();
  out.afterDelete = nodeIds();
  out.deletes = deletes.slice();
  teardown && teardown();
  root.innerHTML = "";
  // THE DISCRIMINATOR (the fixture the matrix demanded): a STALE NON-EMPTY localStorage
  // save at remount - another device, or simply the pre-delete save. The fixed hydration
  // gate takes the server's authoritative `stores: []`; the old `.length` gate read empty
  // as absent and resurrected exactly this stale copy. Without the poison, an honest empty
  // save let restoreCanvas's OWN fix rescue the gate - the rescued-by-both-halves shape.
  window.localStorage.setItem("dbsearch.canvas.v1", JSON.stringify({
    tenant: "acme",
    nodes: [{ id: "csv-1", kind: "csv", bu: "ops", acl: ["oid-owner"],
              config: { description: "csv-1" }, x: 900, y: 470 },
            { id: "csv-2", kind: "csv", bu: "ops", acl: ["oid-owner"],
              config: { description: "csv-2" }, x: 1470, y: 470 }],
  }));
  teardown = mod.mountCanvas(root);
  await settle();
  out.afterRemount = nodeIds();
  out.composesAfterRemount = composes.length;
} else if (scenario === "dev_empty_restore") {
  // The restoreCanvas clause lives on the DEV-RIG fallback only: no real login configured
  // -> loadLiveDemo(), where a saved-but-EMPTY canvas must render EMPTY. The old
  // `.length` guard read it as no-save and resurrected the demo manifest over a canvas
  // the operator had deliberately emptied.
  authBody = { enabled: false, signed_in: false, google_enabled: false, linked: [] };
  window.localStorage.setItem("dbsearch.canvas.v1",
                              JSON.stringify({ tenant: "acme", nodes: [] }));
  teardown && teardown();
  root.innerHTML = "";
  teardown = mod.mountCanvas(root);
  await settle();
  out.afterDevRemount = nodeIds();
} else if (scenario === "refusal") {
  deleteViaPanel("csv-1");
  await settle();
  out.afterRefusedDelete = nodeIds();
  out.deletes = deletes.slice();
  out.toast = document.getElementById("toast")?.textContent?.trim() || null;
} else if (scenario === "undo") {
  deleteViaPanel("csv-1");
  await settle();
  const composesBefore = composes.length;
  const undoBtn = document.querySelector("#undoToast #undoDel");
  if (!undoBtn) throw new Error("the undo toast never rendered");
  undoBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  out.afterUndo = nodeIds();
  out.undoComposed = composes.length > composesBefore;
} else {
  throw new Error(`unknown scenario ${scenario}`);
}

console.log(JSON.stringify(out, null, 1));
process.exit(0);
