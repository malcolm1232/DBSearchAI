// src/dbsearch/server/static/js/main.js
import { loadConfig, mountDevSwitcher } from "./identity.js";
import { setModel, authMe } from "./api.js";
import { normalizeLegacyRoute, startRouter } from "./router.js";
import { initShell } from "./login.js";


// #43: generation-model picker in the topbar. Dropdown when >1 model is available
// (e.g. memory-ollama: llama3.2:3b vs llama3.1:8b), else a muted pill showing the model.
function buildModelPick(cfg) {
  const slot = document.getElementById("model-pick");
  const models = cfg.chat_models || [];
  const disabled = cfg.disabled_models || [];   // #67 greyed-out, non-selectable placeholders
  if (!slot || models.length === 0) return;
  setModel(cfg.chat_model);
  slot.textContent = "";
  slot.append(Object.assign(document.createElement("span"),
    { className: "model-label", textContent: "Model" }));
  if (models.length < 2 && disabled.length === 0) {
    slot.append(Object.assign(document.createElement("span"),
      { className: "model-static", textContent: cfg.chat_model }));
    return;
  }
  const sel = document.createElement("select");
  sel.id = "model-select";
  for (const m of models) sel.append(new Option(m, m));
  for (const m of disabled) {                   // unselectable, greyed by the browser + CSS
    const o = new Option(m, m);
    o.disabled = true;
    sel.append(o);
  }
  sel.value = cfg.chat_model;
  sel.addEventListener("change", () => setModel(sel.value));
  slot.append(sel);
}

async function boot() {
  // #560: FIRST, before anything reads the location. A pre-#560 "/#/chat" bookmark has to
  // become "/chat" while it is still only a URL - initShell decides landing-vs-app from the
  // path, so normalising later would show the landing page with Chat mounted behind it.
  normalizeLegacyRoute();
  const cfg = await loadConfig();              // /config only - rendering happens below
  const pill = document.getElementById("edition-pill");
  pill.textContent = "";
  pill.append(Object.assign(document.createElement("span"), { className: "pulse" }));
  pill.append(document.createTextNode(`${cfg.edition} · ${cfg.backend}`));
  buildModelPick(cfg);
  // #413: the ONE navigation, shared with the canvas. Inserted before <main> so it
  // lands in the app-grid's "sidebar" area.
  // #415: dynamic so the URL can carry the build id. A static specifier cannot, and
  // an unversioned ./ui/rail.js is exactly the cached-module hole that removed the
  // navigation from a warm browser.
  const v = window.DBS_BUILD_ID ? `?v=${window.DBS_BUILD_ID}` : "";   // #557
  // #865: `railMounted` comes from rail.js because the check below has to agree with what
  // renderRail actually builds, and it did not - this asked for `.rail` while the rail root
  // is `.navrail`, so it matched nothing and the mount was never actually guarded. Asking
  // rail.js the question leaves no class name here to drift.
  const { renderRail, railMounted } = await import(`./ui/rail.js${v}`);
  // #630: the account control. Dynamic for the same #415 reason as the rail - a static
  // specifier cannot carry the build id, and an unversioned module URL is how a warm cache
  // keeps serving a stale copy forever.
  //
  // `authMe()` returns null on failure rather than throwing (api.js), and that null is passed
  // through DELIBERATELY: renderAccount draws nothing at all for it. "Not signed in" is a
  // claim about the user, and a page that could not reach /auth/me has not earned it - which
  // is the other half of #373, where the shell asserted the opposite without checking.
  const { renderAccount } = await import(`./ui/account.js${v}`);
  renderAccount(document.getElementById("account"), await authMe(), cfg);
  // AFTER the control, because the dev switcher lives in a slot the dropdown creates. The
  // other order painted the selector and then cleared it.
  mountDevSwitcher(cfg);
  const grid = document.getElementById("view-app");
  const surface = document.getElementById("surface");
  if (grid && surface && !railMounted(grid)) {
    grid.insertBefore(renderRail({ current: "ask" }), surface);
  }
  initShell(cfg);                              // theme + demo login-gate + landing; picks initial view
  startRouter(surface);
}

boot().catch((err) => {
  console.error("[boot]", err);
  const s = document.getElementById("surface");
  if (s) s.textContent = "Failed to load. Is the server running?";
});
