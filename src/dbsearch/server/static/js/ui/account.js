// src/dbsearch/server/static/js/ui/account.js
//
// #630: ONE control for two facts the product was blurring together.
//
// THE DEFECT the owner hit: /ask offers "Sign in", /canvas offers "Sign in with Microsoft",
// and they are different acts. Signing in creates a SESSION; connecting a provider vaults a
// credential DBSearch can redeem to read your documents. A user who had done the first but
// not the second had no way to tell - the topbar said a name, the canvas said sign in, and
// the two appeared to contradict each other.
//
// So the dropdown answers both questions separately and in that order: who you are, then
// what this product may read on your behalf.
//
// RULE 8 GOVERNS EVERY ROW HERE. "Never assert a state you have not verified" is not a
// slogan in this file: the shell once rendered a hardcoded "Signed in" label without checking
// for a token, so anonymous visitors were told they were signed in and then got a 401 on
// their first question (#373). Every word below comes from THIS page load's /auth/me, and the
// unreachable case renders nothing at all rather than guessing in either direction.
import { billingCheckout, billingPortal, billingStatus, connectAws,
         disconnectProvider, signOut } from "../api.js";

const THEME_KEY = "dbsearch_theme";

// #652: the cfg the control was last painted with, so a repaint after a disconnect keeps the
// dev-auth switcher slot instead of silently dropping it on a dev rig. renderAccount is the
// only writer; providerRow is the only reader.
let lastCfg = {};

// THE FIXED ROSTER (owner's decision). Every provider a user might come looking for, each
// labelled with what is true of it HERE. `enabledFlag` is the key on /auth/me that says this
// deployment can actually do it; a null flag means no implementation exists at all.
//
// FOUR STATES, FOUR SENTENCES, and blurring any two of them rebuilds the defect:
//   Connected           `linked` names it - a credential exists AND decrypts
//   Not connected       wired here, you simply have not granted it
//   Not configured here the implementation exists, this box lacks it (a client id; for
//                       Amazon, boto3 in the image)
//   Not yet supported   no implementation exists (no row today - Amazon graduated to
//                       aws_enabled under ADR 0024; the state stays for the next provider)
//
// "Not configured here" and "not yet supported" are deliberately different: one is a
// deployment's choice and the operator can change it, the other is a fact about the product
// and no amount of configuring will help. Telling an operator to go and configure something
// that does not exist is the kind of small lie that costs somebody an afternoon.
//
// #643 ADDED NO FIFTH STATE, AND THAT WAS THE FINDING. #210's stale session - the vault is
// in-memory by design while the session cookie is signed and survives a restart, so
// `signed_in` stays true after the credential is gone - used to be called out by name in the
// canvas's own auth chip ("session expired - sign in again to query"). Folding the canvas in
// meant deciding where that warning lives, and the answer turned out to be: it does not,
// because THE CLIENT CANNOT TELL. A stale vault and a sign-in that never produced a refresh
// token in the first place (app.py only vaults `if u.get("refresh_token")`) are the same
// /auth/me payload. Saying "expired" would be asserting a cause this control has not
// verified, which is the exact rule at the top of this file.
//
// So the STATE stays "Not connected" - true in both cases - and what #210 actually needed is
// carried by the ACTION instead. See providerRow.
// `connect` is where the Connect pill GOES. It used to be `/canvas` for every provider, and
// that was #646's first half: Connectors has no Microsoft grant flow at all (renderAuth builds
// from exactly one branch, `google_enabled`), so on a box with Google off the pill landed on a
// page whose auth area rendered an empty string. Not buried - absent. Google's pill "worked"
// only by accident, because that one branch happened to exist there.
//
// Each provider now points at its own linking route, which is also the only honest place to
// point: those routes require a session and hang the credential off the identity you are
// already signed in as (#193 for Google, ADR 0023 for Entra).
const ROSTER = [
  { key: "entra", name: "Microsoft", enabledFlag: "enabled", connect: "/auth/entra/link" },
  { key: "google", name: "Google", enabledFlag: "google_enabled",
    connect: "/auth/google/login" },
  // ADR 0024: the Amazon row means AWS AS A DATA SOURCE (owner ruling, #650) - the user
  // vaults their own access keys through a form, not an OAuth redirect, because AWS
  // delegated data access is IAM and no consumer OAuth reaches the data plane. `key` is
  // "aws" because that is the vault's idp name (`linked` reports it); `keyEntry` routes the
  // Connect pill to the inline form below instead of a linking URL. `aws_enabled` is
  // implementation presence (boto3 in the image), so a box that cannot validate keys says
  // "Not configured here" and never offers a form that would 501.
  { key: "aws", name: "Amazon", enabledFlag: "aws_enabled", connect: null, keyEntry: true },
];

