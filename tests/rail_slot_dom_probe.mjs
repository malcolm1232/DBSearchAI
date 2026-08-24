// Mounts the REAL shared rail in a real DOM (jsdom) and reports what the slot does (#631).
//
// The rail is shared by the app shell and the canvas, so the properties worth proving are
// structural: the slot exists, it sits BETWEEN the nav items and the foot, the nav itself is
// unchanged by its arrival, and the accessor finds it.
//
//   node tests/rail_slot_dom_probe.mjs <jsdom/lib/api.js> <rail.js>
import { pathToFileURL } from "node:url";

const [, , jsdomPath, railPath] = process.argv;
const { JSDOM } = await import(pathToFileURL(jsdomPath).href);

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
// The rail persists the user's collapse choice; jsdom has no localStorage by default and the
// rail must not depend on one existing to render at all.
globalThis.localStorage = {
  _v: {},
  getItem(k) { return Object.prototype.hasOwnProperty.call(this._v, k) ? this._v[k] : null; },
  setItem(k, v) { this._v[k] = String(v); },
};

const { renderRail, railSlot, NAV, railMounted } = await import(pathToFileURL(railPath).href);

const nav = renderRail({ current: "ask" });
document.body.append(nav);

// #865: THE IDEMPOTENCE CHECK AND THE THING IT CHECKS MUST AGREE.
//
// main.js mounted the rail behind `if (!grid.querySelector(".rail"))` - a class no element in
// the tree carries, since the root is `navrail` and a class selector matches whole tokens. So
// the condition was always true and the mount was never guarded at all.
//
// Drives the REAL exported predicate, not a copy of it. An earlier version of this probe
// re-implemented main.js's `if` inline, which proved the rail's own class was self-consistent
// and would have stayed green if main.js went back to ".rail" - testing around the defect
// instead of at it. `railMounted` is now the only phrasing of the question that exists.
const grid = document.createElement("div");
document.body.append(grid);
const mountStep = () => {
  if (!railMounted(grid)) grid.append(renderRail({ current: "ask" }));
};
mountStep();
const railsAfterOne = grid.querySelectorAll("nav").length;
mountStep();
const railsAfterTwo = grid.querySelectorAll("nav").length;
// The old selector, kept as a CONTROL so the probe reports what it would have found. It must
// find nothing - that is the defect, stated as a measurement rather than as a memory of it.
const deadSelectorMatches = grid.querySelectorAll(".rail").length;

const children = [...nav.children].map((c) => (c.className || "").split(" ")[0]);
const slot = railSlot();

// A surface fills it, the router empties it. Prove both are possible on the real node.
let filled = false;
let emptied = false;
if (slot) {
  const row = document.createElement("button");
  row.className = "rail-thread";
  row.textContent = "a thread";
  slot.append(row);
  filled = document.querySelectorAll(".navrail-slot .rail-thread").length === 1;
  document.querySelector(".navrail-slot").innerHTML = "";
  emptied = document.querySelectorAll(".navrail-slot .rail-thread").length === 0;
}

console.log(JSON.stringify({
  children,
  slot_found: slot !== null,
  slot_is_in_the_rail: !!(slot && nav.contains(slot)),
  slot_index: children.indexOf("navrail-slot"),
  foot_index: children.indexOf("navrail-foot"),
  brand_index: children.indexOf("navrail-brand"),
  last_item_index: children.lastIndexOf("navrail-item"),
  nav_ids: NAV.filter((n) => n.id).map((n) => n.id),
  nav_hrefs: NAV.filter((n) => n.id).map((n) => n.href),
  rendered_item_ids: [...nav.querySelectorAll(".navrail-item")]
    .map((a) => a.dataset.railId),
  active_id: nav.querySelector(".navrail-item.active")?.dataset.railId || "",
  filled,
  emptied,
  // #865
  rendered_root_class: (nav.className || "").split(" ")[0],
  rail_mounted_finds_a_real_rail: railMounted(grid),
  rail_mounted_on_empty: railMounted(document.createElement("div")),
  rails_after_one_mount: railsAfterOne,
  rails_after_two_mounts: railsAfterTwo,
  dead_selector_matches: deadSelectorMatches,
}));
