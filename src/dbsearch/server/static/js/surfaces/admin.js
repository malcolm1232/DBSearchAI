// src/dbsearch/server/static/js/surfaces/admin.js
//
// #589: this page is "Your data" - it belongs to the person whose documents these are.
//
// It used to be an Admin Console: eight panels rendered in the order the API happened to
// expose them, printing whatever each endpoint returned. Driven on the live site it showed a
// real signed-in customer a demo tenant ("acme"), demo users (alice, bob), demo groups with
// "0 doc(s)", a SharePoint source named Contoso that had never synced, their own oid instead
// of their name, a `grant:<uuid>` principal for a share that had been revoked, a panel that
// said "Not reported by this backend", and a telemetry block of snake_case counters. The
// file's own header called it "intent-level layout; visual polish is a later UX pass".
//
// The rebuild answers one question - "what have I put in here, and who can see it?" - and
// everything that answers a different question is either operator-only or gone:
//
//   kept, and made primary   documents, adding one, sharing one, what has been asked
//   operator-only            stores, sources, index health, users and groups, telemetry,
//                            permission tester - all DEPLOYMENT facts, not the owner's
//   deleted                  nothing that had an answer; panels that could only say
//                            "not reported" no longer render at all rather than rendering
//                            their own emptiness
//
// The operator gate is the SERVER's (`/config.operator`, ADR 0011 s3). This is presentation
// only: it decides what to draw, never what is permitted.
import {
  adminIndex, adminIdentities, adminDocuments, adminDocumentSegments, adminTelemetry,
  adminPermissionTest, adminSources, adminResync, ingestJob, getConfig, uploadDocument,
  routerCatalog, storeSchema, authMe, documentGrants, shareDocument, revokeShare, myQuestions,
  deleteDocument, myShares, shareQuestions, revokeConversationShare,
} from "../api.js";
import { el } from "../ui/components.js";
import { errorBlock } from "../ui/errors.js";
// #607/#608: the SAME dialog the Ask surface opens, reopened here in edit mode. Imported
// rather than reimplemented, because the promise that a share can only ever be narrowed is
// kept by that DOM having no control which widens one - a second dialog built here would be a
// second DOM where somebody would have to remember the rule.
import { buildShareModal } from "./ask.js";
// #607 review round 1, Finding 3. The edit dialog declares `aria-modal="true"` and used to
// implement NEITHER Escape NOR a focus trap: those live in `mountAsk`'s closure, so opening the
// same panel from here inherited the markup and none of the behaviour. That is a promise in
// markup the code did not keep - a keyboard user could Shift+Tab straight out of a dialog the
// screen reader had just been told was the only thing on the page. One definition, both
// screens (DESIGN_SYSTEM s6).
import { wireModalHost, focusFirstIn } from "../ui/modal.js";

// #593: whether this caller has a session, so a refusal can be explained in the right terms.
// Null until mountAdmin has asked - explain() treats that as "do not claim either way".
let _signedIn = null;

/** Render a failure the way #409 decided failures are rendered: what happened, and what to do.
 *
 *  Every panel used to end `catch (e) { body.textContent = \`Error: ${e.message}\`; }`, so
 *  signed out the page showed "Error: admin/documents failed: 401" beside a banner that
 *  already said "Not signed in" and offered the button that fixes it. A status code is not an
 *  explanation, and this page in particular is read by the document's owner, not an operator.
 */
function fail(node, err) {
  node.innerHTML = "";
  node.append(errorBlock(err, { signedIn: _signedIn }));
}

function panel(title, body, { wide = false, sub = "" } = {}) {
  const head = [el("h2", { class: "admin-panel-title" }, title)];
  if (sub) head.push(el("p", { class: "admin-panel-sub" }, sub));
  return el("section", { class: wide ? "admin-panel admin-panel-wide" : "admin-panel" },
    ...head, body);
}

function kv(label, value) {
  return el("div", { class: "admin-kv" },
    el("span", { class: "admin-k" }, label), el("span", { class: "admin-v" }, String(value)));
}

const KIND_GLYPH = { sharepoint: "📁", local: "🗂" };

// #695: follow an edition-rail crawl to a terminal state, reporting progress as it goes.
// Terminal is succeeded|failed (pipeline/jobs.py TERMINAL); queued|running are not.
// Bounded rather than unbounded: a poller with no ceiling turns a stuck job into a spinner
// that never resolves and a button disabled forever. On timeout we resolve null and the
// caller re-reads the row, so the panel shows whatever actually happened rather than a lie.
const JOB_POLL_MS = 700;
const JOB_POLL_MAX = 120;          // ~85s

