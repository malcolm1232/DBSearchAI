export const NAV_LINKS = [
  { label: "Product", href: "/product" },
  { label: "Security", href: "/security" },
  { label: "Architecture", href: "/architecture" },
  { label: "Self-host", href: "/self-host" },
  { label: "Pricing", href: "/pricing" },
] as const;

export const DOCS_URL = "https://github.com/dbsearch-ai/dbsearch#readme";
export const GITHUB_URL = "https://github.com/dbsearch-ai/dbsearch";
/*
 * Same-origin by default (#401): the site is exported and served by the very box
 * that runs the app, so the shell is a plain path.
 *
 * This used to default to http://127.0.0.1:8090, which is a fine default for
 * `npm run dev` against a local backend and a silent disaster in a static export
 * - the fallback is baked into the HTML at build time, so a production build with
 * NEXT_PUBLIC_APP_URL unset shipped "Open app" links pointing at the VISITOR's own
 * localhost. It did exactly that on the first deploy.
 */
export const APP_URL = process.env.NEXT_PUBLIC_APP_URL || "/app";
/* Where "Self-host free" lands: the Ask workspace. Connecting data is a
 * second step, reachable from the shell's Connectors nav, which goes to
 * the canvas. */
export const START_URL = process.env.NEXT_PUBLIC_START_URL || "/ask";
export const CANVAS_URL = process.env.NEXT_PUBLIC_CANVAS_URL || "/canvas";
/* #386: the whole sign-in funnel (multi-tenant Entra, per-owner workspaces,
 * credential panel) shipped behind a landing that never said "Sign in" - the
 * word appeared nowhere on the page. Same-origin path for the same reason as
 * APP_URL: the box serving this export is the box that runs it.
 *
 * #446: points at /signin, NOT /auth/login. Going straight to the IdP dropped the
 * visitor onto an "unverified publisher" consent screen with no context, which
 * reads as phishing. /signin is ours, explains the redirect first, and is also the
 * only place a Google-primary visitor can start. */
export const SIGN_IN_URL = process.env.NEXT_PUBLIC_SIGN_IN_URL || "/signin";
