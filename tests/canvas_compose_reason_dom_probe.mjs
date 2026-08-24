// #781 - drives the REAL canvas compose path in a real DOM (jsdom) and reports what a
// signed-in owner would actually see when /router/compose SKIPS a store, as JSON on stdout.
// Read by tests/selftest_781_compose_reason.py.
//
// THE DEFECT THIS EXISTS TO CATCH. POST /router/compose already returns, per skipped store,
// a precise reason ("build/probe failed: postgres config missing [password] ..."), and
// composeUp() already writes it onto the node (n.reason). The render layer then threw it
// away: the node's status dot said title="planned" - the status WORD, not the reason - the
// node card showed nothing, and the status bar said "5 sources · 3 connected" naming neither
// the failing stores nor the cause. Reproduced live on prod (card #781): the owner watched a
// node turn red with the one-word tooltip "planned" while the response carried the answer.
// The inspector panel DID render node.reason - but only once you open the failing node, and
// nothing on screen told you which node to open.
//
// So this boots the canvas exactly as prod does for a signed-in user - /auth/me says
// signed_in, /router/manifest returns their stored stores, and boot's own composeUp() POSTs
// to /router/compose - then reads back, per node: the status dot's title, any visible reason
// element, and the status bar's text.
//
// THE SHARPER HALF IS INJECTION (#786's lesson). The reason string is SERVER text landing in
// two different sink classes: an attribute (the dot's title) and element content (the visible
// line). An attribute sink is broken by `"` and content by `<`, so the `sharepoint` fixture
// reason carries both plus an on* handler payload. The probe reports every element and every
// event-handler attribute the hostile string managed to create; the guard requires zero.
//
// It is NOT a browser: no layout, no paint, no :hover. It can say "the reason is in the
// document"; it cannot say "the reason is readable at 212px" - the real-browser pass on prod
// is still owed and is what closes the card.
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
window.localStorage.clear();

// ---- the fixtures -----------------------------------------------------------------------
//
// The owner's live manifest: three stores. azure_sql-1 composes; the other two are skipped,
// which is the 260817 prod state verbatim (an RDS store missing its password, and the owner's
// EXISTING sharepoint store silently skipped for "no ACL" - sub-finding (c) on the card: the
// silence already cost him on a store he did not add that day).
const MANIFEST = { tenant: "acme", stores: [
  { id: "azure_sql-1",    kind: "azure_sql",    business_unit: "finance", acl: ["oid-owner"],
    config: { description: "sales" } },
  { id: "rds_postgres-1", kind: "rds_postgres", business_unit: "ops",     acl: ["oid-owner"],
    config: { description: "inventory" } },
  { id: "sharepoint",     kind: "sharepoint",   business_unit: "hr",      acl: [],
    config: { description: "policies" } },
] };

// The real prod reason, verbatim from the card's live repro.
const RDS_REASON = "build/probe failed: postgres config missing [password] "
  + "(use ${ENV} refs in stores.yml - resolved server-side)";
// The hostile reason: `"` breaks out of an unescaped attribute, `<img ... onerror>` executes
// in unescaped content. Both characters in ONE string so a fixture cannot be rescued by the
// sink it never reaches (#786: the esc drop survived until the fixture carried `<`).
const HOSTILE_REASON = 'no ACL" onmouseover="alert(1)" data-x="'
  + '<img src=x onerror=alert(2)> - nobody can see this store';