async function _awaitJob(jobId, onTick) {
  if (!jobId) return null;
  for (let i = 0; i < JOB_POLL_MAX; i++) {
    let j;
    try {
      j = await ingestJob(jobId);
    } catch (e) {
      return null;                 // 404 once the job is reaped - fall back to the row
    }
    if (onTick) onTick(j);
    if (j.status === "succeeded" || j.status === "failed") return j;
    await new Promise((r) => setTimeout(r, JOB_POLL_MS));
  }
  return null;
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

/* #587: `freshness` arrives as the raw router string - "ingested@2026-08-07T10:45:28.070462+00:00".
   Forty-two characters of machine text in a flex row, next to a `.store-name` that is
   `flex:1; min-width:0; overflow-wrap:anywhere`. The timestamp does not shrink, so it won a
   width fight against the name and the name broke ONE CHARACTER PER LINE down the panel -
   "folder-1" rendered as a vertical column. The layout bug and the ugly string were the same
   bug: the least important thing on the row was the one that could not yield. */
function freshnessLabel(raw) {
  if (!raw) return "";
  const m = /^ingested@(.+)$/.exec(raw);
  if (m) return `indexed ${relTime(m[1])}`;
  if (raw === "never-synced") return "never synced";
  return raw;
}

function sourceRow(s) {
  const last = el("span", { class: "src-last" }, relTime(s.last_sync_at));
  const count = el("span", { class: "src-count" }, `${s.doc_count} doc(s)`);
  const status = el("span", { class: `src-status src-${s.status}` }, s.status);
  const btn = el("button", { type: "button", class: "src-resync" }, "Resync");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Syncing…";
    try {
      // #695: /admin/resync SUBMITS a crawl (202 + job handle) since #569 - it does not
      // return the updated row. Reading last_sync_at/doc_count/status off the handle gave
      // `undefined`, which rendered as "undefined doc(s)", a blank status chip and a
      // last-sync still reading "never": the panel told the operator the sync had not
      // happened while the job was running. 202 is `ok`, so nothing threw and the error
      // branch below never ran - it failed silently into nonsense rather than loudly.
      const handle = await adminResync(s.source_id);
      const settled = await _awaitJob(handle.job_id, (j) => {
        status.textContent = j.phase || j.status;
        status.className = "src-status src-running";
        if (j.docs_total) count.textContent = `${j.docs_done}/${j.docs_total} doc(s)`;
      });
      if (settled && settled.status === "failed") {
        // `error` is an exception CLASS NAME by construction (LAW 1 - never a driver or
        // connector message, which can quote document content or a credential).
        status.textContent = settled.error || "failed";
        status.className = "src-status src-error";
      } else {
        // Re-read the row from the source of truth rather than reconstructing it from the
        // job: the summary is what every other render of this panel uses.
        const rows = await adminSources();
        const u = rows.find((r) => r.source_id === s.source_id) || {};
        last.textContent = relTime(u.last_sync_at);
        count.textContent = `${u.doc_count ?? 0} doc(s)`;
        status.textContent = u.status || "idle";
        status.className = `src-status src-${u.status || "idle"}`;
      }
    } catch (e) {
      status.textContent = "error";
      status.className = "src-status src-error";
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });
  return el("div", { class: "src-row" },
    el("span", { class: "src-kind" }, KIND_GLYPH[s.kind] || "•"),
    el("span", { class: "src-name" }, s.display_name),
    last, count, status, btn);
}

// #562: one composed store, with its schema one click away. The list is already trimmed to
// the caller by visible_stores() (gate #1) - this renders what it is handed and never filters
// again, because a second filter in the client is a second thing that can disagree.
function storeRow(s) {
  const detail = el("div", { class: "store-detail" });
  // Only a SQL store has a schema to show. Offering the control on a document store is an
  // affordance that can only ever disappoint - the honest snapshot of THAT store is its
  // document list, which the Documents panel already is.
  const sql = (s.capabilities || []).includes("analytical");
  const btn = sql
    ? el("button", { type: "button", class: "store-schema" }, "Show schema")
    : el("span", { class: "store-nosql" }, "documents");
  let loaded = false;
  if (sql) btn.addEventListener("click", async () => {
    if (loaded) {                       // toggle, so a wide schema can be put away again
      detail.innerHTML = "";
      loaded = false;
      btn.textContent = "Show schema";
      return;
    }
    btn.disabled = true;
    detail.textContent = "Loading…";
    try {
      const d = await storeSchema(s.store_id);
      detail.innerHTML = "";
      if (!d.tables.length) {
        // A SQL store with no tables visible is what a pushdown store looks like when the
        // caller's grants cover nothing (#304). Only say that when it is a SQL store: a
        // document store has no tables BY NATURE, and telling its owner their grants are
        // the reason sends them to fix a permission that was never the problem.
        detail.append(el("div", { class: "admin-muted" },
          d.kind === "federated_sql"
            ? "No tables are visible to your grants on this store."
            : "This store holds documents, not tables. What is indexed in it is listed "
              + "under Documents below."));
      }
      d.tables.forEach((t) => {
        const cols = (t.columns || []).map((c) => `${c.name} ${c.type || ""}`.trim()).join(", ");
        // null row_count means the engine cannot count, NOT that the table is empty. Saying
        // "0" here would tell an operator a full warehouse is empty (#392).
        const n = t.row_count === null || t.row_count === undefined
          ? "row count not reported" : `${t.row_count} row(s)`;
        detail.append(el("div", { class: "store-table" },
          el("span", { class: "store-tname" }, t.table),
          el("span", { class: "store-tcount" }, n),
          el("div", { class: "store-tcols" }, cols)));
      });
      loaded = true;
      btn.textContent = "Hide schema";
    } catch (e) { fail(detail, e); }
    finally { btn.disabled = false; }
  });
  return el("div", { class: "store-block" },
    el("div", { class: "store-row" },
      el("span", { class: "store-name" }, s.title || s.store_id),
      el("span", { class: "store-kind" }, s.kind || ""),
      el("span", { class: "store-caps" }, (s.capabilities || []).join(" · ")),
      el("span", { class: "store-fresh" }, freshnessLabel(s.freshness)),
      btn),
    detail);
}

