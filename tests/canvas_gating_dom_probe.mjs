// #823 - adding a source is gated on the sign-in / provider-link state, with an AFFORDANCE
// rather than a greyed-out row (owner ruling 260818: "greyed out is so ugly, bad UX").
//
// The rule has three layers:
//   1. real login configured but nobody signed in  -> every provider flyout offers sign-in
//      instead of its services. Gated on isDemoMode(), NOT on !signed_in: a dev rig has
//      signed_in permanently false and drives identity through X-DBSearch-User, so gating on
//      signed_in would break every dev rig (the load-bearing comment at openUploadPicker).
//   2. signed in, provider not linked -> that provider's flyout offers "Connect your <x>"
//      instead of its services. Link state is /auth/me's `linked`, which is the vault's own
//      answer and refuses to report a credential it cannot decrypt.
//   3. Files & Links needs only an account, so its own kinds are never link-gated.
//
// Scenarios (each runs in its own process, with its own /auth/me body):
//   signed_out          - layer 1: every provider, including Files, offers sign-in.
//   unlinked            - layer 2+3: cloud providers offer Connect, Files offers services.
//   linked              - the control an over-broad gate fails: everything opens normally.
//   dev_rig             - no real login configured: nothing is gated (the dev-rig rule).
//
// Same jsdom harness as canvas_delegation_dom_probe.mjs (#809/#814).
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

// ---- the identity under test -------------------------------------------------------------
const AUTH = {
  // real login configured, nobody signed in -> isDemoMode() is true
  signed_out: { enabled: true, google_enabled: true, aws_enabled: true, signed_in: false,
                name: "", email: "", oid: "", linked: [] },
  // signed in, nothing linked at all (exactly bob-test on prod today)
  unlinked: { enabled: true, google_enabled: true, aws_enabled: true, signed_in: true,
              name: "Bob", email: "bob@example.com", oid: "oid-bob", linked: [] },
  // signed in with every credential vaulted (the over-broad-gate control)
  linked: { enabled: true, google_enabled: true, aws_enabled: true, signed_in: true,
            name: "Bob", email: "bob@example.com", oid: "oid-bob",
            linked: ["entra", "google", "aws"] },
  // no real login configured at all: a dev rig, which must behave exactly as before
  dev_rig: { enabled: false, google_enabled: false, aws_enabled: false, signed_in: false,
             name: "", email: "", oid: "", linked: [] },
  // #949: signed in, nothing linked - same identity as `unlinked`, used to drive the
  // click-a-gated-tile routing check below.
  click_gated: { enabled: true, google_enabled: true, aws_enabled: true, signed_in: true,
                 name: "Bob", email: "bob@example.com", oid: "oid-bob", linked: [] },
}[scenario];
if (!AUTH) throw new Error(`unknown scenario ${scenario}`);

let serverStores = [];
const J = (body, status = 200) =>
  Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });

globalThis.fetch = (path, opts = {}) => {
  const p = String(path);
  const m = (opts.method || "GET").toUpperCase();
  if (p.startsWith("/auth/me")) return J(AUTH);
  if (m === "PUT" && p.startsWith("/router/manifest")) return J({ saved: true, stores: 0 });
  if (p.startsWith("/router/manifest"))
    return J({ manifest: { tenant: "acme", stores: serverStores } });
  if (p.startsWith("/router/compose")) return J({ stores: [], skipped: [] });
  if (p.startsWith("/router/demo")) return J({ manifest: { tenant: "demo", stores: [] } });
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

// Rows are identified by their VISIBLE label, so this probe reads the same thing a user does
// and does not depend on any attribute the fix happens to add.
// #920: "Microsoft 365" is gone - SharePoint was its only kind and moved into the renamed
// "Files & Links" row, so a row here would be advertising nothing.
const LABELS = { azure: "Azure", google: "Google Cloud", aws: "AWS",
                 files: "Files & Links" };

const out = { scenario, providers: {} };
for (const [key, label] of Object.entries(LABELS)) {
  const row = [...document.querySelectorAll("#rail .prov")]
    .find((r) => r.querySelector(".pn b")?.textContent.trim() === label);
  if (!row) { out.providers[key] = { present: false }; continue; }
  row.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle(5);
  const menu = document.getElementById("provmenu");
  const cta = menu.querySelector(".gate-cta");
  out.providers[key] = {
    present: true,
    // #920: what this caller can actually ADD. A row may now mix addable and gated tiles,
    // and counting a gated tile as an offer would let the #551 defect back in through the
    // side door - the count has to mean the same thing it meant when the whole row gated.
    svcCount: menu.querySelectorAll(".svc:not(.gated)").length,
    gatedSvcCount: menu.querySelectorAll(".svc.gated").length,
    ctaText: cta ? cta.textContent.trim() : null,
    ctaHref: cta ? (cta.getAttribute("href") || "") : null,
    gateText: menu.querySelector(".gate-msg")?.textContent.trim() || null,
    // #949: the services are now REVEALED even on a fully-gated row, so the probe reports the
    // three sets a person distinguishes - what is listed, what they can add, what is gated -
    // and whether the connect action sits in a BANNER above them rather than replacing them.
    kinds: [...menu.querySelectorAll(".svc")].map((e) => e.dataset.kind),
    addable: [...menu.querySelectorAll(".svc:not(.gated)")].map((e) => e.dataset.kind),
    gatedKinds: [...menu.querySelectorAll(".svc.gated")].map((e) => e.dataset.kind),
    bannerCta: !!menu.querySelector(".gate-banner .gate-cta"),
    // the row itself must NOT be dimmed into a disabled look (the rejected UX)
    rowGreyed: row.classList.contains("disabled") || row.hasAttribute("disabled"),
  };
}

// #949: clicking a gated tile must ROUTE to connect (open the account panel or follow a link),
// never add a node that could only 403. This runs only for the click scenario so the ordinary
// reveal scenarios stay side-effect-free.
if (scenario === "click_gated") {
  let connectReached = false;
  const origHref = Object.getOwnPropertyDescriptor(window.location, "href");
  try {
    Object.defineProperty(window.location, "href",
      { set() { connectReached = true; }, get() { return "http://localhost/canvas"; },
        configurable: true });
  } catch { /* jsdom may refuse; the account-panel path below still proves routing */ }
  // an account button whose click is the connect affordance for a form-vaulted provider
  const acct = document.createElement("button");
  acct.addEventListener("click", () => { connectReached = true; });
  const holder = document.createElement("div");
  holder.id = "account"; holder.appendChild(acct); document.body.appendChild(holder);

  const beforeNodes = document.querySelectorAll(".node").length;
  const gRow = [...document.querySelectorAll("#rail .prov")]
    .find((r) => r.querySelector(".pn b")?.textContent.trim() === "AWS");
  gRow.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle(5);
  const gatedTile = document.querySelector("#provmenu .svc.gated");
  if (gatedTile) {
    gatedTile.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await settle(5);
  }
  out.click = { tileFound: !!gatedTile, connectReached,
                nodesAdded: document.querySelectorAll(".node").length - beforeNodes };
  if (origHref) Object.defineProperty(window.location, "href", origHref);
}

console.log(JSON.stringify(out, null, 1));
process.exit(0);
