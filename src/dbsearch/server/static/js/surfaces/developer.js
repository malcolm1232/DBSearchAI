// src/dbsearch/server/static/js/surfaces/developer.js
//
// #590: API keys, and a quickstart that actually starts.
//
// This page used to end with an "Endpoint reference" built by walking `openapi().paths` and
// printing every method it found - about seventy lines, in the order the router happened to
// register them. That list was useless in three separate ways and harmful in a fourth:
//
//   it was not an API      GET /, /robots.txt, /signin, /canvas, /admin and /developer are
//                          PAGES. A developer reading the list cannot tell which of these
//                          they are meant to call.
//   it had no contract     no parameters, no request body, no response shape, no status
//                          codes. "POST /search" tells you nothing you can act on.
//   it was unordered       the one endpoint almost everyone wants (ask a question) sat
//                          between /admin/telemetry and /connectors/sharepoint/callback.
//   it advertised a seam   POST /auth/dev/seed is a TEST-ONLY route, hidden behind
//                          DBSEARCH_DEV_SEED and capable of minting a session for an
//                          arbitrary oid. Listing it on a public page is free reconnaissance.
//
// What replaces it is the smallest thing that is actually true and actually usable: how to
// authenticate, the two calls that answer a question, and a copyable curl that works as
// written. The full machine-readable spec stays one link away at /openapi.json, which is
// where a developer who wants exhaustive detail should be sent anyway.
import { el } from "../ui/components.js";
import { createKey, developerKeys, revokeKey } from "../api.js";

function panel(title, body, { sub = "", wide = false } = {}) {
  const head = [el("h2", { class: "admin-panel-title" }, title)];
  if (sub) head.push(el("p", { class: "admin-panel-sub" }, sub));
  return el("section", { class: wide ? "admin-panel admin-panel-wide" : "admin-panel" },
    ...head, body);
}

function relTime(iso) {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

function keyRow(rec, onRevoke) {
  const revokeBtn = el("button", { type: "button", class: "share-revoke" }, "Revoke");
  revokeBtn.addEventListener("click", () => onRevoke(rec.id, revokeBtn));
  return el("tr", {},
    el("td", {}, rec.label),
    el("td", { class: "dev-mono" }, rec.id),
    el("td", {}, relTime(rec.created_at)),
    el("td", {}, rec.last_used_at ? relTime(rec.last_used_at) : "never used"),
    el("td", { class: "dev-num" }, String(rec.request_count)),
    el("td", {}, rec.revoked ? el("span", { class: "dev-revoked" }, "revoked") : revokeBtn),
  );
}

// A copy button beats "select the text and press cmd-C", and it is four lines.
function copyable(text) {
  const pre = el("pre", { class: "dev-code" }, text);
  const btn = el("button", { type: "button", class: "dev-copy" }, "Copy");
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      btn.textContent = "Copied";
      setTimeout(() => { btn.textContent = "Copy"; }, 1600);
    } catch (_) {
      btn.textContent = "Press cmd-C";
      setTimeout(() => { btn.textContent = "Copy"; }, 2400);
    }
  });
  return el("div", { class: "dev-codewrap" }, btn, pre);
}

