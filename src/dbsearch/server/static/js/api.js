// src/dbsearch/server/static/js/api.js
// Thin client over the REST contract. Identity rides the X-DBSearch-User header (dev);
// in prod the server derives identity from the verified token and ignores any header (LAW 2).
let _user = null;
let _model = "";

export function setUser(u) { _user = u || null; }
export function getUser() { return _user; }
export function setModel(m) { _model = m || ""; }   // #43: selected generation model
export function getModel() { return _model; }

function headers() {
  const h = { "Content-Type": "application/json" };
  if (_user) h["X-DBSearch-User"] = _user;
  return h;
}

// #392: what Ask may honestly offer before the user types. Returns null on 401 so the
// caller can render "sign in" rather than an error, and null on any other failure so an
// unreachable endpoint degrades to showing nothing extra - never to a false "no documents".
export async function askSuggestions() {
  try {
    const r = await fetch("/ask/suggestions", { headers: headers(), credentials: "same-origin" });
    if (r.status === 401) return { unauthenticated: true };
    if (!r.ok) return null;
    return r.json();   // {known, indexed, authorized_docs, examples}
  } catch { return null; }
}

export async function search(question) {
  const r = await fetch("/search", {
    method: "POST", headers: headers(), body: JSON.stringify({ question, model: _model }),
  });
  if (!r.ok) throw new Error(`search failed: ${r.status}`);
  return r.json();   // {answer, citations:[{doc,title,uri}], authorized_docs:[...]}
}

export async function chat(convId, question) {
  const r = await fetch("/chat", {
    method: "POST", headers: headers(),
    body: JSON.stringify({ conv_id: convId, question, model: _model }),
  });
  if (!r.ok) throw new Error(`chat failed: ${r.status}`);
  return r.json();   // {answer, citations, authorized_docs, conv_id}
}

// Streaming chat (#50): onToken(text) fires per token; onDone({answer,citations,authorized_docs}) at the end.
export async function chatStream(convId, question, onToken, onDone) {
  const r = await fetch("/chat/stream", {
    method: "POST", headers: headers(),
    body: JSON.stringify({ conv_id: convId, question, model: _model }),
  });
  if (!r.ok || !r.body) throw new Error(`chat failed: ${r.status}`);
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "", sawDone = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 2);
      if (!line.startsWith("data:")) continue;
      const ev = JSON.parse(line.slice(5).trim());
      if (ev.type === "token") onToken(ev.text);
      else if (ev.type === "done") { sawDone = true; onDone(ev); }
      // #952: the server's terminal failure event. Throwing here lands in submit()'s catch,
      // which renders the message and re-enables the input - the alternative was typing dots
      // forever over a stream that had already died.
      else if (ev.type === "error") throw new Error(ev.message || "answer generation failed");
    }
  }
  // #952: the stream ended with neither done nor error - the server died mid-flight (or a
  // proxy cut the body). Resolving silently here is the wedge: no answer, no error, dots
  // forever. An exception is the honest shape; the documents are untouched.
  if (!sawDone) throw new Error("the answer stream ended before the reply - ask again");
}