async function renderStores(grid) {
  const body = el("div", { id: "admin-stores" }, "Loading…");
  grid.append(panel("Databases", body,
    { sub: "Every store composed on this deployment that you are allowed to see." }));

  /* #591: measured on prod, /router/catalog took 22 SECONDS. It probes each composed store,
     and a store whose engine is stopped is answered by a TCP timeout, not an error - so the
     endpoint is as slow as the least reachable database on the box. The panel used to sit on
     a bare "Loading…" for the whole of that, which reads as broken rather than as slow.
     Waiting is fine; waiting with no explanation is not. Fixing the endpoint is #591; this is
     the honest surface for however long it takes. */
  const slow = setTimeout(() => {
    if (body.textContent === "Loading…") {
      body.replaceChildren(el("div", { class: "admin-muted" },
        "Still checking. One of the connected databases is slow to answer, usually because "
        + "its engine is stopped or unreachable."));
    }
  }, 4000);

  try {
    const cat = await routerCatalog();
    clearTimeout(slow);
    body.innerHTML = "";
    if (cat === null) {
      body.append(el("div", { class: "admin-empty" },
        "Nothing composed yet — connect a database on the Connectors canvas."));
      return;
    }
    const units = cat.business_units || [];
    const stores = units.flatMap((bu) => (bu.sources || []).flatMap((src) => src.stores || []));
    if (!stores.length) {
      body.append(el("div", { class: "admin-empty" },
        "No stores you can see are composed on this deployment."));
      return;
    }
    units.forEach((bu) => {
      const buStores = (bu.sources || []).flatMap((src) => src.stores || []);
      if (!buStores.length) return;
      body.append(el("h3", { class: "store-bu" }, bu.id || "(no business unit)"));
      buStores.forEach((s) => body.append(storeRow(s)));
    });
  } catch (e) { clearTimeout(slow); fail(body, e); }
}

async function renderSources(grid) {
  const body = el("div", { id: "admin-sources" }, "Loading…");
  const p = panel("Connected sources", body,
    { sub: "Libraries and folders that sync into the index." });
  grid.append(p);
  try {
    const sources = await adminSources();
    body.innerHTML = "";
    // A source list of nothing is not worth a panel. Removing the whole section beats
    // printing "No sources connected." into an empty box the reader has to parse first.
    if (!sources.length) { p.remove(); return; }
    sources.forEach((s) => body.append(sourceRow(s)));
  } catch (e) { fail(body, e); }
}

/* ---- the owner's view -------------------------------------------------------------- */

/** Who can read this document, said in words rather than in principals.
 *
 * `allowed_principals` is the raw ACL: the owner's oid, `tenant:<tid>` for an org-wide
 * upload (#575), and one `grant:<id>` per share ever made (ADR 0017 never rewrites an ACL,
 * so a revoked share leaves its principal behind forever). Printing that list is what the
 * old page did, and it showed a customer a uuid for a share they had already revoked.
 *
 * `liveGrants` is the registry's answer - the shares that are actually live right now - and
 * it is the only honest source for the count.
 */
function audienceLabel(doc, me, liveGrants) {
  const acl = doc.allowed_principals || [];
  // #582: a document that reached you through a SHARE is not yours, and the audience
  // wording written for an owner reads as nonsense on it - "Only you" on somebody else's
  // handbook, or "1 group(s)" describing an audience you are not the author of. Say the one
  // true thing instead. Checked FIRST, before any owner-shaped branch below.
  if (doc.shared_with_you === true && doc.owned_by_you !== true) {
    return { text: "Shared with you", tone: "shared" };
  }
  const org = acl.some((p) => String(p).startsWith("tenant:"));
  const n = liveGrants.length;
  if (org) return { text: n ? `Everyone at your organization, plus ${n} named` : "Everyone at your organization", tone: "wide" };
  if (n) return { text: n === 1 ? "You and 1 other person" : `You and ${n} other people`, tone: "shared" };
  const others = acl.filter((p) => p !== me && !String(p).startsWith("grant:")
                                    && !String(p).startsWith("tenant:"));
  if (others.length) return { text: `${others.length} group(s)`, tone: "shared" };
  return { text: "Only you", tone: "private" };
}

function shareBox(doc, grants, onChanged) {
  const box = el("div", { class: "share-box" });
  const list = el("div", { class: "share-list" });

  const paint = () => {
    list.innerHTML = "";
    if (!grants.length) {
      list.append(el("div", { class: "admin-muted" }, "Not shared with anyone yet."));
      return;
    }
    grants.forEach((g) => {
      const revoke = el("button", { type: "button", class: "share-revoke" }, "Remove");
      revoke.addEventListener("click", async () => {
        revoke.disabled = true;
        revoke.textContent = "Removing…";
        try {
          await revokeShare(g.grant_id);
          grants = grants.filter((x) => x.grant_id !== g.grant_id);
          paint();
          onChanged(grants);
        } catch (e) {
          revoke.disabled = false;
          revoke.textContent = "Remove";
          list.append(el("div", { class: "share-err" }, e.message));
        }
      });
      list.append(el("div", { class: "share-row" },
        el("span", { class: "share-who" }, g.grantee_oid),
        el("span", { class: "share-when" }, `added ${relTime(g.created_at)}`),
        revoke));
    });
  };
  paint();

  const who = el("input", { type: "text", class: "share-input",
    placeholder: "Their sign-in address or ID", autocomplete: "off" });
  const err = el("div", { class: "share-err" });
  const add = el("button", { type: "button", class: "share-add" }, "Share");
  add.addEventListener("click", async () => {
    const v = who.value.trim();
    err.textContent = "";
    if (!v) { err.textContent = "Enter who you want to share with."; return; }
    add.disabled = true;
    add.textContent = "Sharing…";
    try {
      const g = await shareDocument(doc.doc_external_id, v);
      grants = grants.concat([g]);
      who.value = "";
      paint();
      onChanged(grants);
    } catch (e) {
      err.textContent = e.message;
    } finally { add.disabled = false; add.textContent = "Share"; }
  });

  box.append(
    list,
    el("div", { class: "share-controls" }, who, add),
    err,
    el("p", { class: "admin-note" },
      "They read it as themselves, so every access stays attributable. "
      + "Removing access takes effect immediately."),
  );
  return box;
}