function elx(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

// Inline hairline SVGs on a 16 grid, `stroke: currentColor`, exactly as the rail draws its
// icons (design system §6). Never emoji: they render as flat glyphs on Windows and coloured
// blobs on macOS, which is what made the old nav look like a different product per OS.
const ICON = {
  moon: '<path d="M13.6 9.8A5.8 5.8 0 0 1 6.2 2.4a5.9 5.9 0 1 0 7.4 7.4Z"/>',
  sun: '<circle cx="8" cy="8" r="3"/>'
     + '<path d="M8 1.6v1.6M8 12.8v1.6M14.4 8h-1.6M3.2 8H1.6'
     + 'M12.5 3.5l-1.1 1.1M4.6 11.4l-1.1 1.1M12.5 12.5l-1.1-1.1M4.6 4.6 3.5 3.5"/>',
  exit: '<path d="M6.5 14H3.4A1.4 1.4 0 0 1 2 12.6V3.4A1.4 1.4 0 0 1 3.4 2h3.1"/>'
      + '<path d="m10.8 11.2 3.2-3.2-3.2-3.2"/><path d="M14 8H6.2"/>',
};

function ico(paths) {
  return `<svg class="acct-ico" viewBox="0 0 16 16" aria-hidden="true">${paths}</svg>`;
}

/** Two letters a human recognises. Never the raw oid: `shortLabel` used to render an Entra
 *  oid as "a1b2c3d4…", which is a machine identifier shown to a person as if it meant
 *  something. /auth/me carries `name` and `email`; /config carries only the oid, which is
 *  why this control reads the former. */
function initials(me) {
  const src = String(me.name || me.email || "?").trim();
  const parts = src.split(/[\s@._-]+/).filter(Boolean);
  const a = (parts[0] || "?")[0] || "?";
  const b = parts[1] ? parts[1][0] : "";
  return (a + b).toUpperCase();
}

/** Which identity provider this SESSION came through, when the server said so. Absent is
 *  absent: no guess is made from the email domain, because a gmail address proves nothing
 *  about how somebody authenticated. */
function signedInWith(me) {
  const idp = String(me.idp || me.provider || "").toLowerCase();
  const named = ROSTER.find((p) => p.key === idp);
  if (named) return `Signed in with ${named.name}`;
  if (idp === "local") return "Signed in with email";
  return "Signed in";
}

/** True when this is the provider the session was authenticated THROUGH, and there is no
 *  redeemable credential for it (#210).
 *
 *  It does NOT say why - see the note above; a lost vault and a sign-in that never yielded a
 *  refresh token are indistinguishable here. It says only which REMEDY applies, and on that
 *  the two causes agree: signing in again is what mints the credential. That is a different
 *  answer from the one every other unconnected provider gets, which is why it is worth
 *  detecting at all.
 *
 *  Deliberately narrow. Only the provider you actually authenticated with - a Microsoft
 *  session with no Google grant is not a broken sign-in, it is a Google account you have
 *  never connected, and sending that user to /auth/login would be a dead end. */
function needsReSignIn(p, me) {
  const idp = String(me.idp || me.provider || "").toLowerCase();
  return Boolean(me.signed_in && p.enabledFlag && me[p.enabledFlag] &&
                 idp === p.key && !(me.linked || []).includes(p.key));
}

function providerRow(p, me) {
  const row = elx("div", "acct-provider");
  row.append(elx("span", "acct-provider-name", p.name));

  let state;
  let cls = "";
  if (!p.enabledFlag) {
    state = "Not yet supported";
    cls = "acct-dim";
  } else if ((me.linked || []).includes(p.key)) {
    // The ONLY branch that may say "Connected". `VAULT.linked()` refuses to report a
    // credential it cannot decrypt, precisely because telling a user they are connected to a
    // cloud that will fail on first use is worse than telling them nothing.
    state = "Connected";
    cls = "acct-ok";
  } else if (me[p.enabledFlag]) {
    state = "Not connected";
    // #210, via #643: warn only where the user is being told something that CONTRADICTS the
    // line above it. "Signed in with Microsoft" and "Microsoft: Not connected" are both true
    // and together they are the whole picture, but read quickly they look like a mistake -
    // and the consequence is real (every delegated ask fails with "sign in to query this
    // source"). Amber says "this row is asking you to act", nothing more.
    if (needsReSignIn(p, me)) cls = "acct-warn";
  } else {
    state = "Not configured here";
    cls = "acct-dim";
  }
  row.append(elx("span", `acct-provider-state ${cls}`.trim(), state));

  // EVERY ROW CONTRIBUTES EXACTLY THREE CELLS, and the empty one is load-bearing. The rows are
  // `display: contents` so all of them share one grid, and grid auto-placement has no idea
  // where a row ends: give one row two cells and the next row's name slides up into the hole.
  // With nobody connected the last row happened to be the short one and it looked fine; with
  // everybody connected the list folded into a two-column snake reading "Microsoft |
  // Connected | Google". A spacer is what makes the row boundary real to the grid, and
  // selftest_630 asserts the cell count so this cannot quietly come back.
  if (state === "Not connected") {
    // ONE CLICK, and it has to be the right one. For the provider this session came through,
    // the credential is minted BY signing in, so /auth/login is the fix and the grant flow on
    // Connectors is a dead end - there is nothing there to grant against. For every other
    // provider it is the reverse: the grant flow lives on the canvas, and this is a pointer
    // to it, not a second copy of it.
    const resign = needsReSignIn(p, me);
    if (p.keyEntry && !resign) {
      // ADR 0024: AWS has no linking URL to send anyone to - Connect reveals the key form
      // renderAccount parked after the roster (a BUTTON, because it changes this panel
      // rather than leaving it; an <a href> would also trip the menu's follow-a-link
      // auto-close, #647, at the exact moment the user needs the panel to stay open).
      const b = elx("button", "acct-connect", "Connect");
      b.type = "button";
      b.setAttribute("aria-expanded", "false");
      b.addEventListener("click", () => {
        const form = b.closest(".acct-menu")?.querySelector(".acct-aws");
        if (!form) return;
        form.hidden = !form.hidden;
        b.setAttribute("aria-expanded", String(!form.hidden));
        if (!form.hidden) form.querySelector("input")?.focus();
      });
      row.append(b);
      return row;
    }
    const a = elx("a", "acct-connect", resign ? "Sign in again" : "Connect");
    // #646: the provider's OWN linking route, not /canvas. `resign` still goes to /auth/login
    // because that case is a re-authentication - the credential is minted BY signing in as
    // the same identity, and there is nothing to link.
    a.href = resign ? "/auth/login" : (p.connect || "/canvas");
    // The ink pill is the house primary action, and in this list exactly one row can earn it:
    // the amber one, where a delegated ask is failing right now. "Connect" stays the quiet
    // hairline pill because it offers a capability rather than repairing a broken one. Two
    // identical pills would have said the two rows were equally urgent, which they are not.
    if (resign) a.classList.add("acct-connect-act");
    row.append(a);
  } else if (state === "Connected") {
    // #652: the undo. `TokenVault.drop` has taken a per-cloud idp since #193 and nothing ever
    // called it that way, so a row could say "Connected" with no way back - and the Google
    // callback's refusal told a stuck user to "disconnect it there first" about a control
    // that did not exist. STILL THE THIRD CELL: the rows are `display: contents` and grid
    // auto-placement has no row boundary, so this has to occupy the same slot the Connect
    // pill and the spacer do, or the list folds into a snake again (see the note above).
    row.append(disconnectButton(p, me));
  } else {
    row.append(elx("span", "acct-provider-gap"));
  }
  return row;
}

/** Two presses, no dialog. A window.confirm() would block every subsequent browser event
 *  (and the extension along with it), and a modal is far too much furniture for one row -
 *  but a single click silently revoking a credential is worse, because re-granting means a
 *  whole OAuth round trip and a consent screen. So the button arms itself, and disarms on a
 *  timer if the user does nothing.
 *
 *  Repaints from the SERVER's `linked` list, never from the assumption that the click worked.
 *  Same stance as sign-out (#592): a revocation that failed must not leave the row reading
 *  "Not connected" while DBSearch can still read your data - that is the one lie this control
 *  must never tell. */
function disconnectButton(p, me) {
  const b = elx("button", "acct-disconnect", "Disconnect");
  b.type = "button";
  b.title = `Stop DBSearch redeeming your ${p.name} credential`;
  let armed = false;
  let timer = null;
  const disarm = () => {
    armed = false;
    clearTimeout(timer);
    b.classList.remove("acct-disconnect-arm");
    b.textContent = "Disconnect";
  };
  b.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      b.classList.add("acct-disconnect-arm");
      b.textContent = "Confirm?";
      timer = setTimeout(disarm, 4000);
      return;
    }
    clearTimeout(timer);
    b.disabled = true;
    b.textContent = "Disconnecting…";
    try {
      const res = await disconnectProvider(p.key);
      // Repaint the whole control from what the vault ACTUALLY holds now. renderAccount is
      // pure over its input, so handing it the server's list is the cheapest way to keep the
      // panel and /auth/me from ever disagreeing.
      const host = b.closest(".acct");
      if (host) {
        renderAccount(host, { ...me, linked: res.linked || [] }, lastCfg);
        // Reopen: the user is standing INSIDE this panel and a repaint closes it. Having the
        // menu vanish at the moment you act reads as the click having gone somewhere else,
        // and hides the very row you were trying to change.
        const trigger = host.querySelector(".acct-avatar");
        if (trigger) trigger.click();
      }
    } catch (_) {
      b.disabled = false;
      b.classList.add("acct-disconnect-arm");
      b.textContent = "Failed - retry";
      timer = setTimeout(disarm, 4000);
    }
  });
  return b;
}