// #689 (ADR 0025): re-execute a proof a routed answer showed, under the CALLER's own guards.
// The server re-checks gate #1 (an invisible store is a 404, identical to a nonexistent one)
// and the token's binding to this identity before anything runs, so this call can never reach
// a store the clicker cannot see - the button is a convenience, never the authorization.
//
// The error text is the server's own `detail` where there is one, because the two failures a
// person can act on say different things ("proof token invalid - re-ask the question" is a
// stale bubble; a 404 is a store that is no longer there) and collapsing them into "rerun
// failed: 403" tells the reader nothing they can use.
export async function rerunProof({ store_id, sql, token }) {
  const r = await fetch("/router/rerun", {
    method: "POST", headers: headers(),
    body: JSON.stringify({ store_id, sql, token }),
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch (e) { detail = ""; }
    throw new Error(detail || `could not verify this source (${r.status})`);
  }
  return r.json();
}

export async function getConfig() {
  const r = await fetch("/config");
  if (!r.ok) throw new Error(`config failed: ${r.status}`);
  return r.json();
}

export async function adminIndex() {
  const r = await fetch("/admin/index", { headers: headers() });
  if (!r.ok) throw new Error(`admin/index failed: ${r.status}`);
  return r.json();
}

export async function adminIdentities() {
  const r = await fetch("/admin/identities", { headers: headers() });
  if (!r.ok) throw new Error(`admin/identities failed: ${r.status}`);
  return r.json();
}

export async function adminDocuments() {
  const r = await fetch("/admin/documents", { headers: headers() });
  if (!r.ok) throw new Error(`admin/documents failed: ${r.status}`);
  return r.json();   // [{doc_external_id, title, uri, allowed_principals:[...]}]
}

export async function adminTelemetry() {
  const r = await fetch("/admin/telemetry", { headers: headers() });
  if (!r.ok) throw new Error(`admin/telemetry failed: ${r.status}`);
  return r.json();
}

export async function adminAudit(limit = 25) {
  const r = await fetch(`/admin/audit?limit=${encodeURIComponent(limit)}`, { headers: headers() });
  if (!r.ok) throw new Error(`admin/audit failed: ${r.status}`);
  return r.json();   // [{ts, user, question, surface, authorized_docs, n_authorized}, ...] newest-first
}

// #593: the caller's OWN question history. Distinct from adminAudit() above, which is the
// deployment-wide trail and operator-only (#549) - the owner's panel used to call THAT, so it
// 403'd for every ordinary user. The server filters by the verified session, so there is no
// oid parameter here to get wrong and no client-side filter to trust.
export async function myQuestions(limit = 25) {
  const r = await fetch(`/me/questions?limit=${encodeURIComponent(limit)}`,
                        { headers: headers(), credentials: "same-origin" });
  if (!r.ok) throw new Error(`me/questions failed: ${r.status}`);
  return r.json();   // [{ts, user, question, surface, authorized_docs, n_authorized}, ...]
}

export async function adminPermissionTest(userOid, question) {
  const r = await fetch("/admin/permission-test", {
    method: "POST", headers: headers(),
    body: JSON.stringify({ user_oid: userOid, question: question || "" }),
  });
  if (!r.ok) throw new Error(`admin/permission-test failed: ${r.status}`);
  return r.json();
}

// #562: the STORE plane, so Admin can report on the databases and not only the documents.
// Both are already trimmed to the caller server-side (visible_stores / gate #1); the UI must
// never re-filter, only render what it is given.
export async function routerCatalog() {
  const r = await fetch("/router/catalog", { headers: headers() });
  // 409 = nothing composed yet. That is a normal state for a fresh workspace, not a failure,
  // and the caller renders it as such rather than as a red error line.
  if (r.status === 409) return null;
  if (!r.ok) throw new Error(`router/catalog failed: ${r.status}`);
  return r.json();   // {tenant, business_units:[{id, sources:[{id, stores:[...]}]}]}
}

export async function storeSchema(storeId) {
  const r = await fetch(`/router/stores/${encodeURIComponent(storeId)}/schema`,
                        { headers: headers() });
  if (!r.ok) throw new Error(`store schema failed: ${r.status}`);
  return r.json();   // {store_id, title, kind, counts_known, tables:[{table, columns, row_count}]}
}

export async function adminSources() {
  const r = await fetch("/admin/sources", { headers: headers() });
  if (!r.ok) throw new Error(`admin/sources failed: ${r.status}`);
  return r.json();   // [{source_id, kind, display_name, last_sync_at, doc_count, status}]
}

export async function adminDocumentSegments(docId) {
  const r = await fetch(`/admin/documents/${encodeURIComponent(docId)}/segments`, { headers: headers() });
  if (!r.ok) throw new Error(`admin/documents/segments failed: ${r.status}`);
  return r.json();   // [{chunk_id, locator, preview}]
}

export async function adminResync(sourceId) {
  const r = await fetch("/admin/resync", {
    method: "POST", headers: headers(),
    body: JSON.stringify({ source_id: sourceId }),
  });
  if (!r.ok) throw new Error(`admin/resync failed: ${r.status}`);
  // #695: SUBMITS a crawl and returns 202 with a job handle - NOT the updated summary.
  // #569 made this asynchronous (a full re-crawl has no business inline in a request, LAW 4)
  // and this comment used to still say "the updated source summary", so the caller read
  // fields that were never there and rendered "undefined doc(s)". Follow `poll`, then re-read
  // adminSources() for the settled row.
  return r.json();   // {source_id, job_id, job_status, poll}
}

export async function ingestJob(jobId) {
  const r = await fetch(`/ingest/jobs/${encodeURIComponent(jobId)}`, { headers: headers() });
  if (!r.ok) throw new Error(`ingest/jobs failed: ${r.status}`);
  // {job_id, source_id, status, phase, docs_done, docs_total, docs_skipped, error}
  return r.json();
}

// Multipart upload — do NOT set Content-Type (browser sets the multipart boundary).
export async function uploadDocument(file, acl, title) {
  const fd = new FormData();
  fd.append("file", file);
  (acl || []).forEach((g) => fd.append("acl", g));
  if (title) fd.append("title", title);
  const h = {};
  const u = getUser();
  if (u) h["X-DBSearch-User"] = u;
  const r = await fetch("/admin/upload", { method: "POST", headers: h, body: fd });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json(); // #917: 202 submit — {external_id, title, acl, job_id, job_status, poll}
}

export async function draftProposal(brief) {
  const r = await fetch("/draft", {
    method: "POST", headers: headers(), body: JSON.stringify({ brief }),
  });
  if (!r.ok) throw new Error(`draft failed: ${r.status}`);
  return r.json();   // {brief, plan:[...], sections:[{title, prose, citations, authorized_docs}]}
}

// #57/#59 two-phase conversational draft. intent ∈ chat|ready|confirm|cancel.
// Returns {state, reply, requirements, draft}. The strong model only runs on "confirm".
export async function draftTurn(convId, message, intent) {
  const r = await fetch("/draft/turn", {
    method: "POST", headers: headers(),
    body: JSON.stringify({ conv_id: convId, message: message || "", intent: intent || "chat" }),
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();
}

// #61 streaming confirm: onEvent(ev) fires per SSE event
// (plan | section_start | token | section_done | done | error).
export async function draftStream(convId, onEvent) {
  const r = await fetch("/draft/stream", {
    method: "POST", headers: headers(),
    body: JSON.stringify({ conv_id: convId, intent: "confirm" }),
  });
  if (!r.ok || !r.body) throw new Error(`draft stream failed: ${r.status}`);
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 2);
      if (!line.startsWith("data:")) continue;
      onEvent(JSON.parse(line.slice(5).trim()));
    }
  }
}

