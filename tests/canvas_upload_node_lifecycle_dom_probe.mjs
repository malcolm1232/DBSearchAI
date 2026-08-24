// #923 - the uploads node is FIRST-CLASS: drives the REAL canvas in a real DOM (jsdom)
// through the owner's three rulings, remounting between steps because two of the three
// only exist across a reload (the canvas_delete_persists pattern - a probe that never
// remounts is structurally blind to persistence).
//
//   add          - clicking Files & Links -> "Upload files" in the sidebar ADDS the node
//                  (auto-selected, panel open, NO modal), Cmd+S writes the row whose layout
//                  carries "your-documents"; a remount on that row restores the node at
//                  0 docs. The modal is never the first hop.
//   refresh_docs - #921: a user whose ONLY source is uploads (row = authoritative empty
//                  stores, no marker) refreshes - the node must be there with the count.
//   delete_full  - node delete arms an inline confirm naming the OWN-doc count (nothing
//                  sent while armed), then DELETEs exactly the caller's own documents -
//                  never one merely shared TO them - and the node leaves the canvas. The
//                  remount (on the row the flush wrote, docs now the shared leftover)
//                  auto-adopts a "1 doc" node: the shared doc still exists and honesty
//                  beats tidiness.
//   delete_empty - deleting an EMPTY node is not destructive: no confirm, no /documents
//                  DELETE, and the remount keeps it gone (the marker really left the row).
//
// Read by tests/selftest_923_upload_node_lifecycle.py; reports JSON on stdout.
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
const fakeStorage = {
  getItem: (k) => (_ls.has(k) ? _ls.get(k) : null),
  setItem: (k, v) => _ls.set(k, String(v)),
  removeItem: (k) => _ls.delete(k),
  clear: () => _ls.clear(),
};
Object.defineProperty(window, "localStorage", { value: fakeStorage, configurable: true });
Object.defineProperty(globalThis, "localStorage", { value: fakeStorage, configurable: true });

// ---- the server ---------------------------------------------------------------------------
const DOC = (id, shared) => ({ doc_external_id: id, title: "Doc " + id,
  uri: "upload://" + id, allowed_principals: [shared ? "oid-other" : "oid-owner"],
  shared_with_you: !!shared });

// stores is ALWAYS an array - authoritative empty, the #731 contract
let serverManifest = { tenant: "acme", stores: [], layout: {} };
let serverDocs = [];
const docDeletes = [];   // every DELETE /documents/{id} attempted
const rowPuts = [];      // every PUT /router/manifest body

const J = (body, status = 200) =>
  Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
const authBody = { enabled: true, signed_in: true, name: "Owner", email: "owner@example.com",
                   oid: "oid-owner", google_enabled: false, linked: [] };
globalThis.fetch = (path, opts = {}) => {
  const p = String(path);
  const m = (opts.method || "GET").toUpperCase();
  if (p.startsWith("/auth/me")) return J(authBody);
  if (m === "DELETE" && p.startsWith("/documents/")) {
    const id = decodeURIComponent(p.split("/").pop());
    docDeletes.push(id);
    const doc = serverDocs.find((d) => d.doc_external_id === id);
    if (!doc) return J({ detail: "not found" }, 404);
    if (doc.shared_with_you) return J({ detail: "only the owner may delete" }, 403);
    serverDocs = serverDocs.filter((d) => d.doc_external_id !== id);
    return J({ deleted: id });
  }
  if (p.startsWith("/admin/documents")) return J(serverDocs.slice());
  if (m === "PUT" && p.startsWith("/router/manifest")) {
    try {
      const b = JSON.parse(opts.body);
      rowPuts.push(b);
      serverManifest = b.manifest || b;   // the row adopts what the canvas saved
    } catch { rowPuts.push(null); }
    return J({ saved: true });
  }
  if (p.startsWith("/router/manifest")) return J({ manifest: serverManifest });
  if (p.startsWith("/router/compose")) return J({ stores: [], skipped: [] });
  if (p.startsWith("/connectors/sharepoint/status")) return J({ configured: false, connected: [] });
  if (p.startsWith("/admin/sources")) return J([]);
  if (p.startsWith("/config")) return J({ env: [] });
  return J({});
};

// ---- drive it -----------------------------------------------------------------------------
const mod = await import(pathToFileURL(canvasPath).href);
const settle = async (n = 60) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };

const root = document.getElementById("root");
let teardown;
const mount = async () => { teardown = mod.mountCanvas(root); await settle(); };
const remount = async () => { teardown && teardown(); root.innerHTML = ""; await mount(); };

const nodeEl = () => [...document.querySelectorAll(".node")]
  .find((el) => el.querySelector(".nid")?.textContent.trim() === "your-documents");
const nodeFreshness = () => {
  const el = nodeEl(); if (!el) return null;
  const pill = [...el.querySelectorAll(".pill")].find((s) => /\bdocs?\b/.test(s.textContent));
  return pill ? pill.textContent.trim() : null;
};
const panelText = () => document.getElementById("panel")?.textContent || "";
const modalOpen = () => document.getElementById("spPicker")?.classList.contains("show") === true;

function clickUploadInSidebar() {
  const row = [...document.querySelectorAll(".prov")]
    .find((el) => /Files & Links/.test(el.textContent));
  if (!row) throw new Error("the Files & Links provider row never rendered");
  row.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const svc = document.querySelector('#provmenu .svc[data-kind="upload"]');
  if (!svc) throw new Error("the flyout's Upload files entry never rendered");
  svc.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
}
function deleteViaMenu() {
  nodeEl().dispatchEvent(new window.MouseEvent("contextmenu", { bubbles: true }));
  const item = [...document.querySelectorAll("#ctxmenu .ci")]
    .find((el) => el.textContent.trim() === "Delete");
  if (!item) throw new Error("the context menu's Delete item never rendered");
  item.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
}
const flushRow = async () => {
  document.dispatchEvent(new window.KeyboardEvent("keydown",
    { key: "s", ctrlKey: true, bubbles: true, cancelable: true }));
  await settle();
};

