// src/dbsearch/server/static/js/ui/rail.js
//
// The ONE navigation rail (#413).
//
// Ask/Chat/Draft used to sit in a left sidebar on the shell and in a top cluster
// on the canvas, so the navigation moved when you moved between surfaces. This is
// the single definition of what the navigation IS.
//
// #643: it is no longer "shared by two documents" - there is one document. The canvas
// became a surface of the shell, so this rail is mounted exactly once, by main.js. The
// plain-DOM style with no imports is kept anyway; nothing here needs more, and it is what
// let this file be the shared definition through the years when there were two front-ends.

const COLLAPSE_KEY = "dbsearch_rail_collapsed";

/** The class on the rail's root <nav>, and the ONE definition of it (#865).
 *
 * main.js mounts the rail at most once, and expressed that as
 * `if (!grid.querySelector(".rail"))`. Nothing in the tree has ever carried the class `rail` -
 * the root is `navrail`, and the only `rail`-prefixed classes are `rail-thread`,
 * `rail-slot-head`, `rail-new`. A class selector matches whole tokens, so `.rail` matched
 * nothing, the condition was always true, and the guard read as "mount at most once" while
 * meaning nothing at all.
 *
 * It never fired because `boot()` runs once per document. That is a fact about the caller,
 * not about the guard, and it is exactly the kind of fact that changes quietly - a retry on a
 * failed /config, a re-init after sign-in. Two rails would then both match `.navrail-slot`,
 * and `railSlot()` returns the FIRST: Ask would write its conversation list into a rail that
 * is not the one on screen.
 *
 * Not exported on its own: `railMounted` below is what callers ask, so no caller has to hold
 * a class name at all. A dead guard is worse than no guard, because the next reader takes it
 * as proof the case is handled. */
const RAIL_CLASS = "navrail";

/** Is a rail already mounted inside `container`? (#865)
 *
 * THE PREDICATE LIVES HERE, not at the call site, because the call site got it wrong and
 * nothing could tell. main.js asked `container.querySelector(".rail")` - a class no element in
 * the tree has ever carried - so it always answered "no rail here" and the mount was
 * unguarded. Any check phrased in terms of a class name is a second place that has to know how
 * `renderRail` builds its root, and that is precisely the pair that drifted.
 *
 * A caller now asks a question instead of matching a string, and this file answers it with the
 * same constant it builds with. There is nothing left to keep in step. */
export function railMounted(container) {
  return !!(container && container.querySelector(`.${RAIL_CLASS}`));
}

/**
 * The navigation, in one place.
 *
 * #560: every `href` is a REAL PATH, never a fragment. When the rail was mounted by two
 * documents and only one of them ran a router, a "#/draft" item was live navigation on the
 * shell and a dead link on /canvas, where clicking Draft moved the URL to /canvas#/draft and
 * changed nothing on screen. Paths worked in both places without the rail having to know
 * which page it was on. That is still the rule, and #643 made it uniform: every item here is
 * now a shell path, so router.js intercepts all of them and no rail click is ever a document
 * load. The brand's "/" is the one link here that is not a surface, and it still navigates
 * for real - the landing page is a different thing, not a view of the workspace.
 *
 * Every path here must be one the server actually serves (SHELL_PATHS in app.py), because a
 * path link that misses is a 404, not a no-op.
 */
export const NAV = [
  { group: "Workspace" },
  { id: "ask",   label: "Ask",   href: "/ask",
    icon: '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/>' },
  // #632: Chat is GONE from here, merged into Ask rather than sitting beside it. The two
  // rendered the same backend behind two skins, so a thread begun on one was only findable
  // from the other, and no honest sentence distinguished them in this list.
  { id: "draft", label: "Draft", href: "/draft",
    icon: '<path d="M11.5 2.5 13.5 4.5 6 12H4v-2z"/><path d="M2.5 14.5h11"/>' },
  { group: "Operate" },
  // The canvas IS the connector surface (#408). #643 made it a surface of this shell, so
  // this is an in-document route like every other item here, not a link to a second page.
  { id: "canvas", label: "Connectors", href: "/canvas",
    icon: '<path d="M6 2.5v4M10 2.5v4"/><path d="M4 6.5h8v2a4 4 0 0 1-8 0z"/><path d="M8 12.5v2"/>' },
  { id: "admin", label: "Admin", href: "/admin",
    icon: '<circle cx="8" cy="8" r="2"/><path d="M8 1.5v2M8 12.5v2M14.5 8h-2M3.5 8h-2M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4M12.6 12.6l-1.4-1.4M4.8 4.8 3.4 3.4"/>' },
  { id: "developer", label: "Developer", href: "/developer",
    icon: '<path d="m5.5 5-3 3 3 3M10.5 5l3 3-3 3"/>' },
];

const CHEVRON =
  '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6.5 4 4 4-4 4"/></svg>';

function svg(paths) {
  return `<svg class="navrail-ico" viewBox="0 0 16 16" aria-hidden="true">${paths}</svg>`;
}

