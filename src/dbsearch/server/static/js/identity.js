// src/dbsearch/server/static/js/identity.js
//
// #630: this file used to build the WHOLE topbar identity chip - three branches covering dev
// auth, signed-in prod and anonymous prod. Two of those moved into ui/account.js, which
// answers a question this one never could: signed in is not the same as connected, and the
// user could not tell the two apart.
//
// What is left is the one thing that genuinely belongs here: the DEV-AUTH IDENTITY SWITCHER.
// It is not an account control - there is no account - it is a test affordance for choosing
// which fake user the next request claims to be, and it exists only when the server says dev
// auth is on. Keeping it separate is deliberate: nothing in ui/account.js can ever set a
// caller identity, so the file that talks about who you are has no power to change it.
import { getConfig, setUser, getUser } from "./api.js";

/** Fetch only. It used to render as well, and that mattered: the account control paints the
 *  dropdown (and with it the slot this switcher goes in), so rendering during the fetch step
 *  put the selector on screen a moment before `renderAccount` cleared its host. Two steps,
 *  in an order the caller can see. */
export async function loadConfig() {
  return getConfig();
}

/** The dev-auth switcher, mounted AFTER the account control so its slot exists. */
export function mountDevSwitcher(cfg) {
  // Inside the account dropdown when there is one, falling back to the topbar host - a dev
  // rig with no session still needs the selector, and it is the only control on that page
  // that decides who you are.
  const slot = document.getElementById("acct-dev-slot")
            || document.getElementById("account");

  if (cfg.dev_auth && cfg.users.length && slot) {
    slot.innerHTML = "";
    slot.append(Object.assign(document.createElement("span"),
      { className: "acct-dev-label", textContent: "Searching as" }));
    const sel = document.createElement("select");
    sel.id = "user-select";
    for (const u of cfg.users) sel.append(new Option(u, u));
    sel.value = cfg.users[0];
    setUser(cfg.users[0]);
    sel.addEventListener("change", () => setUser(sel.value));
    slot.append(sel);
  } else {
    // Prod: identity comes from the verified session token, never from a client-set header.
    // ui/account.js renders WHO from /auth/me; this only makes sure nothing here is claiming
    // to be somebody.
    setUser(null);
  }
}

export { getUser };
