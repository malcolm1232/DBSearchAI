// #810 - a failure reason must not survive the compose that fixed it. composeUp's success
// branch set status="connected" but left n.reason in place, and the status dot's tooltip
// renders status+reason - so a store that failed once and then composed clean showed
// "connected: build/probe failed..." in the dot tooltip until a full reload.
// (Only composeUp: adoptApplied rebuilds nodes fresh via nodeFromEntry, so no stale reason
// can exist there - clearing one would be an equivalent-mutant home, the #799 lesson.
// testConn's connected-degraded reason is deliberate and untouched.)
// Same jsdom harness as canvas_draft_autosave_dom_probe.mjs (#818).
//
// Scenario:
//   recompose_clears_reason - boot compose fails BOTH stores (reasons set); a second
//                     compose (the button) fixes csv-1 and keeps csv-2 failing. csv-1's
//                     dot tooltip must be exactly "connected"; csv-2 keeps its reason
//                     (the control an over-broad clear fails).
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
  removeItem: (k) => _ls.delete(k),
  clear: () => _ls.clear(),
};
Object.defineProperty(window, "localStorage", { value: fakeStorage, configurable: true });
Object.defineProperty(globalThis, "localStorage", { value: fakeStorage, configurable: true });

// ---- the server: compose #1 fails both stores, compose #2 fixes csv-1 only --------------
const ENTRY = (id, kind) => ({ id, kind, business_unit: "ops", acl: ["oid-owner"],
                               config: { description: id } });
const serverStores = [ENTRY("csv-1", "csv"), ENTRY("csv-2", "csv")];
const REASON_1 = "build/probe failed: Unable to locate credentials";
const REASON_2 = "build/probe failed: still broken";
let composeCalls = 0;

const J = (body, status = 200) =>
  Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
const authBody = { enabled: true, signed_in: true, name: "Owner", email: "owner@example.com",
                   oid: "oid-owner", google_enabled: false, linked: [] };

globalThis.fetch = (path, opts = {}) => {
  const p = String(path);
  if (p.startsWith("/auth/me")) return J(authBody);
  if ((opts.method || "GET").toUpperCase() === "PUT" && p.startsWith("/router/manifest"))
    return J({ saved: true, stores: serverStores.length });
  if (p.startsWith("/router/manifest"))
    return J({ manifest: { tenant: "acme", stores: serverStores } });
  if (p.startsWith("/router/compose")) {
    composeCalls++;
    // #804 scenario: the endpoint itself errors, so composeUp's .catch runs.
    if (scenario === "failed_compose_button_restores")
      return J({ detail: "manifest needs a tenant" }, 400);
    if (composeCalls === 1)
      return J({ stores: [], skipped: [{ id: "csv-1", reason: REASON_1 },
                                       { id: "csv-2", reason: REASON_2 }] });
    return J({ stores: [{ store_id: "csv-1", freshness: "live" }],
               skipped: [{ id: "csv-2", reason: REASON_2 }] });
  }
  if (p.startsWith("/admin/sources")) return J([]);
  if (p.startsWith("/config")) return J({ env: [] });
  return J({});
};

// ---- drive it -----------------------------------------------------------------------------
const mod = await import(pathToFileURL(canvasPath).href);
const settle = async (n = 60) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };

const root = document.getElementById("root");
mod.mountCanvas(root);
await settle();

const dot = (id) => {
  const el = [...document.querySelectorAll(".node")]
    .find((n) => n.querySelector(".nid")?.textContent.trim() === id);
  const s = el && el.querySelector(".status");
  return s ? { title: s.getAttribute("title"), cls: s.className } : null;
};

const out = { scenario };

if (scenario === "recompose_clears_reason") {
  out.composeCallsAfterBoot = composeCalls;
  out.afterFail = { "csv-1": dot("csv-1"), "csv-2": dot("csv-2") };
  document.getElementById("compose").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }));
  await settle();
  out.composeCallsAfterClick = composeCalls;
  out.afterFix = { "csv-1": dot("csv-1"), "csv-2": dot("csv-2") };
} else if (scenario === "failed_compose_button_restores") {
  // #804 - the failure label must be SHOWN (the error is real information) and must then
  // RELEASE the button: the success path restores the label after 2.6s, the failure path
  // never did, so "Compose failed: ..." was the button's text until a full reload.
  const btn = document.getElementById("compose");
  btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  out.afterFailureText = btn.textContent;
  await new Promise((r) => setTimeout(r, 4300));   // outwait the failure-path restore timer
  out.afterWaitText = btn.textContent;
} else {
  throw new Error(`unknown scenario ${scenario}`);
}

console.log(JSON.stringify(out, null, 1));
process.exit(0);
