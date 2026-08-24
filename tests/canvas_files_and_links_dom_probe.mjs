// #920 - the document kinds live under "Files & Links" and need NO cloud-brand account.
//
// THE OWNER'S REQUIREMENT, verbatim (260822): "what if i dont connect to any google account?
// i need to NOT be connected to google and still use the gdrive and connect link."
//
// So the identity this probe boots is the one that matters: signed in to DBSearch, with
// `linked: []` - no Google, no Microsoft - on a deployment where BOTH logins are configured
// (so realLoginConfigured() is true and providerGate genuinely engages; a dev rig would
// short-circuit it and prove nothing).
//
// Scenarios:
//   unlinked_can_add_gdrive - the "Files & Links" row exists, its flyout lists gdrive and
//                             sharepoint with NO gate, and clicking Google Drive ADDS a node
//                             carrying the adder's own oid as its audience (#920: filed
//                             private to the adder, and the gdrive factory REFUSES an empty
//                             acl, so an empty one would fail at compose).
//   databases_still_gated   - the brand gate is intact where the as-you rationale is real:
//                             Google Cloud still lists ONLY bigquery and still gates an
//                             unlinked caller; Azure still gates.
//   linked_user_unchanged   - a caller WITH google linked sees the same Files & Links row
//                             (the regroup is not conditional on linkage).
//   entra_linked_opens_sharepoint
//                           - the other half of the kind-level gate, and the control that
//                             stops "no gate at all" passing as a fix: SharePoint is gated
//                             for the unlinked caller above and UNGATED once entra is
//                             vaulted, because its ingest really does run on the caller's
//                             own Microsoft consent wherever the tile is filed.
//
// Read by tests/selftest_920_files_and_links.py; reports JSON on stdout.
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
const _ls = new Map();
Object.defineProperty(window, "localStorage", {
  value: { getItem: (k) => (_ls.has(k) ? _ls.get(k) : null),
           setItem: (k, v) => _ls.set(k, String(v)),
           removeItem: (k) => _ls.delete(k), clear: () => _ls.clear() },
  configurable: true });
Object.defineProperty(globalThis, "localStorage", { value: window.localStorage, configurable: true });

// ---- the server -----------------------------------------------------------------------------
// BOTH idps configured (enabled + google_enabled) so providerGate really runs; `linked` is the
// variable under test.
const LINKED = scenario === "linked_user_unchanged" ? ["google"]
             : scenario === "entra_linked_opens_sharepoint" ? ["entra"] : [];
const authBody = { enabled: true, google_enabled: true, aws_enabled: true, signed_in: true,
                   name: "Nolink", email: "nolink@example.com", oid: "oid-nolink",
                   idp: "local", has_org: false, linked: LINKED };
const J = (body, status = 200) =>
  Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
globalThis.fetch = (path, opts = {}) => {
  const p = String(path);
  if (p.startsWith("/auth/me")) return J(authBody);
  if (p.startsWith("/admin/documents")) return J([]);
  if (p.startsWith("/admin/sources")) return J([]);
  if (p.startsWith("/admin/principals")) return J({ available: false, principals: [], reason: "n/a" });
  if (p.startsWith("/connectors/sharepoint/status")) return J({ configured: false, connected: [] });
  if (p.startsWith("/router/manifest")) return J({ manifest: { tenant: "acme", stores: [] } });
  if (p.startsWith("/router/compose")) return J({ stores: [], skipped: [] });
  if (p.startsWith("/config")) return J({ env: [], operator: false });
  return J({});
};

// ---- drive it -------------------------------------------------------------------------------
const mod = await import(pathToFileURL(canvasPath).href);
const settle = async (n = 60) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };
const root = document.getElementById("root");
mod.mountCanvas(root);
await settle();

