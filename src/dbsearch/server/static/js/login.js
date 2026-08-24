// src/dbsearch/server/static/js/login.js
// Landing, initial theme, and the hand-off into the app. There is NO password gate here: #329 removed
// the localStorage demo card, which printed its own credentials and let every path through it
// reach /canvas regardless. Authentication is a backend concern (user_auth.py / google_auth.py);
// this file only decides which view to show. #346 removed the landing's "Sign in" buttons, so
// it no longer routes to an IdP at all - the canvas owns that.
const THEME_KEY = "dbsearch_theme";

// #638: ONE mechanism. This used to toggle the `hidden` attribute, which meant the document
// shipped with BOTH views hidden and stayed blank until this ran - after loadConfig()'s fetch.
// The <head> script now stamps `data-view` before first paint and CSS shows the right one, so
// by the time this is called it is usually confirming a decision already made. It still has a
// job: the landing's buttons and any future in-page view switch go through here.
export function showView(name) {
  document.documentElement.setAttribute("data-view", name);
  window.scrollTo(0, 0);
}

// #630: APPLYING the stored theme stays here; TOGGLING it moved into the account control
// (ui/account.js), along with Sign out. Both were separate topbar buttons and neither
// answered the question the user actually had. The apply must still happen on every boot and
// before first paint - it is what stops a dark-theme user getting a white flash on load - and
// it has to run on pages that have no account control at all.
function applyStoredTheme() {
  document.documentElement.setAttribute("data-theme",
    localStorage.getItem(THEME_KEY) || "light");
}

// #346 removed the landing's sign-in buttons ("nothing to sign in to yet"); #386 brought
// ONE back, because the self-serve funnel now exists end to end and a returning user had
// no path back in. #446 points it at /signin rather than /auth/login: jumping straight to
// the IdP showed an unverified-publisher consent screen with no context.
function wireLanding() {
  const on = (id, ev, fn) => { const e = document.getElementById(id); if (e) e.addEventListener(ev, fn); };

  // #309: the landing hands off to /canvas — the product. It used to swap to the app
  // shell in place, which is why "Launch demo" landed on #/ask with the URL unchanged.
  // The signed-in path needs nothing here: /auth/callback already returns to /canvas.
  on("lp-demo", "click", () => { location.href = "/canvas"; });
  on("lp-demo-2", "click", () => { location.href = "/canvas"; });
  on("lp-signin", "click", () => { location.href = "/signin"; });

  // #592's sign-out lives in the account control now (ui/account.js), which kept its rule
  // verbatim: end the session for real, and report failure ON the control - navigating away
  // while still signed in looks exactly like success.
}

// The shell surfaces that keep their own URL (#309) — must match SHELL_PATHS in app.py.
// Exported because router.js needs the same list to decide which rail clicks it may
// handle in-document (#560); a second copy there is a second thing to forget to update.
//
// `/c` IS DELIBERATELY ABSENT (#605 task 12), and this note exists so the next reader does not
// "fix" it. The link doorway is not an app-shell surface: it serves its own document
// (static/visitor.html, mounted by js/visitor.js), because a visitor has no account and
// therefore no rail, no model picker, no Connectors/Admin/Developer and no "New conversation".
// Two hand-maintained copies of one fact is a known hazard here, so selftest_nav_shell.py now
// asserts this set and app.py's tuple are EQUAL in both directions rather than only that
// Python's entries appear here - neither list can grow a path the other lacks.
//
// `/chat` IS DELIBERATELY ABSENT TOO (#632), for a different reason than `/c`: it is not a
// separate document, it is a MERGED one. Ask and Chat rendered the same backend twice, so a
// thread begun on Chat was only findable from Ask. GET /chat now 308s to /ask server-side,
// which is why it must not be in this set: a path listed here is one the shell claims it can
// render in-document, and router.js would then intercept the click and mount nothing.
//
// `/canvas` IS PRESENT AS OF #643, and used to be the third deliberate absence here. It was
// absent for the same reason `/c` is: it served its own document. That stopped being true when
// canvas.html became js/surfaces/canvas.js - the shell renders Connectors itself now, so a
// click on it must be intercepted like any other surface. Leaving it out was exactly what made
// Connectors a full page reload while Ask and Draft were a tab switch.
export const SHELL_PATHS = new Set(["/app", "/ask", "/draft", "/admin", "/developer", "/canvas"]);

// Decide the initial view from the PATH, not from cfg.dev_auth (#309). The old rule —
// auto-enter the app whenever dev auth was on — meant "/" never showed the landing on a
// dev rig, so "Launch demo" appeared to dump you on #/ask. Now "/" is always the landing
// and the shell surfaces enter the app directly at their own URLs.
export function initShell(cfg) {
  applyStoredTheme();
  wireLanding();
  const path = location.pathname.replace(/\/+$/, "") || "/";
  showView(SHELL_PATHS.has(path) ? "app" : "landing");
}