/** ADR 0024: the AWS key form. Parked hidden after the roster and revealed by the Amazon
 *  row's Connect button - inline in the panel rather than a page or modal, because two
 *  fields do not earn a navigation, and the row the user is acting on should stay visible
 *  while they act.
 *
 *  The server FALSIFIES the keys against sts:GetCallerIdentity before vaulting, so the only
 *  success path here is one where AWS itself answered - the repaint that follows reads the
 *  server's own `linked` list, same stance as disconnectButton: never paint "Connected"
 *  from the assumption that the click worked. A rejection surfaces AWS's own reason, which
 *  is the one sentence the user can act on. */
function awsKeyForm(host, me) {
  const wrap = elx("div", "acct-aws");
  wrap.hidden = true;

  const akid = document.createElement("input");
  akid.type = "text";
  akid.placeholder = "Access key ID";
  akid.autocomplete = "off";
  akid.spellcheck = false;
  const secret = document.createElement("input");
  secret.type = "password";
  secret.placeholder = "Secret access key";
  secret.autocomplete = "new-password";

  const err = elx("div", "acct-aws-err");
  err.hidden = true;
  const go = elx("button", "acct-aws-go", "Connect AWS");
  go.type = "button";

  const submit = async () => {
    err.hidden = true;
    const id = akid.value.trim();
    const sec = secret.value.trim();
    if (!id || !sec) {
      err.textContent = "Both fields are required.";
      err.hidden = false;
      return;
    }
    go.disabled = true;
    go.textContent = "Validating with AWS…";
    try {
      const res = await connectAws(id, sec);
      // Repaint from the vault's own answer and reopen - the user is standing inside this
      // panel, and the row flipping to green Connected IS the confirmation (see
      // disconnectButton for why the reopen matters).
      renderAccount(host, { ...me, linked: res.linked || [] }, lastCfg);
      const trigger = host.querySelector(".acct-avatar");
      if (trigger) trigger.click();
    } catch (e) {
      go.disabled = false;
      go.textContent = "Connect AWS";
      err.textContent = String((e && e.message) || e);
      err.hidden = false;
    }
  };
  go.addEventListener("click", submit);
  for (const input of [akid, secret]) {
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  }

  wrap.append(
    akid, secret, err, go,
    elx("div", "acct-aws-note",
        "Validated with AWS, stored encrypted, never shown again. "
        + "Queries on your Redshift then run as your own AWS identity."));
  return wrap;
}