// #948: a document that arrived through a CONNECTOR (gdrive, sharepoint_link, folder), not an
// upload. It lives in its store's own in-process index, so the upload-plane actions all 404 on
// it: Download and Check-text read /admin/documents/{id}/... off the upload index, Delete is
// removal of one row that this listing does not own (a connector doc is removed by deleting its
// SOURCE node, #947), and Share cannot re-share a link-sourced doc. So this row draws NONE of
// them. It shows where the document came from and an Open-source link to the real file, which is
// the honest read affordance for content whose original lives in Drive / SharePoint.
function connectorDocRow(doc) {
  const kindLabel = { gdrive: "Google Drive", sharepoint_link: "SharePoint",
                      sharepoint: "SharePoint", folder: "Folder" }[doc.source_kind]
                    || (doc.source_kind || "Connected source");
  const badge = el("span", { class: "doc-source-badge", title:
    `Ingested from ${kindLabel} · store ${doc.source_store}. Remove it by deleting that source `
    + `node on the canvas.` }, kindLabel);
  const acts = el("div", { class: "doc-acts" });
  if (doc.uri) {
    const open = el("a", { class: "doc-act", href: doc.uri, target: "_blank",
      rel: "noopener noreferrer" }, "Open source");
    open.title = "The original file, where it lives in " + kindLabel;
    acts.append(open);
  }
  return el("article", { class: "doc-item doc-item-connector" },
    el("div", { class: "doc-head" },
      el("h3", { class: "doc-title" }, doc.title || doc.doc_external_id),
      badge),
    acts);
}

function documentRow(doc, me, grid) {
  // #948: connector-sourced documents take a distinct, action-light card (see above).
  if (doc.source_store) return connectorDocRow(doc);
  const audience = el("span", { class: "doc-audience" });
  let grants = [];

  const setAudience = (gs) => {
    const a = audienceLabel(doc, me, gs);
    audience.textContent = a.text;
    audience.className = `doc-audience doc-audience-${a.tone}`;
  };
  setAudience([]);

  const drawer = el("div", { class: "doc-drawer" });
  let open = "";

  const toggle = (name, build) => async () => {
    if (open === name) { drawer.innerHTML = ""; open = ""; return; }
    drawer.innerHTML = "";
    drawer.append(el("div", { class: "admin-muted" }, "Loading…"));
    open = name;
    try {
      const node = await build();
      if (open !== name) return;              // a second click landed first
      drawer.innerHTML = "";
      drawer.append(node);
    } catch (e) {
      drawer.innerHTML = "";
      drawer.append(el("div", { class: "share-err" }, e.message));
    }
  };

  const shareBtn = el("button", { type: "button", class: "doc-act" }, "Share");
  shareBtn.addEventListener("click", toggle("share", async () => {
    grants = await documentGrants(doc.doc_external_id);
    setAudience(grants);
    return shareBox(doc, grants, setAudience);
  }));

  const textBtn = el("button", { type: "button", class: "doc-act" }, "Check text");
  textBtn.title = "See exactly what DBSearch extracted and searches over";
  textBtn.addEventListener("click", toggle("text", async () => {
    const segs = await adminDocumentSegments(doc.doc_external_id);
    const wrap = el("div", { class: "seg-wrap" });
    wrap.append(el("div", { class: "admin-muted" },
      `${segs.length} passage(s) indexed from this file:`));
    segs.forEach((s) => {
      const loc = s.locator && s.locator.kind
        ? `${s.locator.kind} ${s.locator.n ?? s.locator.path ?? ""}` : "whole document";
      wrap.append(el("div", { class: "seg-row" },
        el("span", { class: "seg-loc" }, loc),
        el("span", { class: "seg-text" }, s.preview)));
    });
    return wrap;
  }));

  // #562: a plain link, not a fetch. The browser already sends the session and already knows
  // how to save a file; routing bytes through JS to hand them back buys nothing.
  const dl = el("a", { class: "doc-act",
    href: `/admin/documents/${encodeURIComponent(doc.doc_external_id)}/download`,
    download: "" }, "Download");
  dl.title = "The original file and the extracted text DBSearch indexed";

  const acts = el("div", { class: "doc-acts" }, textBtn, dl);

  // #582: SHARE gets the same ownership rule as Delete, for the same #551 reason. Once a
  // grantee could see a shared document in their own listing, this button appeared on it -
  // and a share cannot be re-shared (ADR 0017 s2), so it could only ever 404. Read parity
  // for a grantee is READ parity: Check text and Download, not Share and not Delete.
  if (doc.owned_by_you === true) acts.prepend(shareBtn);

  // #594: only on documents that are YOURS. `owned_by_you` is the server's answer, and it is
  // absent on a backend that cannot tell - in which case no button is drawn at all, because
  // unknown is not "yes" and this is the one action with no undo. Offering it on a document
  // the API will refuse would be the "tile that always fails" trap (#551), and here the tile
  // that always fails would be the DELETE button, which is the worst possible one to guess at.
  if (doc.owned_by_you === true) acts.append(deleteControl(doc, me, grid));

  return el("article", { class: "doc-item" },
    el("div", { class: "doc-head" },
      el("h3", { class: "doc-title" }, doc.title || doc.doc_external_id),
      audience),
    acts,
    drawer);
}

/** Delete, behind a deliberate second click.
 *
 *  Not a native confirm(): a browser modal blocks the whole page - and every automation
 *  session that has to verify this - until somebody dismisses it by hand. The two-step swap
 *  in place says exactly what is about to happen and stays inside the page.
 */