const COMPOSE = {
  skipped: {
    stores: [{ store_id: "azure_sql-1", freshness: "live" }],
    skipped: [{ id: "rds_postgres-1", kind: "rds_postgres", reason: RDS_REASON },
              { id: "sharepoint",     kind: "sharepoint",   reason: HOSTILE_REASON }],
  },
  // The control scenario: everything composes. The reason line and the status-bar warning
  // must NOT render - a fix that stamps warnings on healthy canvases fails this.
  clean: {
    stores: [{ store_id: "azure_sql-1", freshness: "live" },
             { store_id: "rds_postgres-1", freshness: "live" },
             { store_id: "sharepoint", freshness: "live" }],
    skipped: [],
  },
  // #808: every store COMPOSED - nothing is skipped - but azure_sql-1's `tables:` allowlist
  // matched nothing, so it is live, routable, and able to answer precisely nothing. The
  // canvas had no vocabulary for this: `.nreason` is gated on status==="planned", which this
  // store is not. The hostile string rides the warning for the same reason it rides the
  // reason above - this is a NEW pair of sinks (a title attribute and element content), and
  // #786's lesson is that an escape drop survives until the fixture actually carries `"`
  // and `<`.
  warned: {
    stores: [{ store_id: "azure_sql-1", freshness: "live",
               warnings: ['This source connected, but the `tables:` allowlist matched none '
                          + 'of its tables" onmouseover="alert(1)" data-x="'
                          + '<img src=x onerror=alert(3)> - schema-qualify the entries'] },
             { store_id: "rds_postgres-1", freshness: "live", warnings: [] },
             { store_id: "sharepoint", freshness: "live" }],
    skipped: [],
  },
};
const compose = COMPOSE[scenario];
if (!compose) throw new Error(`unknown scenario ${scenario}`);

// ---- the wire ----------------------------------------------------------------------------
// Stub `fetch`, never the module. /auth/me says signed-in so bootCanvas takes loadLiveUser,
// /router/manifest returns the stored stores, and boot's composeUp() gets the fixture above -
// the exact chain a real reload of /canvas runs.
const J = (body) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
globalThis.fetch = (path) => {
  const p = String(path);
  if (p.startsWith("/auth/me")) {
    return J({ enabled: true, signed_in: true, name: "Owner", email: "owner@example.com",
               oid: "oid-owner", google_enabled: false, linked: [] });
  }
  if (p.startsWith("/router/manifest")) return J({ manifest: MANIFEST });
  if (p.startsWith("/router/compose")) return J(compose);
  if (p.startsWith("/admin/sources")) return J([]);
  if (p.startsWith("/config")) return J({ env: [] });
  return J({});
};

// ---- drive it ------------------------------------------------------------------------------
const { mountCanvas } = await import(pathToFileURL(canvasPath).href);
mountCanvas(document.getElementById("root"));

const settle = () => new Promise((r) => setTimeout(r, 0));
for (let i = 0; i < 60; i++) await settle();     // boot -> manifest -> composeUp -> renderAll

const txt = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : null);

// Per node, as the reader meets it: which store, what the dot's tooltip says, and what (if
// any) visible reason element the card carries.
const nodes = [...document.querySelectorAll(".node")].map((el) => {
  const reasonEl = el.querySelector(".nreason");
  const warnEl = el.querySelector(".nwarn");      // #808
  return {
    id: txt(el.querySelector(".nid")),
    dotTitle: el.querySelector(".status")?.getAttribute("title") ?? null,
    reasonText: reasonEl ? txt(reasonEl) : null,
    reasonTitle: reasonEl ? reasonEl.getAttribute("title") : null,
    warnText: warnEl ? txt(warnEl) : null,
    warnTitle: warnEl ? warnEl.getAttribute("title") : null,
  };
});

// Injection evidence, gathered the #786 way: not "does it look escaped" but "what elements
// and handlers did the hostile string actually create". innerHTML first, textContent second.
const world = document.getElementById("root");
const injected_imgs = [...world.querySelectorAll(".node img, #statusbar img")].length;
const handler_attrs = [];
for (const el of world.querySelectorAll(".node *, #statusbar *")) {
  for (const a of el.attributes || []) {
    if (/^on/i.test(a.name)) handler_attrs.push(`${el.tagName.toLowerCase()}@${a.name}`);
  }
}

console.log(JSON.stringify({
  scenario,
  nodes,
  statusbar: txt(document.getElementById("statusbar")),
  composeBtn: txt(document.getElementById("compose")),   // #808: "N live · M needs attention"
  injected_imgs,
  handler_attrs,
}, null, 1));
process.exit(0);