/** Paint the control into `host` from data alone - no fetching here, so it can be driven in
 *  node with a stubbed /auth/me payload and every state can be seen without a server.
 *
 *  `me === null` means the server could not be asked. Renders NOTHING: "Not signed in" is a
 *  claim about the user, and a page that could not reach /auth/me has no business making it
 *  (the other half of #373 - the original bug was asserting the opposite without checking). */

/** #775: how much of this account's plan is in use, and the one action that changes it.
 *
 * The whole section is HIDDEN unless the deployment can actually sell something. A
 * self-hosted box holds its own storage and is free forever (ADR 0027 rule 6), and a hosted
 * one before its live keys are set cannot take a payment either - drawing Upgrade in either
 * case is #551's tile-that-always-fails with a card number attached.
 *
 * `used_bytes: null` means the backend cannot meter, which is NOT zero. A bar drawn at 0%
 * for an account that might be full is a confident lie, so the bar is simply not drawn.
 */
async function paintStorage(host) {
  const s = await billingStatus();
  if (!s) return;                       // billing lookup failed; say nothing at all
  const canBuy = Array.isArray(s.sellable) && s.sellable.length > 0;
  const metered = s.metered && typeof s.used_bytes === "number"
                  && typeof s.quota_bytes === "number";
  if (!canBuy && !metered) return;      // nothing true to say

  host.hidden = false;
  host.append(elx("div", "acct-head", "Storage"));

  if (metered) {
    const pct = s.quota_bytes > 0
      ? Math.min(100, Math.round((s.used_bytes / s.quota_bytes) * 100)) : 0;
    const line = elx("div", "acct-storage-line",
      `${fmtBytes(s.used_bytes)} of ${fmtBytes(s.quota_bytes)} used`);
    const bar = elx("div", "acct-bar");
    const fill = elx("div", "acct-bar-fill");
    fill.style.width = `${pct}%`;
    // Amber only once it is nearly gone, so the colour means something when it appears.
    if (pct >= 90) bar.classList.add("acct-bar-full");
    bar.append(fill);
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-valuenow", String(pct));
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
    bar.setAttribute("aria-label", `Storage used: ${pct}%`);
    host.append(line, bar);
  }
  if (s.tier) host.append(elx("div", "acct-storage-plan", `Plan: ${tierLabel(s.tier)}`));
  if (!canBuy) return;

  const act = elx("div", "acct-storage-act");
  // An account Stripe already knows gets the portal, where changing plan and cancelling
  // both live - so the panel never has to grow its own billing screen.
  const known = !!s.tier && s.tier !== "free";
  if (known) {
    act.append(billingButton("Manage billing", async () => (await billingPortal()).url));
  }
  for (const t of s.sellable) {
    if (t.name === s.tier) continue;           // never offer the plan they are already on
    const label = `Upgrade to ${tierLabel(t.name)} (${fmtGb(t.quota_gb)}, ${fmtPrice(t.price_cents)}/mo)`;
    act.append(billingButton(label, async () => (await billingCheckout(t.name)).url));
  }
  host.append(act);
}