function deleteControl(doc, me, grid) {
  const wrap = el("span", { class: "doc-del" });
  const draw = (confirming) => {
    wrap.innerHTML = "";
    if (!confirming) {
      const btn = el("button", { type: "button", class: "doc-act doc-act-danger" }, "Delete");
      btn.title = "Remove this document and everything indexed from it";
      btn.addEventListener("click", () => draw(true));
      wrap.append(btn);
      return;
    }
    const yes = el("button", { type: "button", class: "doc-act doc-act-danger" },
                   "Yes, delete");
    const no = el("button", { type: "button", class: "doc-act" }, "Cancel");
    no.addEventListener("click", () => draw(false));
    yes.addEventListener("click", async () => {
      yes.disabled = true;
      no.disabled = true;
      yes.textContent = "Deleting…";
      try {
        await deleteDocument(doc.doc_external_id);
        // Re-read the listing rather than removing the row locally: the server is the one
        // that knows what is left, and a row hidden in the browser while the delete half
        // failed would be the same lie #592 was.
        //
        // `me` MUST be threaded through. Passing "" here shipped once and was caught only by
        // driving it: audienceLabel() needs the caller's own oid to recognise a private
        // document, so every surviving row's badge flipped from "Only you" to "1 group(s)"
        // the moment any document was deleted. Nothing was actually shared - the page just
        // stopped being able to tell.
        renderDocuments(grid, me, { replace: true });
      } catch (e) {
        wrap.innerHTML = "";
        wrap.append(el("span", { class: "share-err" }, e.message));
        setTimeout(() => draw(false), 4000);
      }
    });
    wrap.append(el("span", { class: "doc-del-ask" }, "Delete permanently?"), yes, no);
  };
  draw(false);
  return wrap;
}

async function renderDocuments(grid, me, { replace = false } = {}) {
  // #552: re-render in place after an upload. The panel used to be built only at mount, so a
  // successful upload left "No documents indexed yet" on screen at exactly the moment the
  // user is deciding whether the product works.
  let body = replace ? document.getElementById("admin-documents") : null;
  if (body) {
    body.innerHTML = "Loading…";
  } else {
    body = el("div", { id: "admin-documents" }, "Loading…");
    grid.append(panel("Your documents", body,
      { wide: true,
        // #582: "everything you have added" stopped being true the moment a document
        // shared with you could appear here.
        sub: "Everything you have added or been given, and who can read each one." }));
  }
  try {
    const docs = await adminDocuments();
    body.innerHTML = "";
    if (!docs.length) {
      body.append(el("div", { class: "admin-empty" },
        "Nothing here yet. Add a document below, then ask a question about it."));
      return;
    }
    docs.forEach((d) => body.append(documentRow(d, me, grid)));
  } catch (e) { fail(body, e); }
}

async function renderUpload(grid, me) {
  // #539: no group picker. It used to be mandatory and could only offer the DEMO groups, so a
  // real user picked a principal they did not hold and their own upload became invisible to
  // them - reproduced on the live site. The document is private to the uploader by default,
  // and sharing is a separate deliberate act (#538) rather than a guess made at upload time.
  const body = el("div", { class: "up-form" });
  const fileInput = el("input", { type: "file", class: "up-file",
    accept: ".pdf,.txt,.md,.pptx,.docx,.csv,.xlsx,.json" });
  const titleInput = el("input", { type: "text", class: "up-title",
    placeholder: "Title (optional) — defaults to the filename" });
  const status = el("div", { class: "upload-status" });
  const btn = el("button", { type: "button", class: "up-go" }, "Add document");

  btn.addEventListener("click", async () => {
    if (!fileInput.files[0]) { status.textContent = "Choose a file first."; return; }
    btn.disabled = true;
    status.textContent = "Extracting text, indexing…";
    try {
      // Empty ACL = private to you, resolved server-side from the session (never a
      // client-supplied oid, which would be an ACL the caller could forge).
      const res = await uploadDocument(fileInput.files[0], [], titleInput.value.trim());
      // #917: the upload is a SUBMIT (202 + ingest job, LAW 4) - follow the job to its
      // terminal state, narrating the runner's real phases. A parse failure is the async
      // home of the old 422 and must stay as loud (#181).
      let job = { status: res.job_status || "queued", phase: "" };
      while (job.status !== "succeeded" && job.status !== "failed") {
        await new Promise((r) => setTimeout(r, 700));
        job = await ingestJob(res.job_id);
        status.textContent = ({ extracting: "Extracting text…", embedding: "Embedding…",
          indexing: "Indexing…" })[job.phase] || "Working…";
      }
      if (job.status === "failed") {
        throw new Error(/ParseProducedNoText/.test(job.error || "")
          ? "no extractable text in file" : (job.error || "ingest failed"));
      }
      status.textContent = `Added "${res.title}" — indexed, private to you.`;
      fileInput.value = "";
      titleInput.value = "";
      renderDocuments(grid, me, { replace: true });      // #552: reflect it immediately
    } catch (e) {
      status.textContent = `Could not add it: ${e.message}`;
    } finally { btn.disabled = false; }
  });

  body.append(
    el("div", { class: "up-row" }, fileInput),
    el("div", { class: "up-row" }, titleInput),
    el("div", { class: "up-row" }, btn),
    status,
    el("p", { class: "admin-note" },
      "PDF, Word, PowerPoint, Excel, CSV, Markdown, text or JSON. Up to 10MB. "
      + "New documents are private to you until you share them."),
  );
  grid.appendChild(panel("Add a document", body));
}

