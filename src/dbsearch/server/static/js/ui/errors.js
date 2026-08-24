// src/dbsearch/server/static/js/ui/errors.js
// One typed-error vocabulary for every surface (#409).
//
// This started life inside chat.js when #374 was fixed there. Ask still printed
// `Error: ${err.message}`, so the same 401 produced a helpful card in Chat and a
// bare "Error: chat failed: 401" in Ask. Two implementations of "what went wrong"
// will always drift; there is now one.
//
// The rule these follow: say what happened in the user's terms, and offer the
// action that resolves it. A status code is not an explanation.
import { el } from "./components.js";

/**
 * Map a thrown fetch error onto something a human can act on.
 *
 * `signedIn` (#593): a 403 means two completely different things depending on who is asking,
 * and the old copy only covered one of them. "Not available in the demo. Sign in to use the
 * live product." is right for a demo visitor and nonsense for a signed-in customer who simply
 * is not an operator of the deployment - telling them to sign in when they already are sends
 * them round a loop that cannot help. Callers that know pass it; callers that do not get
 * today's behaviour unchanged.
 *
 * #628: both Sign in actions target /signin. They pointed at /canvas, so the 401 a signed-out
 * visitor hit on Admin offered a button that took them to Connectors instead of to a sign-in
 * page. This module is where that mistake reached the most surfaces at once, since every
 * surface renders its 401 through here.
 */
export function explain(err, { signedIn = null } = {}) {
  const status = Number(String((err && err.message) || "").match(/\b(\d{3})\b/)?.[1]);
  if (status === 401) {
    return { title: "You are not signed in.",
             body: "Sign in to search the documents you have access to.",
             action: { label: "Sign in", href: "/signin" } };
  }
  if (status === 403 && signedIn === true) {
    return { title: "This is not yours to see.",
             body: "It reports on the whole deployment, and this one limits that to its operators." };
  }
  if (status === 403) {
    return { title: "Not available in the demo.",
             body: "Sign in to use the live product.",
             action: { label: "Sign in", href: "/signin" } };
  }
  if (status === 429) {
    return { title: "Too many requests.", body: "Give it a moment and try again." };
  }
  if (status >= 500) {
    return { title: "The server could not answer that.",
             body: "This is not a permissions problem. Try again, and if it persists the logs will have the detail." };
  }
  return { title: "Something went wrong.",
           body: (err && err.message) ? err.message : "Unknown error." };
}

/** Build the standard error block. Callers decide where to put it. */
export function errorBlock(err, opts = {}) {
  const info = explain(err, opts);
  const box = el("div", { class: "error-body" },
    el("strong", {}, info.title),
    el("span", {}, info.body));
  if (info.action) {
    box.append(el("a", { class: "btn-primary btn-sm", href: info.action.href },
                  info.action.label));
  }
  return box;
}
