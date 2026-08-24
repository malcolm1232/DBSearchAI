// src/dbsearch/server/static/js/router.js
import { SHELL_PATHS } from "./login.js";
import { mountAsk } from "./surfaces/ask.js";
import { mountAdmin } from "./surfaces/admin.js";
import { mountDraft } from "./surfaces/draft.js";
import { mountDeveloper } from "./surfaces/developer.js";
import { mountCanvas } from "./surfaces/canvas.js";


// #643: "canvas" is a real surface here now, not a redirect. surfaces/connectors.js is gone
// with it - it existed only to bounce the legacy #/connectors route at the /canvas DOCUMENT,
// and there is no longer a document to bounce to. LEGACY_HASH_TARGET below still heals the old
// fragment to /canvas, which the router now renders itself.
const ROUTES = {
  "ask": mountAsk,
  "canvas": mountCanvas,
  "admin": mountAdmin,
  "draft": mountDraft,
  "developer": mountDeveloper,
};

// Where a pre-#560 fragment should land. Every shell surface maps to its own path; the
// odd ones out are #/connectors, which predates Connectors becoming the canvas (#408), and
// #/chat, which #632 merged INTO Ask - an old "#/chat" bookmark heals straight to /ask
// rather than to a path that would only redirect there anyway.
const LEGACY_HASH_TARGET = {
  ask: "/ask", chat: "/ask", draft: "/draft", admin: "/admin",
  developer: "/developer", connectors: "/canvas",
};

const firstSegment = (p) => p.replace(/^\/+|\/+$/g, "").split("/")[0];

// #309: the shell is served at /ask, /chat, /draft, /admin and /developer as well as
// /app, so the PATH names the surface. /app has no surface of its own and falls through
// to the "ask" default.
//
// #560: the path is now the ONLY thing consulted. It used to be the fallback, with the
// hash winning whenever one was present - and because the rail linked fragments, clicking
// Chat from /ask produced /ask#/chat: a URL whose path and hash disagreed, where the path
// was the lie. Legacy fragments are dealt with once, up front, by normalizeLegacyRoute().
function currentRoute() {
  return firstSegment(location.pathname);
}

/**
 * Rewrite a pre-#560 "#/x" URL to the path that surface now lives at, before anything
 * reads the location. Old links, bookmarks and anything a user pasted somewhere keep
 * working, and the URL heals itself instead of leaving a fragment behind as a second,
 * contradicting source of truth.
 *
 * The fragment wins here, and ONLY here: that is what it meant when the link was minted.
 *
 * Must run before login.js decides landing-vs-app from the path, or "/#/chat" shows the
 * landing page with Chat mounted invisibly behind it.
 */
export function normalizeLegacyRoute() {
  const m = /^#\/([A-Za-z]+)/.exec(location.hash);
  if (!m) return;
  const target = LEGACY_HASH_TARGET[m[1].toLowerCase()];
  // An unknown fragment is not a route we ever had. Drop it rather than guess a path
  // the server does not serve - a bad guess turns a stale link into a 404 on reload.
  const path = target || location.pathname;
  history.replaceState({}, "", path + location.search);
}

export function startRouter(root) {
  // #643: what the surface on screen asked us to run before the next one mounts.
  //
  // Until Connectors moved in-document, wiping innerHTML was a complete teardown: every
  // surface's state lived in the nodes being thrown away. The canvas is the first that
  // does not - it hangs an Escape handler, a resize handler and a capture-phase pointerdown
  // handler off window/document, watches data-theme, and polls a SharePoint ingest. Left
  // behind, its Escape handler would reach for #spPicker on every keypress in Ask.
  //
  // A mount returns its teardown or returns nothing.
  //
  // TYPE-CHECKED, not truthiness-checked, and that distinction cost a prod deploy. `mountAdmin`
  // is `async`, so it returns a PROMISE - truthy, and not callable. Storing it and calling it
  // on the next route change threw `unmount is not a function` BEFORE the wipe and the mount,
  // so leaving Admin left the old surface frozen on screen while the URL and the rail both
  // moved. Every surface looked broken except the one you had just left.
  //
  // Nothing warned: the four older surfaces return nothing TODAY, so `|| null` happened to be
  // right for three of them and wrong for the async one, and the local pass only walked
  // Ask <-> Connectors. Any mount is free to be async or to return a value; only a function
  // is a teardown.
  let unmount = null;

  function render() {
    const name = currentRoute() || "ask";
    const mount = ROUTES[name] || ROUTES["ask"];
    const route = ROUTES[name] ? name : "ask";
    // Inlined rather than imported: router.js is loaded through main.js's module
    // graph and a static specifier here would reintroduce an unversioned URL.
    document.querySelectorAll(".navrail-item").forEach((a) => {
      a.classList.toggle("active", a.dataset.railId === route);
    });
    // #631: surface content in the rail dies with the surface that put it there. Without
    // this, Ask's conversation list would still be sitting in the rail while Admin is on
    // screen - a list of threads next to a page that cannot open one.
    const slot = document.querySelector(".navrail-slot");
    if (slot) slot.innerHTML = "";
    // Before the DOM goes, so a teardown can still find its own nodes. Guarded: a teardown
    // that throws must not take the next surface down with it - the render has to continue,
    // or one bad unmount freezes the app on the surface the user is trying to leave.
    if (unmount) {
      try { unmount(); } catch (err) { console.error("[router] teardown failed", err); }
      unmount = null;
    }
    root.innerHTML = "";
    const teardown = mount(root);
    unmount = typeof teardown === "function" ? teardown : null;
  }

  // #560: the rail links real paths, so without this every click would be a full document
  // reload - a fresh /config fetch and a re-mounted rail to move between two surfaces that
  // are already loaded. Intercept only what THIS document can render: a same-origin,
  // plain left click on a shell path. "/" is the landing - a different thing, not a view of
  // the workspace - and must navigate for real, and a modified click (new tab, download,
  // middle button) is the user asking the browser for something we are not entitled to
  // cancel. #643: /canvas used to be named here alongside "/" as the other real document;
  // it is a surface now, and it is precisely that exclusion that made Connectors reload.
  document.addEventListener("click", (e) => {
    if (e.defaultPrevented || e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a || (a.target && a.target !== "_self") || a.hasAttribute("download")) return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    const path = url.pathname.replace(/\/+$/, "") || "/";
    if (!SHELL_PATHS.has(path) || !ROUTES[firstSegment(path)]) return;
    e.preventDefault();
    if (url.pathname + url.search !== location.pathname + location.search) {
      history.pushState({}, "", url.pathname + url.search);
    }
    render();
  });

  // pushState does not fire popstate, but Back and Forward do - without this the rail
  // would move the URL and the browser buttons would silently do nothing.
  window.addEventListener("popstate", render);
  // A hand-typed or externally-linked "#/x" after load still heals to its path.
  window.addEventListener("hashchange", () => { normalizeLegacyRoute(); render(); });
  render();
}