/* ---- #607: Shared - every conversation this person has given away ---------------------- */
//
// The owner asked for this in her own words: "all shared convo needs to appear somewhere...
// Who it is shared to, when etc. or can revoke the email, and more importantly, a place to
// manage the database", and "if someone wants to edit it, can go admin tab, a dedicated
// section for those Shared. And they can edit the database, we can re-use the same modal."
//
// Everything else about sharing lives inside the conversation it came from, which is where the
// decision is made. Afterwards the question inverts - "what have I given away, and to whom?" -
// and there is no thread to be inside for that: the owner does not remember her conv_ids. This
// section is that view, and `GET /shares/mine` is its only source. It renders what the server
// says a share opens RIGHT NOW, never what the thread once cited, because those two stop being
// the same set the moment anything narrows and this section exists to make that visible.

const NO_NAME = "(this conversation has no questions in it yet)";
// A share of a thread whose history has gone leaves nothing to name the row with. Saying so is
// better than an empty cell, which reads as the page failing to load a title.

/** "Anyone with the link", or the person it was shared with.
 *
 *  A link row CANNOT be named after a person - there is nobody on the other end until somebody
 *  opens it - which is exactly why link rows carry the open counts instead: an ordinal use
 *  count is the only trace of a visitor there will ever be, and a link opened forty times when
 *  the owner sent it to one person is the only signal she gets that it travelled.
 *
 *  #603: a people row prints the raw `acct_<hex>` rather than the email that was typed. It is a
 *  real defect, it is carded, and it is deliberately NOT patched here - the fix belongs where
 *  the account record is read, and a second surface guessing at an address would be a second
 *  thing to correct later.
 */
function shareAudience(s) {
  return s.audience === "link"
    ? { text: "Anyone with the link", tone: "wide" }
    : { text: s.grantee_oid, tone: "shared" };
}

function shareWhen(s) {
  const bits = [`shared ${relTime(s.created_at)}`];
  bits.push(s.expires_at ? `expires ${new Date(s.expires_at).toLocaleDateString()}`
                         : "no expiry");
  if (s.audience === "link") {
    // Both numbers, always - including zero. "Opened 0 times" is a fact the owner wants (the
    // link has not been used yet); leaving the line out when it is zero would make its absence
    // ambiguous with a row that simply does not report opens.
    const n = s.opens || 0;
    bits.push(`opened ${n} time${n === 1 ? "" : "s"}`);
    bits.push(n ? `last ${relTime(s.last_open_at)}` : "never opened");
  }
  return bits.join(" · ");
}

function sharedRow(s, host, onGone) {
  const drawer = el("div", { class: "shared-drawer" });
  const scopeLabel = el("span", { class: "shared-scope" });
  const paintScope = (n) => {
    scopeLabel.textContent = `${n} document${n === 1 ? "" : "s"}`;
  };
  let scope = s.scope || [];
  paintScope(scope.length);

  const audience = shareAudience(s);

  // ---- Edit: the Ask surface's modal, in edit mode --------------------------------------
  const editBtn = el("button", { type: "button", class: "doc-act shared-edit" }, "Edit");
  editBtn.addEventListener("click", () => {
    host.innerHTML = "";
    host.append(buildShareModal(s.conv_id, [],
      // Every document in `scope` is one this share ALREADY grants, so all of them are
      // shareable by construction - the flag exists for the mint path, where the thread's
      // citations include documents that were never the owner's to pass on.
      { documents: scope.map((d) => ({ ...d, shareable: true })) },
      { edit: s,
        close: () => { host.innerHTML = ""; return true; },
        guard: () => {},
        onNarrowed: () => {
          // Re-read the whole section from the server rather than editing this row in place.
          // The PATCH answers with COUNTS, not with which documents survived (LAW 1), and a
          // scope this page recomputed from its own checklist would be the browser's opinion
          // of an authorization decision - which is exactly the shape that lets a second Edit
          // open on a set the share no longer has. The dialog stays up over the refresh so the
          // owner reads its confirmation before it goes.
          onGone();
        } }));
    // Focus starts inside the dialog, or the trap has nothing to hold: a Tab from the row
    // behind would otherwise walk the page before it ever reached the panel.
    focusFirstIn(host);
  });

  // ---- View: Task 6's question log --------------------------------------------------------
  const asked = s.questions_asked || 0;
  const askedLabel = el("span", { class: "shared-asked" },
    `asked ${asked} question${asked === 1 ? "" : "s"}`);
  const viewBtn = el("button", { type: "button", class: "doc-act shared-view" }, "View");
  let logOpen = false;
  viewBtn.addEventListener("click", async () => {
    if (logOpen) { drawer.innerHTML = ""; logOpen = false; return; }
    logOpen = true;
    drawer.innerHTML = "";
    drawer.append(el("div", { class: "admin-muted" }, "Loading…"));
    try {
      const log = await shareQuestions(s.conv_id, s.share_id);
      if (!logOpen) return;
      drawer.innerHTML = "";
      const rows = log.questions || [];
      if (!rows.length) {
        // Two different silences, and the owner can act on only one of them. A people share
        // has nothing to log by design (its recipient signs in and asks under her own account,
        // and those questions are hers); a link nobody has used yet may still be used.
        drawer.append(el("div", { class: "admin-muted" },
          s.audience === "link"
            ? "Nobody has asked anything through this link yet."
            : "Questions asked by a named person are their own, not yours to read."));
        return;
      }
      drawer.append(el("div", { class: "admin-muted" },
        `${log.visitors} visitor${log.visitors === 1 ? "" : "s"}, `
        + `${rows.length} question${rows.length === 1 ? "" : "s"}.`));
      // #891: the rows scroll inside their own box. The count line above stays put, so it is
      // still readable once the log is long enough to need scrolling.
      const logList = el("div", { class: "shared-drawer-log" });
      rows.forEach((q) => logList.append(el("div", { class: "audit-row" },
        el("div", { class: "audit-q" }, q.question),
        el("div", { class: "audit-meta" },
          `visitor ${q.visitor} · ${relTime(q.asked_at)}`))));
      drawer.append(logList);
    } catch (e) {
      drawer.innerHTML = "";
      drawer.append(el("div", { class: "share-err" }, e.message));
    }
  });

  // ---- Revoke: the existing DELETE, unchanged ---------------------------------------------
  const revoke = el("button", { type: "button", class: "share-revoke shared-revoke" }, "Revoke");
  revoke.addEventListener("click", async () => {
    revoke.disabled = true;
    revoke.textContent = "Revoking…";
    try {
      await revokeConversationShare(s.share_id);
      // Re-read rather than hiding the row locally: the server is the one that knows what is
      // left, and a row removed in the browser while the revoke half failed would be the same
      // lie the delete path documents.
      onGone();
    } catch (e) {
      revoke.disabled = false;
      revoke.textContent = "Revoke";
      drawer.innerHTML = "";
      drawer.append(el("div", { class: "share-err" }, e.message));
    }
  });

  return el("article", { class: "shared-row" },
    el("div", { class: "shared-head" },
      el("h3", { class: "shared-name" }, s.first_question || NO_NAME),
      el("span", { class: `doc-audience doc-audience-${audience.tone}` }, audience.text)),
    el("div", { class: "shared-when" }, shareWhen(s)),
    el("div", { class: "shared-acts" },
      scopeLabel, editBtn, askedLabel, viewBtn, revoke),
    drawer);
}