async function renderKeys(grid) {
  const body = el("div", {});
  const tbody = el("tbody", {});
  const status = el("div", { class: "dev-status" });
  const labelInput = el("input", { type: "text", class: "share-input",
    placeholder: "What is this key for? e.g. ci-pipeline" });
  const createBtn = el("button", { type: "button", class: "share-add" }, "Create key");
  const empty = el("tr", {}, el("td", { colspan: "6", class: "admin-empty" },
    "No keys yet. Create one to call the API from a script or another service."));

  async function refresh() {
    tbody.replaceChildren();
    try {
      const keys = await developerKeys();
      if (!keys.length) { tbody.appendChild(empty); return; }
      keys.forEach((k) => tbody.appendChild(keyRow(k, doRevoke)));
    } catch (e) { status.textContent = `Error: ${e.message}`; }
  }
  async function doRevoke(id, btn) {
    btn.disabled = true;
    btn.textContent = "Revoking…";
    try { await revokeKey(id); status.textContent = `Revoked ${id}.`; refresh(); }
    catch (e) {
      status.textContent = `Revoke failed: ${e.message}`;
      btn.disabled = false;
      btn.textContent = "Revoke";
    }
  }
  createBtn.addEventListener("click", async () => {
    const label = labelInput.value.trim();
    if (!label) { status.textContent = "Give the key a label so you can recognise it later."; return; }
    createBtn.disabled = true;
    try {
      const { token } = await createKey(label);
      // Shown once, by design: the server keeps a hash, not the token.
      status.replaceChildren(el("div", { class: "token-once" },
        el("strong", {}, "Copy this now. It is not shown again."),
        copyable(token)));
      labelInput.value = "";
      refresh();
    } catch (e) { status.textContent = `Could not create the key: ${e.message}`; }
    finally { createBtn.disabled = false; }
  });

  body.append(
    el("table", {}, el("thead", {}, el("tr", {},
      el("th", {}, "Label"), el("th", {}, "Key id"), el("th", {}, "Created"),
      el("th", {}, "Last used"), el("th", { class: "dev-num" }, "Requests"), el("th", {}, ""))),
      tbody),
    el("div", { class: "share-controls" }, labelInput, createBtn),
    status,
    el("p", { class: "admin-note" },
      "A key acts as you: it returns exactly the documents you can already see, and nothing "
      + "more. Revoking one takes effect immediately."),
  );
  grid.appendChild(panel("API keys", body,
    { wide: true, sub: "Call DBSearch from a script or another service, as yourself." }));
  refresh();
}

function renderQuickstart(grid) {
  const origin = window.location.origin;
  const body = el("div", { class: "dev-guide" });

  body.append(
    el("p", { class: "admin-note" },
      "Every request authenticates with your key. Identity comes from the key itself, never "
      + "from a field in the request, so a caller cannot ask for someone else's results."),

    el("h3", { class: "admin-sub" }, "Ask a question"),
    el("p", { class: "admin-note" },
      "Returns the answer, the documents it drew on, and a citation for each claim."),
    copyable(
      `curl -X POST ${origin}/search \\\n`
      + `  -H "Authorization: Bearer dbk_YOUR_KEY" \\\n`
      + `  -H "Content-Type: application/json" \\\n`
      + `  -d '{"question": "what is our parental leave policy?"}'`),

    el("h3", { class: "admin-sub" }, "Add a document"),
    el("p", { class: "admin-note" },
      "Indexed private to you; share it afterwards from Your data. Returns 202 with a "
      + "job handle — follow `poll` (GET /ingest/jobs/{job_id}) until status is "
      + "succeeded (#917)."),
    copyable(
      `curl -X POST ${origin}/admin/upload \\\n`
      + `  -H "Authorization: Bearer dbk_YOUR_KEY" \\\n`
      + `  -F "file=@handbook.pdf"`),

    el("h3", { class: "admin-sub" }, "Everything else"),
    el("p", { class: "admin-note" },
      "The full machine-readable specification, including every parameter and response shape:"),
    el("div", { class: "dev-links" },
      el("a", { class: "doc-act", href: "/openapi.json", target: "_blank",
                rel: "noopener" }, "OpenAPI specification"),
      el("a", { class: "doc-act", href: "/developer/graphql-schema", target: "_blank",
                rel: "noopener" }, "GraphQL schema")),
  );

  grid.appendChild(panel("Quickstart", body,
    { wide: true, sub: "Two calls cover almost everything." }));
}

export function mountDeveloper(root) {
  root.replaceChildren();
  root.append(el("div", { class: "admin-head" },
    el("h1", {}, "Developer"),
    el("p", { class: "admin-lede" },
      "Keys and a quickstart for calling DBSearch from your own code.")));
  const grid = el("div", { class: "admin-grid" });
  root.appendChild(grid);
  renderKeys(grid);
  renderQuickstart(grid);
}
