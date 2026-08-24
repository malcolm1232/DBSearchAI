// #818 - an added node must survive a reload: the canvas mirrors every mutation into the
// server row (debounced, dirty-checked PUT /router/manifest), and Cmd+S flushes it now.
// Same jsdom harness as canvas_delete_persists_dom_probe.mjs (#731) - the defect only
// exists across a remount, so the probe unmounts and remounts.
//
// The wire stub is a tiny server: PUT /router/manifest REPLACES `serverStores` from the
// posted body, GET /router/manifest serves it - so the remount sees exactly what a real
// reload would see after the autosave (or the loss, when the autosave is missing).
//
// Scenarios:
//   draft_autosave_survives - add a node from the rail, wait out the debounce: a PUT
//                     carries the draft; remount: the draft is still on the canvas.
//   cmd_s_flush     - add a node, press Cmd+S: the PUT lands without waiting for the
//                     debounce, the browser's save dialog is suppressed
//                     (defaultPrevented), and a "Workspace saved" toast confirms.
//   clean_no_put    - mount and touch nothing: ZERO PUTs. Hydration must not re-save the
//                     row it just read (the dirty-check clause).
//   dev_no_put      - no real login (dev rig): add a node, ZERO PUTs - localStorage
//                     remains the dev rig's only store; the server row is a signed-in
//                     concept (#368).
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
// jsdom has no pointer capture; the drag handler calls both. Inert stubs keep the REAL
// pointerdown/move/up path drivable.
window.HTMLElement.prototype.setPointerCapture = () => {};
window.HTMLElement.prototype.releasePointerCapture = () => {};
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
let serverStores = [ENTRY("csv-1", "csv")];
let serverLayout = scenario === "layout_hydrates_from_server" ? { "csv-1": [1234, 777] } : null;
const puts = [];          // manifests PUT to /router/manifest
const composes = [];      // manifests POSTed to /router/compose

const J = (body, status = 200) =>
  Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
let authBody = { enabled: true, signed_in: true, name: "Owner", email: "owner@example.com",
                 oid: "oid-owner", google_enabled: true, aws_enabled: true,
                   // #823 gates the rail on the provider being LINKED. These probes
                   // drive the palette to test something else entirely (delegation,
                   // autosave, the reset guard), so their fixture user is one who CAN
                   // add these kinds. The assertions below are unchanged.
                   linked: ["entra", "google", "aws"] };
if (scenario === "dev_no_put")
  authBody = { enabled: false, signed_in: false, google_enabled: false, linked: [] };

globalThis.fetch = (path, opts = {}) => {
  const p = String(path);
  const m = (opts.method || "GET").toUpperCase();
  if (p.startsWith("/auth/me")) return J(authBody);
  if (p.startsWith("/router/demo"))
    return J({ manifest: { tenant: "acme", stores: [ENTRY("demo-1", "csv")] }, tenant: "acme" });
  if (m === "PUT" && p.startsWith("/router/manifest")) {
    // the real endpoint replaces the caller's row with the posted manifest, verbatim
    try {
      const body = JSON.parse(opts.body).manifest;
      puts.push(body);
      serverStores = body.stores;
      serverLayout = body.layout || null;
      return J({ saved: true, stores: serverStores.length });
    } catch { puts.push(null); return J({ detail: "bad body" }, 400); }
  }
  if (p.startsWith("/router/manifest")) {
    const man = { tenant: "acme", stores: serverStores };
    if (serverLayout) man.layout = serverLayout;
    return J({ manifest: man });
  }
  if (p.startsWith("/router/compose")) {
    try { composes.push(JSON.parse(opts.body).manifest); } catch { composes.push(null); }
    return J({ stores: serverStores.filter((s) => s.id === "csv-1")
                 .map((s) => ({ store_id: s.id, freshness: "live" })),
               skipped: serverStores.filter((s) => s.id !== "csv-1")
                 .map((s) => ({ id: s.id, reason: "draft" })) });
  }
  if (p.startsWith("/admin/sources")) return J([]);
  if (p.startsWith("/config")) return J({ env: [] });
  return J({});
};