async function renderShared(grid, { replace = false } = {}) {
  let body = replace ? document.getElementById("admin-shared") : null;
  let host = document.getElementById("shared-modal");
  if (body) {
    body.innerHTML = "Loading…";
  } else {
    body = el("div", { id: "admin-shared" }, "Loading…");
    // Its own modal host, empty when closed - the `:empty` rule in app.css is what keeps a
    // fixed overlay from sitting over the page eating clicks while nothing is being edited.
    // Wired ONCE, here at creation, not on every repaint: `renderShared({replace:true})` reuses
    // this same node, and re-wiring it would stack a second keydown listener per narrowing.
    //
    // There is no `closeGuard` equivalent to consult. The share modal has one because its
    // copy-link view holds a token the server will never return again; the edit dialog holds
    // nothing unrecoverable - a narrowing has already been applied by the time anything is
    // shown - so dismissing it can be unconditional here without weakening that guard there.
    host = el("div", { class: "share-modal-backdrop", id: "shared-modal" });
    wireModalHost(host, {
      isOpen: () => host.childElementCount > 0,
      onDismiss: () => { host.innerHTML = ""; },
    });
    grid.append(panel("Shared", el("div", {}, body, host),
      { wide: true,
        sub: "Conversations you have shared, what each one opens, and what has been asked "
           + "through it." }));
  }
  try {
    const data = await myShares();
    const shares = (data && data.shares) || [];
    body.innerHTML = "";
    if (!shares.length) {
      body.append(el("div", { class: "admin-empty" },
        "You have not shared any conversations. Share one from Ask, and it appears here."));
      return;
    }
    shares.forEach((s) => body.append(
      sharedRow(s, host, () => renderShared(grid, { replace: true }))));
  } catch (e) { fail(body, e); }
}

async function renderMyQuestions(grid) {
  const body = el("div", { id: "admin-audit", class: "audit-list" }, "Loading…");
  const p = panel("Questions you have asked", body,
    { sub: "Every answer is recorded against the person who asked it." });
  grid.append(p);
  try {
    // #593: /me/questions, not /admin/audit. This panel sits in the OWNER's half of the page
    // but used to call an operator-only route, so for every ordinary user - the people whose
    // questions these are - it could only ever render a 403. It also asked for the newest 25
    // rows deployment-wide and filtered them HERE, which meant a colleague asking anything
    // pushed the owner's own history out of the window and the panel said "No questions yet."
    // The server now filters by the verified caller, then limits.
    const mine = await myQuestions(25);
    body.innerHTML = "";
    if (!mine.length) {
      body.append(el("div", { class: "admin-empty" }, "No questions yet."));
      return;
    }
    mine.forEach((r) => {
      body.append(el("div", { class: "audit-row" },
        el("div", { class: "audit-q" }, r.question),
        el("div", { class: "audit-meta" },
          `${relTime(r.ts)} · answered from ${r.n_authorized} document(s)`)));
    });
  } catch (e) { fail(body, e); }
}

/* ---- operator-only: facts about the DEPLOYMENT, not about the owner ------------------ */

async function renderIndex(grid) {
  const body = el("div", { id: "admin-index" }, "Loading…");
  const p = panel("Index health", body);
  grid.append(p);
  try {
    const h = await adminIndex();
    body.innerHTML = "";
    body.append(
      kv("Backend", h.backend), kv("Documents", h.doc_count), kv("Chunks", h.chunk_count),
      kv("Embedding model", h.embedding_model), kv("Embedding dim", h.embedding_dim),
      kv("Last index", h.last_index_ts ? relTime(h.last_index_ts) : "—"));
  } catch (e) {
    // #589: the pgvector backend does not implement index_health and answers 501. It used to
    // print "Not reported by this backend." - a panel whose entire content was an admission
    // that it had nothing to say. A section with no content is removed, not rendered empty.
    // (The #392 rule still holds: unknown is not empty. Saying nothing is how you say unknown.)
    if (/\b501\b/.test(e.message || "")) { p.remove(); return; }
    fail(body, e);
  }
}

