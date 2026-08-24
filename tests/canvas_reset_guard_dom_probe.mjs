// #803 - the live-demo reset button vs a signed-in user's stored workspace, driven in a
// real DOM (jsdom) the same way canvas_delete_persists_dom_probe.mjs does (#731 pattern).
//
// The defect: #reset -> loadLiveDemo({fresh:true}) removes the localStorage save, loads
// /router/demo and composeUp()s it - and _persisting_compose OVERWRITES the signed-in
// user's stored user_manifests row with the DEMO manifest. applyModeChrome hides the
// button in DEMO mode only, so a signed-in owner sees it, titled "Load the live demo
// manifest from the server". One click, durable workspace loss (#293: the demo manifest
// is meaningless to a real identity, so they get nothing usable back).
//
// Two clauses, one scenario each, so EITHER clause alone going missing turns a scenario
// red (the #788-shape rule: never a fixture both halves rescue):
//   live_reset_hidden - chrome clause: signed-in live user must not SEE #reset.
//   live_reset_click  - guard clause: even if clicked (stale chrome, a queued handler),
//                       loadLiveDemo must REFUSE for a live user - no demo compose, the
//                       localStorage save intact, the canvas untouched. jsdom dispatches
//                       clicks on display:none elements, which is exactly what makes this
//                       scenario blind to the chrome clause and specific to the guard.
// Controls (green before AND after the fix - they fail an over-broad fix):
//   dev_reset_works   - no real login configured (dev/self-host): reset stays the #199
//                       escape hatch - saved canvas cleared, demo manifest loaded.
//   demo_reset_hidden - demo mode keeps hiding the button (#279 B), unchanged.
//
// #818 repro (separate card, same harness - the scenario documents the defect):
//   draft_dropped_on_remount - signed-in user adds a node from the rail (never composes),
//                       remounts: today the draft is GONE, and the remount's own
//                       saveCanvas destroys the localStorage copy too. Reported raw;
//                       the selftest asserting survival lands with #818's fix.
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
const composes = [];      // manifests POSTed to /router/compose
const demoFetches = [];   // every GET /router/demo

const J = (body, status = 200) =>
  Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
let authBody = { enabled: true, signed_in: true, name: "Owner", email: "owner@example.com",
                 oid: "oid-owner", google_enabled: true, aws_enabled: true,
                   // #823 gates the rail on the provider being LINKED. These probes
                   // drive the palette to test something else entirely (delegation,
                   // autosave, the reset guard), so their fixture user is one who CAN
                   // add these kinds. The assertions below are unchanged.
                   linked: ["entra", "google", "aws"] };
if (scenario === "dev_reset_works")
  authBody = { enabled: false, signed_in: false, google_enabled: false, linked: [] };
if (scenario === "demo_reset_hidden")
  authBody = { enabled: true, signed_in: false, google_enabled: false, linked: [] };

globalThis.fetch = (path, opts = {}) => {
  const p = String(path);
  if (p.startsWith("/auth/me")) return J(authBody);
  if (p.startsWith("/router/demo")) {
    demoFetches.push(p);
    return J({ manifest: { tenant: "acme", stores: [ENTRY("demo-1", "csv")] },
               tenant: "acme" });
  }
  if (p.startsWith("/router/manifest"))
    return J({ manifest: { tenant: "acme", stores: serverStores } });
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

const SAVE_KEY = "dbsearch.canvas.v1";
const nodeIds = () => [...document.querySelectorAll(".node .nid")].map((e) => e.textContent.trim());
const composedIds = (m) => (m && m.stores ? m.stores.map((s) => s.id) : []);
const clickReset = () => document.getElementById("reset")
  .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

function addNodeFromRail() {
  // The real gesture: rail provider row -> flyout -> a service's "+" (never a direct
  // addNode() call - the probe drives what the user drives).
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

if (scenario === "live_reset_hidden") {
  out.resetDisplay = document.getElementById("reset").style.display;
  out.nodesOnMount = nodeIds();
} else if (scenario === "live_reset_click") {
  out.nodesBefore = nodeIds();
  out.composesBefore = composes.map(composedIds);     // boot's own compose (csv-1) lands here
  clickReset();
  await settle();
  out.nodesAfter = nodeIds();
  out.composesAfter = composes.map(composedIds);
  out.demoFetched = demoFetches.length;
  // WHOSE nodes the save holds, not whether one exists - after a demo load the demo
  // state's own renderAll re-writes the save, so mere non-null cannot discriminate.
  const savedNow = JSON.parse(window.localStorage.getItem(SAVE_KEY) || "{}");
  out.localSaveIds = (savedNow.nodes || []).map((n) => n.id);
} else if (scenario === "dev_reset_works") {
  // A dev rig with a saved canvas: reset must still be the escape hatch back to the demo.
  out.resetDisplay = document.getElementById("reset").style.display;
  clickReset();
  await settle();
  out.nodesAfter = nodeIds();
  out.demoFetched = demoFetches.length;
} else if (scenario === "demo_reset_hidden") {
  out.resetDisplay = document.getElementById("reset").style.display;
} else if (scenario === "draft_dropped_on_remount") {
  out.nodesOnMount = nodeIds();
  out.addedKind = addNodeFromRail();
  await settle();
  out.nodesAfterAdd = nodeIds();
  const saved = JSON.parse(window.localStorage.getItem(SAVE_KEY) || "{}");
  out.localSaveAfterAdd = (saved.nodes || []).map((n) => n.id);
  teardown && teardown();
  root.innerHTML = "";                       // the router does this on every route change
  teardown = mod.mountCanvas(root);
  await settle();
  out.nodesAfterRemount = nodeIds();
  const saved2 = JSON.parse(window.localStorage.getItem(SAVE_KEY) || "{}");
  out.localSaveAfterRemount = (saved2.nodes || []).map((n) => n.id);
} else {
  throw new Error(`unknown scenario ${scenario}`);
}

console.log(JSON.stringify(out, null, 1));
process.exit(0);