const rowFor = (re) => [...document.querySelectorAll(".prov")].find((e) => re.test(e.textContent));
const openRow = (row) => row.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
const menuState = () => {
  const pm = document.getElementById("provmenu");
  return {
    header: pm.querySelector(".mh")?.textContent.trim() || null,
    kinds: [...pm.querySelectorAll(".svc")].map((e) => e.dataset.kind),
    // #920: the row may now be OPEN while one tile inside it is gated on its own
    // requirement, so "which kinds are listed" is no longer the same question as "which
    // kinds this caller can add".
    addable: [...pm.querySelectorAll(".svc:not(.gated)")].map((e) => e.dataset.kind),
    gatedKinds: [...pm.querySelectorAll(".svc.gated")].map((e) => e.dataset.kind),
    gatedCtas: [...pm.querySelectorAll(".svc.gated .snn span")].map((e) => e.textContent.trim()),
    gate: pm.querySelector(".gate")?.textContent.trim().slice(0, 80) || null,
  };
};
// The kind a node card DECLARES to the reader (`.nkind`), not a class name - the card has no
// per-kind class, and a probe that invented one reported "no node added" against a canvas
// that had just added one.
const nodeKinds = () => [...document.querySelectorAll(".node")]
  .map((el) => el.querySelector(".nkind")?.textContent.trim() || null);
// #920: the audience the card SHOWS. buildNode renders the adder's own oid as "You" and an
// empty acl as a "no ACL" warning pill, so this reads the same two states a person does.
const nodeAcls = (kind) => {
  const el = [...document.querySelectorAll(".node")]
    .find((n) => n.querySelector(".nkind")?.textContent.trim() === kind);
  return el ? [...el.querySelectorAll(".nbody .pill")].map((p) => p.textContent.trim()) : null;
};

const out = { scenario, railRows: [...document.querySelectorAll(".prov")].map((e) => {
  const b = e.querySelector("b"); return b ? b.textContent.trim() : e.textContent.trim().slice(0, 20);
}) };

// #924: the same unlinked identity clicks the SharePoint LINK tile instead. Read by
// tests/selftest_924_sharepoint_link.py; the 920 scenarios are untouched.
const CLICK = scenario === "unlinked_can_add_sharepoint_link" ? "sharepoint_link" : "gdrive";
if (scenario === "unlinked_can_add_gdrive" || scenario === "linked_user_unchanged"
    || scenario === "entra_linked_opens_sharepoint"
    || scenario === "unlinked_can_add_sharepoint_link") {
  const files = rowFor(/Files & Links/);
  out.filesRowExists = !!files;
  if (!files) { console.log(JSON.stringify(out, null, 1)); process.exit(0); }
  openRow(files);
  await settle();
  const m = menuState();
  out.filesMenu = m;
  out.gdriveOffered = m.kinds.includes("gdrive");
  out.sharepointOffered = m.kinds.includes("sharepoint");
  out.filesGated = m.gate !== null;
  const gd = document.querySelector(`#provmenu .svc[data-kind="${CLICK}"]`);
  if (gd) {
    gd.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await settle();
  }
  out.nodeAdded = nodeKinds().includes(CLICK);
  // #920: filed private to the adder. Read off the card, which says "You" for the caller's
  // own oid and warns "no ACL" when the audience is empty - the empty case is the one that
  // composes green and answers nothing under LAW 2.
  out.pills = nodeAcls(CLICK);
  out.gdrivePills = out.pills;
  out.aclCarriesAdder = (out.pills || []).includes("You");
} else if (scenario === "databases_still_gated") {
  const g = rowFor(/Google Cloud/);
  out.googleRowExists = !!g;
  if (g) { openRow(g); await settle(); out.googleMenu = menuState(); }
  const az = rowFor(/Azure/);
  if (az) { openRow(az); await settle(); out.azureMenu = menuState(); }
} else {
  throw new Error(`unknown scenario ${scenario}`);
}

console.log(JSON.stringify(out, null, 1));
process.exit(0);