async function renderIdentities(grid) {
  const body = el("div", { id: "admin-identities" }, "Loading…");
  const p = panel("Users and groups", body,
    { sub: "Principals this deployment has resolved. Group membership comes from your directory." });
  grid.append(p);
  try {
    const d = await adminIdentities();
    body.innerHTML = "";
    if (!(d.groups || []).length && !(d.users || []).length) { p.remove(); return; }
    if ((d.groups || []).length) {
      body.append(el("h3", { class: "admin-sub" }, "Groups"));
      d.groups.forEach((g) =>
        body.append(kv(g.group_oid, `${g.member_count} member(s) · ${g.doc_count} doc(s)`)));
    }
    if ((d.users || []).length) {
      body.append(el("h3", { class: "admin-sub" }, "Users"));
      d.users.forEach((u) =>
        body.append(kv(u.principal_oid, (u.group_oids || []).join(", ") || "—")));
    }
  } catch (e) { fail(body, e); }
}

async function renderPermissionTester(grid) {
  const out = el("div", { class: "ptest-out", id: "ptest-out" });
  const userSel = el("select", { id: "ptest-user", class: "ptest-user" });
  const q = el("input", { id: "ptest-q", class: "ptest-q", type: "text",
    placeholder: "question (optional)", autocomplete: "off" });
  const run = el("button", { id: "ptest-run", type: "button",
    onclick: () => doTest(userSel.value, q.value, out) }, "Preview as this person");
  const p = panel("Check what someone can see", el("div", {},
    el("div", { class: "ptest-controls" }, userSel, q, run), out),
    { sub: "Runs a real permission-trimmed retrieval as another principal." });
  grid.append(p);
  try {
    const cfg = await getConfig();
    const users = cfg.users || [];
    // An empty selector cannot test anything. On a real deployment `users` is a dev-only
    // list, so the control renders as a blank dropdown next to a button that does nothing -
    // exactly the kind of dead affordance this rebuild is removing.
    if (!users.length) { p.remove(); return; }
    users.forEach((u) => userSel.append(el("option", { value: u }, u)));
  } catch { p.remove(); }
}

async function doTest(userOid, question, out) {
  out.innerHTML = "Running…";
  try {
    const r = await adminPermissionTest(userOid, question);
    out.innerHTML = "";
    out.append(el("div", { class: "ptest-summary" },
      `${userOid}: ${r.authorized_count} visible · ${r.denied_count} denied`));
    r.results.forEach((row) =>
      out.append(el("div", { class: row.returned ? "ptest-row ok" : "ptest-row no" },
        `${row.returned ? "✓" : "✕"} ${row.title || row.doc_external_id}`
        + (row.returned ? ` — via ${row.matched_principals.join(", ")}` : " — denied"))));
  } catch (e) { fail(out, e); }
}

// #589: the counters used to render as their raw keys - `queries_served`, `authorized_docs`,
// `docs_indexed`. Those are variable names, and a page that shows a customer its own variable
// names has stopped being a product surface.
const COUNTER_LABEL = {
  queries_served: "Questions answered",
  authorized_docs: "Documents returned",
  sources_synced: "Source syncs run",
  docs_indexed: "Documents indexed",
  chunks_created: "Passages indexed",
};

async function renderTelemetry(grid) {
  const body = el("div", { id: "admin-telemetry" }, "Loading…");
  const p = panel("Usage", body, { sub: "Counters only. No question text, no document content." });
  grid.append(p);
  try {
    const t = await adminTelemetry();
    const counts = Object.entries(t.counts || {});
    const cost = Object.entries(t.cost || {});
    body.innerHTML = "";
    if (!counts.length && !cost.length) { p.remove(); return; }
    counts.forEach(([k, v]) => body.append(kv(COUNTER_LABEL[k] || k, v)));
    cost.forEach(([k, v]) => body.append(kv(`Cost: ${k}`, v)));
  } catch (e) { fail(body, e); }
}

export async function mountAdmin(root) {
  const grid = el("div", { class: "admin-grid", id: "admin-grid" });

  // #549: several panels report on the WHOLE deployment and are operator-only, so for anyone
  // else they answered 403 and printed "Error: admin/index failed: 403" repeatedly. A refusal
  // rendered as a broken panel reads as "this product is broken", not "this is not yours to
  // see" - so ask /config who this caller is and render only their half. The gate is the
  // SERVER's; this is presentation. Never the reverse (ADR 0011 s3).
  let operator = true;
  let me = "";
  try {
    const [cfg, who] = await Promise.all([getConfig(), authMe()]);
    operator = cfg.operator !== false;
    me = (who && who.oid) || "";
    _signedIn = !!(who && who.signed_in);   // #593: so a refusal is explained in the right terms
    root.append(el("div", { class: "admin-head" },
      el("h1", {}, "Your data"),
      el("p", { class: "admin-lede" },
        who && who.name
          ? `Signed in as ${who.name}. Everything below is scoped to what you are allowed to see.`
          : "Everything below is scoped to what you are allowed to see.")));
  } catch (_) {
    root.append(el("div", { class: "admin-head" }, el("h1", {}, "Your data")));
  }
  root.append(grid);

  // The owner's own material, in the order they care about it.
  renderDocuments(grid, me);
  renderUpload(grid, me);
  // #607: between her own documents and her own questions, because that is what it is about -
  // what she has given away out of the first, and what strangers asked with it.
  renderShared(grid);
  renderMyQuestions(grid);

  if (operator) {
    const opHead = el("div", { class: "admin-op-head" },
      el("h2", {}, "Deployment"),
      el("p", { class: "admin-lede" },
        "Visible to operators of this deployment. These describe the box, not your documents."));
    root.append(opHead);
    const opGrid = el("div", { class: "admin-grid", id: "admin-op-grid" });
    root.append(opGrid);
    renderStores(opGrid);
    renderSources(opGrid);
    renderIndex(opGrid);
    renderIdentities(opGrid);
    renderPermissionTester(opGrid);
    renderTelemetry(opGrid);
  }
}
