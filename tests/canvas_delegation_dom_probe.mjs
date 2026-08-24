// #809 - a palette-added Redshift store must carry its aws_keys delegation block WITHOUT
// the user toggling anything: prod has no ambient AWS identity, so an undelegated redshift
// entry composes to "Unable to locate credentials" every time (ADR 0024 says AWS kinds
// delegate through the caller's own vaulted keys; #673 already made s3 always-delegated).
// Same jsdom harness as canvas_draft_autosave_dom_probe.mjs (#818).
//
// Scenarios:
//   palette_redshift_delegates - add Redshift and Azure SQL from the rail, Cmd+S: the PUT
//                     row carries {kind:"aws_keys",resource:"redshift"} on the redshift
//                     entry; the azure_sql and pre-existing csv entries carry NO delegation
//                     (the controls an over-broad fix fails).
//   yaml_preview    - add Redshift and S3, open the manifest drawer: the preview shows the
//                     delegation line for BOTH (the s3 preview lied before #809 - the
//                     "Same rule as entryOf" comment wasn't), and none for csv.
//   panel_switch    - the redshift panel offers NO require_signin switch (always-delegated
//                     kinds have no identity choice - offering one is the hollow-offer
//                     shape, #654/#656/#660); azure_sql keeps its switch (control).
//
// #814 (ADR 0026) scenarios, same rig - the RDS kinds join the aws_keys rail:
//   palette_rds_delegates - add rds_postgres and rds_mysql from the rail, Cmd+S: each PUT
//                     entry carries {kind:"aws_keys",resource:"rds"}.
//   rds_panel_no_password - the rds_postgres panel collects NO password (the IAM token is
//                     the password, minted server-side) but keeps the db user field;
//                     azure_sql keeps its password field (control).
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

// ---- the server (signed-in live user, one pre-existing csv store) -------------------------
const ENTRY = (id, kind) => ({ id, kind, business_unit: "ops", acl: ["oid-owner"],
                               config: { description: id } });
let serverStores = [ENTRY("csv-1", "csv")];
const puts = [];

const J = (body, status = 200) =>
  Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
const authBody = { enabled: true, signed_in: true, name: "Owner", email: "owner@example.com",
                   oid: "oid-owner", google_enabled: true, aws_enabled: true,
                   // #823 gates the rail on the provider being LINKED. These probes
                   // drive the palette to test something else entirely (delegation,
                   // autosave, the reset guard), so their fixture user is one who CAN
                   // add these kinds. The assertions below are unchanged.
                   linked: ["entra", "google", "aws"] };

globalThis.fetch = (path, opts = {}) => {
  const p = String(path);
  const m = (opts.method || "GET").toUpperCase();
  if (p.startsWith("/auth/me")) return J(authBody);
  if (m === "PUT" && p.startsWith("/router/manifest")) {
    try {
      const body = JSON.parse(opts.body).manifest;
      puts.push(body);
      serverStores = body.stores;
      return J({ saved: true, stores: serverStores.length });
    } catch { puts.push(null); return J({ detail: "bad body" }, 400); }
  }
  if (p.startsWith("/router/manifest"))
    return J({ manifest: { tenant: "acme", stores: serverStores } });
  if (p.startsWith("/router/compose"))
    return J({ stores: [{ store_id: "csv-1", freshness: "live" }], skipped: [] });
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

// Click through the rail to a SPECIFIC kind: open each provider row until the flyout
// offers the kind, then click that service.
function addKindFromRail(kind) {
  for (const row of document.querySelectorAll("#rail .prov")) {
    row.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const svc = document.querySelector(`#provmenu .svc[data-kind="${kind}"]`);
    if (svc) { svc.dispatchEvent(new window.MouseEvent("click", { bubbles: true })); return; }
  }
  throw new Error(`no provider flyout offers kind ${kind}`);
}
const flushRow = async () => {
  document.dispatchEvent(new window.KeyboardEvent("keydown",
    { key: "s", metaKey: true, bubbles: true, cancelable: true }));
  await settle();
};
const nodeEl = (kind) => [...document.querySelectorAll(".node")]
  .find((n) => n.querySelector(".nid")?.textContent.trim().startsWith(kind));

const out = { scenario };

if (scenario === "palette_redshift_delegates") {
  addKindFromRail("redshift");
  addKindFromRail("azure_sql");
  await settle();
  await flushRow();
  const last = puts.at(-1);
  out.putCount = puts.length;
  out.delegations = {};
  for (const s of (last && last.stores) || [])
    out.delegations[s.kind] = s.delegation === undefined ? null : s.delegation;
} else if (scenario === "yaml_preview") {
  addKindFromRail("redshift");
  addKindFromRail("s3");
  await settle();
  document.getElementById("export").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }));
  await settle();
  out.yaml = document.getElementById("yaml").textContent;
} else if (scenario === "palette_rds_delegates") {
  addKindFromRail("rds_postgres");
  addKindFromRail("rds_mysql");
  await settle();
  await flushRow();
  const last = puts.at(-1);
  out.putCount = puts.length;
  out.delegations = {};
  for (const s of (last && last.stores) || [])
    out.delegations[s.kind] = s.delegation === undefined ? null : s.delegation;
} else if (scenario === "rds_panel_no_password") {
  const panelField = (k) =>
    !!(document.querySelector(`#panel input[data-cfg="${k}"]`) ||
       document.querySelector(`#panel input[data-secretfield="${k}"]`));
  addKindFromRail("rds_postgres");
  await settle();
  nodeEl("rds_postgres").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  out.rdsHasPassword = panelField("password");
  out.rdsHasUser = panelField("user");
  addKindFromRail("azure_sql");
  await settle();
  nodeEl("azure_sql").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  out.azureSqlHasPassword = panelField("password");
} else if (scenario === "panel_switch") {
  addKindFromRail("redshift");
  await settle();
  nodeEl("redshift").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  out.redshiftHasSwitch = !!document.querySelector('#panel input[data-cfg="require_signin"]');
  addKindFromRail("azure_sql");
  await settle();
  nodeEl("azure_sql").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  out.azureSqlHasSwitch = !!document.querySelector('#panel input[data-cfg="require_signin"]');
} else {
  throw new Error(`unknown scenario ${scenario}`);
}

console.log(JSON.stringify(out, null, 1));
process.exit(0);
