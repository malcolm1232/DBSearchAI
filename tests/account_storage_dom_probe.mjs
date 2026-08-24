// #775 - the storage/upgrade section of the account panel.
//
// The failure worth guarding is drawing a control that cannot work: an Upgrade button on a
// deployment with no Stripe keys is #551's always-fails tile with a card number attached, and
// a usage bar drawn at 0% for a backend that cannot meter is a confident lie about how full
// somebody is.
//
// Scenarios:
//   sellable        - hosted, metered, tiers for sale: bar + plan + one Upgrade per OTHER tier
//   self_host       - metered but nothing sellable: usage shown, NO upgrade control anywhere
//   unmetered       - billing on, backend cannot meter: offers exist, NO bar (null != zero)
//   already_pro     - on the top tier: Manage billing, and never "upgrade to pro" again
//   billing_down    - /billing/status fails: the section is absent and the roster still paints
import { pathToFileURL } from "node:url";

const [, , jsdomPath, accountPath, scenario] = process.argv;
const { JSDOM } = await import(pathToFileURL(jsdomPath).href);

const dom = new JSDOM("<!doctype html><html><body><div id='host'></div></body></html>",
                      { url: "http://localhost/canvas" });
const { window } = dom;
for (const k of ["document", "window", "location", "HTMLElement", "Node", "Event",
                 "CustomEvent", "getComputedStyle", "MouseEvent", "KeyboardEvent"]) {
  Object.defineProperty(globalThis, k, { value: window[k], configurable: true, writable: true });
}
Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true, writable: true });
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });

const GB = 1024 ** 3;
const STATUS = {
  sellable: { tier: "free", quota_bytes: 10 * GB, used_bytes: 3 * GB, metered: true,
              sellable: [{ name: "plus", quota_gb: 50, price_cents: 99 },
                         { name: "pro", quota_gb: 1024, price_cents: 899 }] },
  self_host: { tier: "free", quota_bytes: 10 * GB, used_bytes: 1 * GB, metered: true,
               sellable: [] },
  unmetered: { tier: "free", quota_bytes: 10 * GB, used_bytes: null, metered: false,
               sellable: [{ name: "plus", quota_gb: 50, price_cents: 99 }] },
  // 980 of 1024 GB = 95.7%, which is PAST the 90% line where the bar turns amber. 900GB was
  // the first fixture here and sits at 88%, so it quietly tested the ordinary path while
  // claiming to test the nearly-full one.
  already_pro: { tier: "pro", quota_bytes: 1024 * GB, used_bytes: 980 * GB, metered: true,
                 sellable: [{ name: "plus", quota_gb: 50, price_cents: 99 },
                            { name: "pro", quota_gb: 1024, price_cents: 899 }] },
  billing_down: null,
}[scenario];
if (STATUS === undefined) throw new Error(`unknown scenario ${scenario}`);

globalThis.fetch = (path) => {
  const p = String(path);
  if (p.startsWith("/billing/status")) {
    return Promise.resolve(STATUS === null
      ? { ok: false, status: 500, json: () => Promise.resolve({}) }
      : { ok: true, status: 200, json: () => Promise.resolve(STATUS) });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
};

const mod = await import(pathToFileURL(accountPath).href);
const settle = async (n = 40) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };

const host = document.getElementById("host");
mod.renderAccount(host, {
  signed_in: true, name: "Bob", email: "bob@example.com", oid: "oid-bob",
  enabled: true, google_enabled: true, aws_enabled: true, linked: ["entra"],
}, {});
await settle();

const sec = host.querySelector(".acct-storage");
const buttons = [...host.querySelectorAll(".acct-storage-act .acct-connect")]
  .map((b) => b.textContent.trim());
console.log(JSON.stringify({
  scenario,
  present: !!sec && !sec.hidden,
  line: sec?.querySelector(".acct-storage-line")?.textContent.trim() || null,
  hasBar: !!sec?.querySelector(".acct-bar"),
  barPct: sec?.querySelector(".acct-bar")?.getAttribute("aria-valuenow") || null,
  barFull: !!sec?.querySelector(".acct-bar.acct-bar-full"),
  plan: sec?.querySelector(".acct-storage-plan")?.textContent.trim() || null,
  buttons,
  // the roster must survive whatever billing does
  rosterRows: host.querySelectorAll(".acct-provider").length,
}, null, 1));
process.exit(0);