const out = { scenario };

if (scenario === "add") {
  await mount();
  out.nodeBefore = !!nodeEl();
  clickUploadInSidebar();
  await settle();
  out.nodeAfterAdd = !!nodeEl();
  out.freshness = nodeFreshness();
  // #950: the panel no longer carries an upload button at all - the one affordance lives on
  // the node. The assertion's intent is unchanged (this panel is the overview), so it anchors
  // on the overview's own copy rather than on a button that deliberately left.
  out.panelIsOverview = /Your documents/.test(panelText())
    && /private to you unless shared/.test(panelText());
  out.modalOpen = modalOpen();
  await flushRow();
  out.lastPutLayoutHasMarker = rowPuts.length > 0 &&
    Array.isArray((rowPuts[rowPuts.length - 1].manifest || {}).layout?.["your-documents"]);
  await remount();       // the row it just saved is what this reload serves
  out.nodeAfterRemount = !!nodeEl();
  out.freshnessAfterRemount = nodeFreshness();
} else if (scenario === "node_upload_button") {
  // #950 (owner, 260824): "i click upload files, nth happens then im like 'huh?'". The node's
  // own button is an IMPERATIVE - it must open the file picker. Before the fix its whole
  // behaviour was `selected=node.uid; renderAll()`, and #923 already auto-selects the node on
  // add, so the panel was open already and the click did nothing visible at all.
  await mount();
  clickUploadInSidebar();
  await settle();
  const el = nodeEl();
  out.nodeAdded = !!el;
  // the state the owner was actually in: node already selected, panel already open
  out.selectedBeforeClick = !!el && el.className.includes("sel");
  out.modalBeforeClick = modalOpen();
  const btn = el && el.querySelector(".up-add");
  out.buttonFound = !!btn;
  out.buttonLabel = btn ? btn.textContent.trim() : null;
  // #950 (owner ruling): the panel must carry NO upload button - exactly one affordance, on
  // the node. Counted, not merely looked up, so a second one reappearing is visible.
  out.panelUploadButtons = [...document.querySelectorAll("#panel button")]
    .filter((b) => /upload|add files/i.test(b.textContent)).length;
  if (btn) {
    btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await settle();
  }
  out.modalAfterClick = modalOpen();
  out.filePickerPresent = !!document.querySelector("#spPickerBody input[type=file]");
} else if (scenario === "refresh_docs") {
  // #921 verbatim: uploads exist, NOTHING else composed, no marker - the refresh gesture
  serverDocs = [DOC("a", false), DOC("b", false), DOC("c", false), DOC("d", false), DOC("e", false)];
  await mount();
  out.nodePresent = !!nodeEl();
  out.freshness = nodeFreshness();
} else if (scenario === "delete_full") {
  serverManifest = { tenant: "acme", stores: [], layout: { "your-documents": [320, 800] } };
  serverDocs = [DOC("own-1", false), DOC("own-2", false), DOC("shared-1", true)];
  await mount();
  out.nodePresent = !!nodeEl();
  out.freshness = nodeFreshness();
  deleteViaMenu();
  await settle();
  // the confirm is a MODAL (owner, 260821), never a sidebar block and never a native confirm()
  const modalBody = document.getElementById("spPickerBody")?.textContent || "";
  out.confirmInModal = modalOpen() && /permanently delete 2 uploaded documents/.test(modalBody);
  out.confirmInPanel = /permanently delete/.test(panelText());
  out.deletesWhileArmed = docDeletes.slice();      // arming must send NOTHING
  // Keep first: it must close the modal, delete nothing, and leave the node standing
  const keep = document.querySelector("#spPickerBody .updoc-nodedel0");
  if (!keep) throw new Error("the Keep button never rendered in the modal");
  keep.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  out.keepClosesModal = !modalOpen();
  out.keepDeletedNothing = docDeletes.length === 0;
  out.nodeAfterKeep = !!nodeEl();
  deleteViaMenu();                                  // re-arm for the real delete
  await settle();
  const go = document.querySelector("#spPickerBody .updoc-nodedel2");
  if (!go) throw new Error("the destructive confirm never rendered in the modal");
  go.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  out.modalClosedAfter = !modalOpen();
  out.deletesSent = docDeletes.slice().sort();
  out.sharedSurvives = serverDocs.some((d) => d.doc_external_id === "shared-1");
  out.nodeAfterDelete = !!nodeEl();
  await flushRow();
  await remount();
  // the shared doc still exists and is readable - the overview auto-adopts, honestly
  out.nodeAfterRemount = !!nodeEl();
  out.freshnessAfterRemount = nodeFreshness();
} else if (scenario === "delete_empty") {
  serverManifest = { tenant: "acme", stores: [], layout: { "your-documents": [320, 800] } };
  await mount();
  out.nodePresent = !!nodeEl();
  deleteViaMenu();
  await settle();
  out.nodeAfterDelete = !!nodeEl();
  out.docDeletes = docDeletes.slice();             // nothing destructive may be sent
  await flushRow();
  out.lastPutLayoutHasMarker = rowPuts.length > 0 &&
    Array.isArray((rowPuts[rowPuts.length - 1].manifest || {}).layout?.["your-documents"]);
  await remount();
  out.nodeAfterRemount = !!nodeEl();
} else {
  throw new Error(`unknown scenario ${scenario}`);
}

console.log(JSON.stringify(out, null, 1));
process.exit(0);