export async function createKey(label) {
  const r = await fetch("/developer/keys", {
    method: "POST", headers: headers(), body: JSON.stringify({ label }),
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();   // {record, token}  — token shown once
}

export async function developerKeys() {
  const r = await fetch("/developer/keys", { headers: headers() });
  if (!r.ok) throw new Error(`developer/keys failed: ${r.status}`);
  return r.json();   // [record, ...]
}

export async function revokeKey(id) {
  const r = await fetch(`/developer/keys/${encodeURIComponent(id)}`, {
    method: "DELETE", headers: headers(),
  });
  if (!r.ok) throw new Error(`revoke failed: ${r.status}`);
  return r.json();
}

// #589: who am I, so the Documents list can say "Only you" instead of printing the caller
// their own oid back. Returns null rather than throwing - a page that cannot name the user
// should degrade to slightly vaguer copy, never to an error panel.
export async function authMe() {
  try {
    const r = await fetch("/auth/me", { credentials: "same-origin" });
    return r.ok ? r.json() : null;
  } catch (_) { return null; }
}

// #775: what this account may store, what it is using, and what it could buy.
//
// Same stance as authMe() and for the same reason: it returns null rather than throwing. A
// billing lookup that fails must not take the account panel down with it - the panel's real
// job is the connected-sources roster, and a missing storage row is a smaller failure than an
// error where the roster should be.
export async function billingStatus() {
  try {
    const r = await fetch("/billing/status", { credentials: "same-origin" });
    return r.ok ? r.json() : null;
  } catch (_) { return null; }
}

// #775: start a subscription, or open the Stripe-hosted portal to change/cancel one.
//
// Both THROW, deliberately, unlike billingStatus above. These are clicks on a button the user
// pressed on purpose: a checkout that silently does nothing leaves somebody staring at a page
// wondering whether they have been charged, which is the worst possible ambiguity to leave
// around money. The caller shows the error.
export async function billingCheckout(tier) {
  const r = await fetch("/billing/checkout", {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tier }),
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
}

export async function billingPortal() {
  const r = await fetch("/billing/portal", { method: "POST", credentials: "same-origin" });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
}

// #592: end the session for real. The shell's "Sign out" was `location.href = "/"` and
// nothing else, so the cookie stayed valid for its full 8h and the vaulted refresh token was
// never dropped - on a shared machine the next person was still signed in as the last one.
//
// Deliberately the OPPOSITE stance to authMe() above: this throws. A sign-out that fails
// quietly is worse than one that fails loudly, because the user walks away believing it
// worked. The caller must not navigate unless this resolves.
export async function signOut() {
  const r = await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
  if (!r.ok) throw new Error(`sign out failed: ${r.status}`);
  return r.json();
}

// #652: forget ONE cloud's credential. Throws for the same reason signOut does - a
// revocation that fails quietly is the worst kind, because the row goes back to "Not
// connected" and the user walks away believing DBSearch can no longer read their data.
// Returns the server's own `linked` list so the caller repaints from the vault rather than
// from an assumption about what the click achieved.
export async function disconnectProvider(idp) {
  const r = await fetch(`/auth/disconnect/${encodeURIComponent(idp)}`,
                        { method: "POST", credentials: "same-origin" });
  if (!r.ok) throw new Error(`disconnect failed: ${r.status}`);
  return r.json();
}

// ADR 0024: link AWS by handing the server the caller's own access keys, once. The server
// FALSIFIES them against sts:GetCallerIdentity before vaulting, so a resolve here means the
// keys really answered as somebody - the response carries that identity (account + arn) and
// the vault's own `linked` list to repaint from. A rejection carries AWS's reason, which is
// the one message the user can act on; surface it, never a bare status code.
export async function connectAws(accessKeyId, secretAccessKey) {
  const r = await fetch("/auth/aws/connect", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ access_key_id: accessKeyId, secret_access_key: secretAccessKey }),
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch (_) { /* non-JSON error body */ }
    throw new Error(detail || `connect failed: ${r.status}`);
  }
  return r.json();
}