/**
 * Build the rail.
 *   current   - id of the surface in view, so it renders as active
 *   collapsed - start as an icon strip. DO NOT set this per surface (#556).
 *
 * The canvas used to pass `collapsed: true`, so moving between Chat and Connectors swapped a
 * 248px labelled sidebar for a 64px icon strip the user never asked for - navigation that
 * redefines itself per page, which is exactly what reads as unfinished. It also cost the
 * group headers (Workspace / Operate) and the permission note on the page visitors land on
 * first, and these icons are not learnable standalone (a plug for Connectors, a sun for
 * Admin). Every surface defaults to expanded.
 *
 * A surface that genuinely needs the room should NOT reintroduce a default here: the Collapse
 * control below persists the user's own choice and wins over this argument, so collapsing once
 * sticks everywhere. The user decides; the page does not.
 */
export function renderRail({ current = "", collapsed = false } = {}) {
  const stored = localStorage.getItem(COLLAPSE_KEY);
  const isCollapsed = stored === null ? collapsed : stored === "1";
  // #634: the reserve was stamped from localStorage before first paint; on a first-ever visit
  // there is no stored value, so reconcile it with whatever this call actually decided.
  document.documentElement.setAttribute("data-rail", isCollapsed ? "icons" : "full");

  const nav = document.createElement("nav");
  nav.className = RAIL_CLASS + (isCollapsed ? " navrail--icons" : "");
  nav.setAttribute("aria-label", "Primary");

  const brand = document.createElement("a");
  brand.className = "navrail-brand";
  brand.href = "/";
  brand.title = "DBSearch.AI";
  brand.innerHTML =
    '<span class="navrail-brand-full">DBSearch<span class="ai">.AI</span></span>' +
    '<span class="navrail-brand-short">D<span class="ai">.</span></span>';
  nav.append(brand);

  for (const item of NAV) {
    if (item.group) {
      const g = document.createElement("div");
      g.className = "navrail-group";
      g.textContent = item.group;
      nav.append(g);
      continue;
    }
    const a = document.createElement("a");
    a.className = "navrail-item" + (item.id === current ? " active" : "");
    a.href = item.href;
    a.dataset.railId = item.id;
    // The label is the accessible name when collapsed, so the title is the only
    // thing a mouse user gets and the text still reaches a screen reader.
    a.title = item.label;
    a.innerHTML = `${svg(item.icon)}<span class="navrail-label">${item.label}</span>`;
    nav.append(a);
  }

  // #631: THE ONE REGION A SURFACE MAY FILL WITH ITS OWN CONTENT. Ask puts its conversation
  // list here; Connectors, Admin and Developer leave it empty and render exactly as before.
  //
  // It sits BETWEEN the nav items and the foot, which is what keeps rule 6 ("navigation never
  // moves") literally true: no item changes position, order or destination because of it. What
  // changes is that the rail can carry content beneath the navigation - which is the shape the
  // owner asked for, and the shape every product with a thread list has.
  //
  // The rail does not know or care what goes in here, and must not - it is the navigation,
  // not a component of whichever surface happens to be on screen. Surfaces write into it
  // through `railSlot()` and the router empties it on every route change, so a surface's
  // content cannot outlive the surface that put it there.
  const slot = document.createElement("div");
  slot.className = "navrail-slot";
  nav.append(slot);

  const foot = document.createElement("div");
  foot.className = "navrail-foot";
  const note = document.createElement("div");
  note.className = "navrail-note";
  note.textContent =
    "Every answer is trimmed to what the signed-in user is allowed to see. " +
    "Permission-faithful by construction.";
  foot.append(note);

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "navrail-toggle";
  const paint = () => {
    const c = nav.classList.contains("navrail--icons");
    toggle.innerHTML = CHEVRON + `<span class="navrail-label">${c ? "Expand" : "Collapse"}</span>`;
    toggle.title = c ? "Expand navigation" : "Collapse navigation";
    toggle.setAttribute("aria-expanded", String(!c));
    toggle.querySelector("svg").style.transform = c ? "" : "rotate(180deg)";
  };
  toggle.addEventListener("click", () => {
    const c = nav.classList.toggle("navrail--icons");
    localStorage.setItem(COLLAPSE_KEY, c ? "1" : "0");
    // #634: keep the pre-paint reserve in step with the live state. The <head> script reads
    // this same key on the NEXT load to hold the column open at the right width; without
    // this line a user who collapses the rail would still get a 248px band on their next
    // navigation and watch it snap to 60px.
    document.documentElement.setAttribute("data-rail", c ? "icons" : "full");
    paint();
  });
  paint();
  foot.append(toggle);
  nav.append(foot);

  return nav;
}

/** The rail's surface-content slot (#631), or null on a page whose rail is not mounted.
 *
 * Returning null rather than throwing is deliberate: the canvas mounts this rail too, and a
 * surface that asks for the slot on a page that has none should render without a thread list,
 * not blow up the page. */
export function railSlot() {
  return document.querySelector(".navrail-slot");
}

/** Mark one item active. The shell calls this on every hash route change. */
export function setRailActive(current) {
  document.querySelectorAll(".navrail-item").forEach((a) => {
    a.classList.toggle("active", a.dataset.railId === current);
  });
}