// ---- drive it -----------------------------------------------------------------------------
const mod = await import(pathToFileURL(canvasPath).href);
const settle = async (n = 60) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const root = document.getElementById("root");
let teardown = mod.mountCanvas(root);
await settle();

const nodeIds = () => [...document.querySelectorAll(".node .nid")].map((e) => e.textContent.trim());
const putIds = () => puts.map((m) => (m && m.stores ? m.stores.map((s) => s.id) : null));

function addNodeFromRail() {
  const row = document.querySelector("#rail .prov");
  if (!row) throw new Error("the rail rendered no provider rows");
  row.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const svc = document.querySelector("#provmenu .svc");
  if (!svc) throw new Error("the provider flyout rendered no services");
  const kind = svc.dataset.kind;
  svc.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  return kind;
}

const out = { scenario };

if (scenario === "draft_autosave_survives") {
  out.addedKind = addNodeFromRail();
  await settle();
  out.putsRightAfterAdd = puts.length;      // the debounce must still be pending here
  await sleep(1400);                        // ride out the debounce window
  await settle();
  out.putIds = putIds();
  teardown && teardown();
  root.innerHTML = "";
  teardown = mod.mountCanvas(root);
  await settle();
  out.nodesAfterRemount = nodeIds();
} else if (scenario === "cmd_s_flush") {
  out.addedKind = addNodeFromRail();
  await settle();
  const ev = new window.KeyboardEvent("keydown",
    { key: "s", metaKey: true, bubbles: true, cancelable: true });
  document.dispatchEvent(ev);
  out.defaultPrevented = ev.defaultPrevented;
  await settle();
  out.putsAfterCmdS = puts.length;          // flushed NOW, not after the debounce
  out.putIds = putIds();
  out.toast = document.getElementById("toast")?.textContent?.trim() || null;
} else if (scenario === "clean_no_put") {
  await sleep(1400);
  await settle();
  out.puts = puts.length;
  out.nodes = nodeIds();
} else if (scenario === "dev_no_put") {
  out.addedKind = addNodeFromRail();
  await sleep(1400);
  await settle();
  out.puts = puts.length;
  out.nodesAfterAdd = nodeIds();
} else if (scenario === "layout_hydrates_from_server") {
  // A row layout written on ANOTHER device: this mount has an EMPTY localStorage, so the
  // position can only have come from the server row.
  const el = [...document.querySelectorAll(".node")]
    .find((n) => n.querySelector(".nid")?.textContent.trim() === "csv-1");
  out.left = el ? el.style.left : null;
  out.top = el ? el.style.top : null;
} else if (scenario === "move_saves_layout") {
  // Ride out the BOOT render's own debounce first (it resolves clean, 0 PUTs) - without
  // this the drag lands inside that pending window and the boot timer rescues a mutant
  // that deleted the drag-end save (the matrix caught exactly that fixture flaw).
  await sleep(1100);
  await settle();
  out.putsBeforeDrag = puts.length;
  const el = [...document.querySelectorAll(".node")]
    .find((n) => n.querySelector(".nid")?.textContent.trim() === "csv-1");
  const head = el.querySelector(".nhead");
  out.before = [el.style.left, el.style.top];
  const ev = (type, x, y) => new window.MouseEvent(type, { bubbles: true, button: 0,
                                                           clientX: x, clientY: y });
  head.dispatchEvent(ev("pointerdown", 100, 100));
  head.dispatchEvent(ev("pointermove", 260, 240));
  head.dispatchEvent(ev("pointerup", 260, 240));
  await settle();
  out.after = [el.style.left, el.style.top];
  await sleep(1400);                        // ride out the debounce
  await settle();
  out.putLayouts = puts.map((m) => (m && m.layout ? m.layout["csv-1"] : null));
} else {
  throw new Error(`unknown scenario ${scenario}`);
}

console.log(JSON.stringify(out, null, 1));
process.exit(0);
