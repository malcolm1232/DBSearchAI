// The public source repository, for "view source" links on the architecture pages.
// Kept as one constant so a rename/org-move is a single edit. Verified against the origin
// remote (git@github.com:malcolm1232/DBSearchAI.git) 260824; `dbsearch-ai/dbsearch` in
// nav.ts was a placeholder that never existed.
export const REPO_URL = "https://github.com/malcolm1232/DBSearchAI";
// `blob/main` follows the default branch, which is what a reader wants for "the current
// source of this file". These links 404 until the repo is public - by design.
export const SOURCE_BASE = `${REPO_URL}/blob/main/`;