// #589: the LIVE grants on a document (ADR 0017). Needed because a document's ACL keeps a
// `grant:<id>` principal forever by design - revocation deletes the grant record, never the
// ACL entry - so the ACL alone cannot say how many people a document is really shared with.
// Reading the registry is the only honest count.
export async function documentGrants(docId) {
  const r = await fetch(`/documents/${encodeURIComponent(docId)}/grants`,
                        { headers: headers(), credentials: "same-origin" });
  if (!r.ok) throw new Error(`grants failed: ${r.status}`);
  return r.json();
}

// #594: remove a document you OWN. The server decides ownership from the verified session and
// answers 404 - never 403 - for anything else, so this cannot be used to probe for documents.
export async function deleteDocument(docId) {
  const r = await fetch(`/documents/${encodeURIComponent(docId)}`,
                        { method: "DELETE", headers: headers(), credentials: "same-origin" });
  if (!r.ok) {
    let detail = `delete failed: ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();   // {deleted, grants_dropped, blob_prefixes_left}
}

export async function shareDocument(docId, granteeOid, expiresInDays) {
  const body = { grantee_oid: granteeOid };
  if (expiresInDays) body.expires_in_days = expiresInDays;
  const r = await fetch(`/documents/${encodeURIComponent(docId)}/grants`, {
    method: "POST", headers: headers(), credentials: "same-origin", body: JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();
}

export async function revokeShare(grantId) {
  const r = await fetch(`/grants/${encodeURIComponent(grantId)}`, {
    method: "DELETE", headers: headers(), credentials: "same-origin",
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();
}

// #600: share a CONVERSATION, not a document. The route mints one conv-scoped grant per
// document the thread has cited so far and a transcript row - both a SNAPSHOT at share
// time (a turn asked afterwards is not included; sharing again updates the boundary, and
// can narrow what a recipient already saw). Response carries `documents` (how many grants
// landed) and `turns_withheld` (how many of the sharer's OWN turns did not travel because
// they drew on a document she cannot pass on) - both are for the SHARER's eyes only.
// #606 / #610: the SAME route, two audiences and an owner-chosen scope.
//
// `audience` selects a code path and is never an authorization fact (the server says so at
// length): "people" mints a named grant somebody signs in to reach, "link" mints an
// unguessable token anybody holding it can open. `exclude_docs` can only NARROW - it is
// subtracted server-side from a set the server computed, so nothing sent from here can widen
// a share, and the modal's checklist has no way to add a document even if it wanted to.
//
// THE `url` FIELD COMES BACK EXACTLY ONCE, on a link share, and is the only moment the
// plaintext token exists outside the browser that asked for it. The row keeps a SHA-256
// digest, so no later read can hand it back: a caller that drops this response has lost the
// link, and the only remedy is minting a new one. Callers must render it, not log it.
export async function shareConversation(convId, opts = {}) {
  const { audience = "people", email = "", granteeOid = "",
          expiresInDays = 0, excludeDocs = [], excludeStores = [] } = opts || {};
  const body = { audience };
  if (email) body.grantee_email = email;
  if (granteeOid) body.grantee_oid = granteeOid;
  if (expiresInDays) body.expires_in_days = expiresInDays;
  // Only when non-empty: an empty list and an absent field mean the same thing to the route,
  // and sending [] on every share would make the "you unchecked everything" refusal harder
  // to tell apart from "the server refused your documents".
  if (excludeDocs && excludeDocs.length) body.exclude_docs = excludeDocs;
  // #851: the SOURCES the owner unticked, sent under the same rule and for the same reason -
  // absent when empty, so "you unchecked everything" stays distinguishable from the server's
  // own refusal.
  if (excludeStores && excludeStores.length) body.exclude_stores = excludeStores;
  const r = await fetch(`/conversations/${encodeURIComponent(convId)}/shares`, {
    method: "POST", headers: headers(), credentials: "same-origin", body: JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();   // {share_id, conv_id, grantor_oid, grantee_oid, expires_at, created_at,
                      //  turn_cutoff, live, audience, documents, turns_withheld, url?}
}

// #610: what a share of this thread WOULD expose, before one exists. This is what the modal's
// checklist renders, and it is computed by the same two calls the share POST makes, so the
// owner can never be shown a set the mint would not use.
//
// `shareable: false` rows are LISTED, not filtered here or anywhere else: they are documents
// the caller only holds through somebody else's grant (ADR 0017 s2), and dropping them would
// leave her counting fewer documents than her own transcript visibly drew on.
export async function shareableDocs(convId) {
  const r = await fetch(`/conversations/${encodeURIComponent(convId)}/shareable`,
                        { headers: headers(), credentials: "same-origin" });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();   // {documents: [{id, title, shareable}], turns}
}

// Who THIS caller has shared THIS conversation with - never somebody else's share of it.
export async function conversationShares(convId) {
  const r = await fetch(`/conversations/${encodeURIComponent(convId)}/shares`,
                        { headers: headers(), credentials: "same-origin" });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();   // {shares: [{share_id, conv_id, grantor_oid, grantee_oid, expires_at,
                      //  created_at, turn_cutoff, live}, ...]}
}

export async function revokeConversationShare(shareId) {
  const r = await fetch(`/conversations/shares/${encodeURIComponent(shareId)}`, {
    method: "DELETE", headers: headers(), credentials: "same-origin",
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();   // {revoked, grants_dropped}
}

// #607: every live share THIS caller has granted, across every conversation, both audiences.
// The per-conversation list above is what the share modal reads while the owner is inside a
// thread; this is the management surface's list, and it is the only one that can answer "what
// have I given away?" - the owner has no list of conv_ids to iterate to get there.
//
// Each row carries `first_question` (the thread's opening question, truncated - the row's human
// name), `scope` (the documents this share ACTUALLY grants right now, not the ones the thread
// cited) and `questions_asked` (how many questions strangers have asked through a link).
export async function myShares() {
  const r = await fetch("/shares/mine", { headers: headers(), credentials: "same-origin" });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();   // {shares: [{...share, first_question, scope: [{id,title}], questions_asked}]}
}

// #608: take documents back out of a share that is already live. REMOVE ONLY - there is no add
// key here and there is none on the server, so nothing sent from this client can widen a share.
// The removed documents' conv-scoped grants are dropped and the share's turn boundary is
// narrowed, both immediately. Precisely what that buys, because the loose version of this
// sentence ("refused on their very next request") was not true of everything: somebody already
// holding the link can no longer RETRIEVE the removed document, and no longer receives the
// shared turn that drew on it, from their very next request onwards. What it does NOT do is
// retract an answer they were already given - that sits in their own fork of the thread and
// stays there, unlike a REVOKE, which 404s the whole page including the fork. The asymmetry is
// carded as #614.
// Narrowing to nothing is a 400 telling the owner to revoke instead, never a live share that
// opens nothing.
export async function narrowShareScope(shareId, removeDocs) {
  const r = await fetch(`/shares/${encodeURIComponent(shareId)}/scope`, {
    method: "PATCH", headers: headers(), credentials: "same-origin",
    body: JSON.stringify({ remove_docs: removeDocs || [] }),
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();   // {...share, removed, documents}
}

// #611: what strangers asked through one link. Questions and timestamps only - the route never
// selects the answer column, so there is no answer text here to render by accident. A people
// share answers an empty list rather than an error.
export async function shareQuestions(convId, shareId) {
  const r = await fetch(`/conversations/${encodeURIComponent(convId)}/shares/`
                        + `${encodeURIComponent(shareId)}/questions`,
                        { headers: headers(), credentials: "same-origin" });
  if (!r.ok) {
    let detail = `${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();   // {questions: [{question, asked_at, visitor}], visitors}
}

// #602: the caller's OWN threads, newest first - the door back into a conversation after a
// reload. The mirror of sharedWithMe() below, and deliberately the same shape: both feed
// conversationTranscript(), so the owner and a grantee reopen a thread the same way.
export async function myConversations() {
  const r = await fetch("/conversations/mine",
                        { headers: headers(), credentials: "same-origin" });
  if (!r.ok) throw new Error(`conversations failed: ${r.status}`);
  return r.json();   // {conversations: [{conv_id, first_question, turns, last_asked_at}]}
}

// The threads OTHERS have opened to this caller, live ones only.
export async function sharedWithMe() {
  const r = await fetch("/conversations/shared-with-me",
                        { headers: headers(), credentials: "same-origin" });
  if (!r.ok) throw new Error(`shared-with-me failed: ${r.status}`);
  return r.json();   // {shares: [...]}
}

// #600: read a shared conversation. Returns null on 404, the same "render the state
// honestly instead of an error line" idiom routerCatalog() uses for its own 409 - here the
// caller is the surface, and a 404 means the share is no longer live (revoked or expired),
// never a blank page.
export async function conversationTranscript(convId) {
  const r = await fetch(`/conversations/${encodeURIComponent(convId)}/transcript`,
                        { headers: headers(), credentials: "same-origin" });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`transcript failed: ${r.status}`);
  return r.json();   // {turns: [{seq, question, answer, own}], own}
}

export async function openapi() {
  const r = await fetch("/openapi.json");
  if (!r.ok) throw new Error(`openapi failed: ${r.status}`);
  return r.json();
}

export async function graphqlSchema() {
  const r = await fetch("/developer/graphql-schema");
  if (!r.ok) throw new Error(`graphql-schema failed: ${r.status}`);
  return (await r.json()).sdl;
}

// The four sp* helpers that lived here (spStatus / spConsentUrl / spDrives /
// spFinish, from the #148 in-app connector) were removed with the legacy
// #/connectors dashboard (#407). The canvas drives the same
// /connectors/sharepoint/* endpoints directly, so the endpoints remain; only
// this shell's unused wrappers are gone.