/** A button that hands off to Stripe, and says so plainly if it cannot.
 *
 * Disabled while in flight: a second click would open a second checkout session, and two
 * tabs both asking for a card is how somebody ends up subscribed twice. */
function billingButton(label, getUrl) {
  const b = elx("button", "acct-connect", label);
  b.type = "button";
  b.addEventListener("click", async () => {
    if (b.disabled) return;
    b.disabled = true;
    const original = b.textContent;
    b.textContent = "Opening...";
    try {
      location.href = await getUrl();
    } catch (e) {
      // Loudly, on the button itself. A payment action that fails quietly leaves somebody
      // unsure whether they have been charged.
      b.textContent = original;
      b.disabled = false;
      const err = elx("div", "acct-storage-err", e.message || "could not reach billing");
      b.parentElement?.append(err);
    }
  });
  return b;
}

function fmtBytes(n) {
  for (const [unit, size] of [["TB", 1024 ** 4], ["GB", 1024 ** 3], ["MB", 1024 ** 2],
                              ["KB", 1024]]) {
    if (n >= size) {
      const v = n / size;
      return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${unit}`;
    }
  }
  return `${n} B`;
}

function fmtGb(gb) { return gb >= 1024 ? `${gb / 1024} TB` : `${gb} GB`; }

/** Tier names are configuration values ("free", "plus"), and a raw config value in a sentence
 *  reads as something that leaked rather than something written. Capitalise for display only:
 *  the name the API and the ladder use is untouched. */
function tierLabel(name) {
  return String(name || "").replace(/^./, (c) => c.toUpperCase());
}

function fmtPrice(cents) { return `$${(cents / 100).toFixed(2)}`; }

export function renderAccount(host, me, cfg = {}) {
  if (!host) return;
  lastCfg = cfg || {};
  host.innerHTML = "";
  if (me === null || me === undefined) return;

  if (!me.signed_in) {
    const a = elx("a", "acct-signin", "Sign in");
    a.href = "/signin";                       // #628: the sign-in page, not Connectors
    host.append(a);
    return;
  }

  const btn = elx("button", "acct-avatar", initials(me));
  btn.type = "button";
  btn.setAttribute("aria-haspopup", "menu");
  btn.setAttribute("aria-expanded", "false");
  btn.title = me.email || me.name || "Account";

  const menu = elx("div", "acct-menu");
  menu.setAttribute("role", "menu");
  menu.hidden = true;

  // The panel opens with the trigger restated as a solid ink mark. It is the one dominant
  // object in a 296px panel (design system §5), and it is what makes the dropdown read as
  // belonging to the circle that opened it rather than as a menu that appeared nearby.
  const who = elx("div", "acct-who");
  who.append(elx("div", "acct-who-mark", initials(me)));
  const whoText = elx("div", "acct-who-text");
  whoText.append(elx("div", "acct-name", me.name || me.email || "Signed in"));
  if (me.email && me.email !== me.name) {
    const mail = elx("div", "acct-email", me.email);
    // Ellipsis rather than the old `word-break: break-all`, which split a long address
    // mid-token across three ragged lines. The full value stays reachable on hover.
    mail.title = me.email;
    whoText.append(mail);
  }
  who.append(whoText);
  who.append(elx("div", "acct-idp", signedInWith(me)));
  menu.append(who);

  // Dev rigs keep their "Searching as" switcher, inside the control rather than beside it.
  // identity.js fills this slot; it is the one place a fake identity may be chosen, and only
  // when the server says dev auth is on.
  if (cfg && cfg.dev_auth) {
    const devSlot = elx("div", "acct-dev");
    devSlot.id = "acct-dev-slot";
    menu.append(devSlot);
  }

  menu.append(elx("div", "acct-head", "Connected sources"));
  // ONE grid for the whole roster, not one flex row per provider. Each `.acct-provider` is
  // `display: contents`, so all three rows share the same three tracks and the states line up
  // into a column instead of each landing wherever its own provider name happened to end.
  // The rows measured 1696.7 / 1724.5 / 1781.9 before this, which is what read as "ugly".
  const sources = elx("div", "acct-providers");
  for (const p of ROSTER) sources.append(providerRow(p, me));
  menu.append(sources);

  // ADR 0024: the AWS key form rides BELOW the grid (a `display: contents` row cannot hold
  // a fourth cell without breaking the three-track roster, see the spacer note above), and
  // only exists in the one state whose Connect button reveals it.
  const awsProvider = ROSTER.find((p) => p.keyEntry);
  if (awsProvider && me[awsProvider.enabledFlag]
      && !(me.linked || []).includes(awsProvider.key)) {
    menu.append(awsKeyForm(host, me));
  }

  // ---- #775 storage + upgrade -----------------------------------------------------------
  // Filled in asynchronously: the panel must paint immediately, and the connected-sources
  // roster is its real job. A slow or failed billing call leaves this slot empty rather than
  // holding up everything above it.
  const storage = elx("div", "acct-storage");
  storage.hidden = true;
  menu.append(storage);
  paintStorage(storage);

  const foot = elx("div", "acct-foot");
  const theme = elx("button", "acct-theme");
  theme.type = "button";
  // Icon and label are separate nodes on purpose. `theme.textContent = ...` would have wiped
  // the SVG on the first repaint, and the same trap applies to the sign-out button below,
  // whose failure path rewrites its own label.
  const themeIco = elx("span", "acct-ico-slot");
  const themeLabel = elx("span", "acct-btn-label");
  theme.append(themeIco, themeLabel);
  const paintTheme = () => {
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    themeIco.innerHTML = ico(dark ? ICON.sun : ICON.moon);
    themeLabel.textContent = dark ? "Light theme" : "Dark theme";
  };
  theme.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark"
      ? "light" : "dark";
    localStorage.setItem(THEME_KEY, next);
    document.documentElement.setAttribute("data-theme", next);
    paintTheme();
  });
  paintTheme();

  // #592's stance, kept exactly: end the session for real, and say so ON THE CONTROL if it
  // fails. Navigating away while still signed in looks identical to success.
  const out = elx("button", "acct-signout");
  out.type = "button";
  const outIco = elx("span", "acct-ico-slot");
  outIco.innerHTML = ico(ICON.exit);
  const outLabel = elx("span", "acct-btn-label", "Sign out");
  out.append(outIco, outLabel);
  out.addEventListener("click", async () => {
    const label = outLabel.textContent;
    out.disabled = true;
    outLabel.textContent = "Signing out…";
    try {
      await signOut();
      location.href = "/";
    } catch (_) {
      out.disabled = false;
      // The failure widens the button past its resting size, so it is marked rather than
      // silently relabelled: red on the control the user just pressed, then back.
      out.classList.add("acct-signout-failed");
      outLabel.textContent = "Sign out failed - retry";
      setTimeout(() => {
        outLabel.textContent = label;
        out.classList.remove("acct-signout-failed");
      }, 4000);
    }
  });
  foot.append(theme, out);
  menu.append(foot);

  const setOpen = (open) => {
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(menu.hidden);
  });
  // Click-away and Escape, because a menu you can only close by finding its own button again
  // is a menu that ends up sitting open over the page.
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !host.contains(e.target)) setOpen(false);
  });
  // #647: and the same is true of a menu you navigate AWAY from. The guard above exempts
  // everything inside the control - right for the theme toggle, wrong for the two links, which
  // leave the surface behind them. #643 is what exposed it: now that every destination is an
  // in-document route, following a link no longer tears the document down, so nothing closed
  // the panel and it sat open on top of the surface it had just sent the user to.
  menu.addEventListener("click", (e) => {
    if (e.target.closest("a[href]")) setOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !menu.hidden) { setOpen(false); btn.focus(); }
  });

  host.append(btn, menu);
}
