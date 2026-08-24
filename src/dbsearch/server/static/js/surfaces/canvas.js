// src/dbsearch/server/static/js/surfaces/canvas.js
//
// Connectors, as a view of the shell rather than a second front-end (#643).
//
// THE DEFECT. Moving from Ask to Connectors was a full document load and looked like one:
// the page blanked, 56KB came down the wire, the topbar changed from "self-host · pgvector
// -ollama / No content leaves your cloud / Model / avatar" to "Data Canvas / Your databases
// / Malcolm Tan signed in / Sign out", and the rail even changed width (183px to 203px,
// because canvas.html redefined seven of tokens.css's variables). Ask to Draft, by contrast,
// is a pushState and a re-render. The owner described the difference exactly - one felt like
// a hard refresh, the other like a tab switch - and diagnosed it from the topbar alone.
//
// It was never a bug in the router. /canvas served canvas.html: a 2720-line document with its
// own <title>, its own 513-line stylesheet, its own topbar and its own identity chip. The
// router could not intercept a navigation to a document the shell does not contain, and was
// right not to try. #634 and #638 made the load LOOK better - they held the rail's column
// open and picked the view before first paint - and neither could make it stop being a load.
//
// So the canvas moved in here. Its markup is the MARKUP template below, its stylesheet is
// css/canvas.css scoped under `.canvas-surface`, and this function is canvas.html's IIFE
// with four changes and nothing else:
//
//   1. it takes a `root` and paints into it, instead of owning <body>
//   2. the topbar's wordmark, theme toggle and identity chip are GONE - the shell's rail and
//      account control own all three, which is what #414's open subtask asked for. The four
//      canvas verbs and the "Connect Google" grant affordance stay, in .cv-head.
//   3. every listener it hangs off window/document is tracked and removed on unmount, because
//      a surface that outlives its own teardown is a surface that acts on the next one
//   4. theme is OBSERVED rather than owned: the account control flips data-theme, and a
//      MutationObserver repaints the canvas. Nodes are drawn with colours read at render
//      time, so without this the canvas keeps its old palette until the next full render.
//
// The stale-build self-heal that used to sit above the IIFE is not here: it is a property of
// the DOCUMENT, not of this surface, so it moved to index.html's head where it now covers
// every surface instead of only this one.

// #689 (ADR 0025): the Sources rail lives in ui/proofs.js now, so /ask explains a routed
// answer exactly the way this surface does. MOVED, not copied - see that module's header.
import { esc, humanizeSnippet, sourcesBlockHTML, wireProofActions } from "../ui/proofs.js";
import { wireModalHost, focusFirstIn } from "../ui/modal.js";

const MARKUP = `
  <header class="cv-head">
    <div class="cv-title">
      <h1>Connectors</h1>
      <div class="sub mono">Your databases</div>
    </div>
    <div class="spacer"></div>
    <!-- #643: identity is the shell's account control. What is left here is the one grant
         affordance that control deliberately points AT rather than duplicating - account.js
         renders a "Connect" link to /canvas for a provider you have not granted yet, and
         this is what it lands on. -->
    <div id="authArea" class="autharea"></div>
    <button class="btn" id="reset" title="Load the live demo manifest from the server">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 4v6h6M20 20v-6h-6"/><path d="M20 10a8 8 0 0 0-14.9-3M4 14a8 8 0 0 0 14.9 3"/></svg>
      Live demo
    </button>
    <button class="btn" id="setupChat" title="Describe your sources in plain language — the setup agent builds and applies the manifest (#116)">⚡ Setup by chat</button>
    <button class="btn" id="compose" title="POST this canvas as a manifest to /router/compose">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 19V5M6 11l6-6 6 6"/></svg>
      Compose up
    </button>
    <button class="btn primary" id="export">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v12M8 11l4 4 4-4M5 21h14"/></svg>
      Export stores.yml
    </button>
  </header>

  <div class="main">
    <aside class="rail" id="rail">
      <div class="eyebrow">Add a source</div>
      <!-- kind buttons injected -->
      <div class="rail-note">Each node is a database. Click <b>+</b> to drop one onto the
      canvas, then wire its connection — DBSearch routes each query to the right one.</div>
    </aside>

    <section class="canvas" id="canvas">
      <div class="world" id="world">
        <svg class="edges" id="edges"></svg>
        <div class="hub" id="hub">
          <div class="core">
            <div class="ring" aria-hidden="true"></div>
            <b>DBSearch Router</b>
            <span>query → store</span>
          </div>
        </div>
        <!-- nodes injected -->
      </div>
      <div class="qdock" id="qdock">
        <div class="qrow">
          <select id="quser" title="Ask as this user (dev auth)"></select>
          <input id="qtext" placeholder="Ask across every composed store — e.g. “what is our parental leave policy”" spellcheck="false">
          <button class="btn" id="qroute" title="Advisor: which store would answer this — scores + why, without executing">Route</button>
          <button class="btn primary" id="qask">Ask</button>
        </div>
        <div class="qresult proof-host" id="qresult"></div>
      </div>
      <div class="zoomctl" id="zoomctl">
        <button id="zoomOut" title="Zoom out">−</button>
        <div class="zpct" id="zoomPct" title="Reset to 100%">100%</div>
        <button id="zoomIn" title="Zoom in">+</button>
        <div class="zsep"></div>
        <button class="zfit" id="zoomFit" title="Fit all sources in view">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>Fit</button>
      </div>
      <div class="sp-toast" id="spToast"></div>
    </section>

    <aside class="panel" id="panel"><!-- config injected --></aside>
  </div>

  <footer class="statusbar" id="statusbar"></footer>

  <div class="provmenu" id="provmenu"></div>
  <div class="ctxmenu" id="ctxmenu"></div>
  <div class="sp-picker" id="spPicker">
    <div class="sp-card" role="dialog" aria-modal="true" aria-labelledby="spPickerTitle">
      <div class="sp-head"><b id="spPickerTitle">Add SharePoint documents</b>
        <button class="btn icon" id="spPickerClose" aria-label="Close">✕</button></div>
      <!-- #880: the progress panel is a SIBLING of the pickable list, not something the list
           renders. A run outlives the list it was started from (and the modal itself), so it
           cannot live inside markup that re-renders when the libraries arrive. -->
      <div class="sp-progress-host" id="spProgress"></div>
      <div class="sp-body" id="spPickerBody"></div>
    </div>
  </div>
  <div class="scrim" id="scrim"></div>
  <aside class="drawer" id="drawer" aria-hidden="true">
    <div class="dhead">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="var(--accent)" stroke-width="1.8"><path d="M4 7h16M4 12h16M4 17h10"/></svg>
      <b>stores.yml</b>
      <span class="fn">— generated from this canvas</span>
      <div class="spacer"></div>
      <button class="btn icon" id="closeDrawer" aria-label="Close">✕</button>
    </div>
    <pre class="yaml" id="yaml"></pre>
    <div class="dfoot">
      <button class="btn primary" id="copyYaml">Copy manifest</button>
      <span class="note">This is exactly what <span class="mono">dbsearch compose up</span> consumes.</span>
    </div>
  </aside>
`;

/* ---------------- #269 demo switch: hide the ⚠ disclosure line ----------------
   Set true to restore. Flipped off for demos at Malco's request.

   KNOW WHAT THIS HIDES. The line carries two DIFFERENT things under one glyph (#264):
     1. a routine routing note  — "holds no data of this kind — not used: reviews, ..."
        Cosmetic. Nothing is lost by hiding it.
     2. a completeness caveat   — "Truncated — showing 5 of 142 rows"
        NOT cosmetic. With it hidden, a five-row answer to "revenue for each product SKU"
        reads as the whole picture. If someone in the room asks "is that all of them?",
        the screen no longer says otherwise.

   The answer PROSE usually still says "a partial sample of 5 products out of 142", because
   that comes from the model, not this line — so the information is generally still on
   screen, just less prominent. Generally, not always: it is not guaranteed the way this
   line was. */
const SHOW_DISCLOSURE = true;

// #715: how close a store must score to the one that was chosen before the answer discloses
// that it also matched. A DISPLAY threshold and nothing else — the selector is untouched, and
// its own thresholds are load-bearing (selftest_router_selector encodes them) and belong to
// #715/#718's design pass. The observed cluster that motivated it spanned 0.044
// (mysql-orders .584 > storefront .549 > support-tickets .548 > aw-sales .540), so 0.05 keeps
// a genuine near-tie visible without turning thirteen connected stores into a wall of names.
const NEAR_TIE = 0.05;

/**
 * Paint Connectors into `root` and return the function that takes it back down.
 *
 * The teardown is not optional politeness. This surface hangs an Escape handler, a resize
 * handler and a capture-phase pointerdown handler off window/document, and it polls an
 * ingest with setInterval. On a document that only ever showed the canvas those lived and
 * died with the page; on the shell, Ask is the very next thing on screen, and an Escape
 * handler still reaching for #spPicker would throw on every keypress there.
 */
export function mountCanvas(root) {
  // The shell's .surface is a padded 760px reading column. See css/canvas.css.
  root.classList.add("surface--bleed");
  const host = document.createElement("div");
  host.className = "canvas-surface";
  host.innerHTML = MARKUP;
  root.append(host);

  // Everything this surface owns outside its own subtree, so unmount can undo it exactly.
  const offs = [];
  const on = (target, type, fn, opts) => {
    target.addEventListener(type, fn, opts);
    offs.push(() => target.removeEventListener(type, fn, opts));
  };
  // Long-running work that must not outlive the surface. The SharePoint ingest poller is
  // the only one today, and it already clears itself on success or failure - but not on
  // "the user moved to Ask mid-crawl", which before this was simply impossible.
  const timers = new Set();

  // #643: IS THIS SURFACE STILL ON SCREEN?
  //
  // Removing listeners and clearing intervals is not a complete teardown, because a fetch
  // already in flight cannot be called back. Observed on prod: leaving Connectors while the
  // SharePoint sync was in flight threw `positionHub -> null.style` and
  // `renderPanel -> null.innerHTML` from a continuation that resolved after the DOM was gone.
  //
  // Two costs, and the quiet one is worse. The console noise is only noise - the writes land
  // nowhere. But the canvas resolves its elements with document.getElementById, so a user who
  // left and came straight back would have a stale callback writing the OLD catalog into the
  // NEW mount, silently. The render funnel below checks this instead.
  let alive = true;
  const track = (id) => { timers.add(id); return id; };

  // getElementById is kept exactly as canvas.html had it, deliberately. The whole shell was
  // checked for id collisions before this merge and there are none across all 58 of the
  // canvas's ids, so a document-wide lookup and a surface-scoped one resolve identically
  // while mounted - and rewriting 200 call sites to prove a property that already holds
  // would bury the four real changes in this file under a mechanical diff.


  const KINDS = {
    local:     {label:"Local index", mono:"IDX", cap:"semantic",   fields:[{k:"description",ph:"what lives in this store (routing signal)",secret:false}]},
    graph_search:{label:"Graph Search",mono:"GS",cap:"semantic",   fields:[{k:"description",ph:"what lives here (routing signal)",secret:false}]},
    // #299: SharePoint has NO editable connection field. It connects via the OAuth
    // "Connect with Microsoft" button, then you pick a whole library OR paste a folder sharing
    // link in the picker (#300) — nothing is typed into the node. The old `site_url` field was
    // dead (the connector never read it) and read as "paste your site here", which it wasn't.
    // #920: `needs` is the gate, and it belongs to the KIND, not to the brand row the kind
    // happens to sit under. SharePoint sits in "Files & Links" now, but its ingest still runs
    // on the caller's own Microsoft consent, so the requirement travels with it. Google Drive
    // deliberately has NO `needs`: slice 1 (#712) reads an "anyone with the link" folder with
    // the deployment's own API key, so demanding a Google account there would be the gate
    // lying about its own requirement - the concrete defect this card was opened for.
    sharepoint:{label:"SharePoint",  mono:"SP",  cap:"semantic",   needs:"entra", fields:[]},
    bigquery:  {label:"BigQuery",    mono:"BQ",  cap:"analytical", fields:[{k:"description",ph:"what this dataset holds (routing signal) — e.g. closed deals: region, amount",secret:false},{k:"project",ph:"${GCP_PROJECT}"},{k:"dataset",ph:"${GCP_DATASET}"},{k:"tables",ph:"optional: scope to these tables — e.g. SalesLT.SalesOrderHeader, SalesLT.Product",secret:false},{k:"require_signin",ph:"yes → queries run as the signed-in user (Google)",secret:false}]},
    // #666 (ADR 0024): these fields used to be cluster/key, which RedshiftEngine.from_config
    // never read - it requires workgroup+database - so a node configured from this panel could
    // never compose (the #654 hollow-offer shape, caught while wiring aws_keys). Human
    // placeholders rather than dollar-brace env refs: no AWS env names exist on a hosted box,
    // and an unresolvable env-shaped placeholder reads as "the system knows this" (#664). No
    // secret field at all - the credential is the caller's own vaulted AWS keys (account
    // menu), or the server's ambient identity on a self-host box. No require_signin switch
    // either (#809): redshift is _ALWAYS_DELEGATED like s3 - identity is not a choice here,
    // and a switch that does nothing is the hollow-offer shape again.
    redshift:  {label:"Redshift",    mono:"RS",  cap:"analytical", fields:[{k:"description",ph:"what this warehouse holds (routing signal) — e.g. sales facts by region, quarter",secret:false},{k:"workgroup",ph:"your Redshift Serverless workgroup — e.g. default-workgroup",secret:false},{k:"database",ph:"dev",secret:false},{k:"region",ph:"us-east-1",secret:false},{k:"tables",ph:"optional: scope to these tables — e.g. public.orders, public.customers",secret:false}]},
    azure_sql: {label:"Azure SQL",   mono:"AZ",  cap:"analytical", fields:[{k:"description",ph:"what this DB holds (routing signal) — e.g. closed deals: region, amount",secret:false},{k:"server",ph:"${AZURE_SQL_SERVER}"},{k:"database",ph:"${AZURE_SQL_DATABASE}"},{k:"user",ph:"${AZURE_SQL_USER}"},{k:"password",ph:"${AZURE_SQL_PASSWORD}",secret:true},{k:"tables",ph:"optional: scope to these tables — e.g. SalesLT.SalesOrderHeader, SalesLT.Product",secret:false},{k:"require_signin",ph:"yes → queries run as the signed-in user (Entra)",secret:false}]},
    // #672: RDS/Aurora on the SAME engines as postgres/mysql, under names that tell the
    // truth. Deliberately NOT the Azure panels re-listed: those seed ${AZURE_PG_HOST} and
    // friends, and showing an Azure env-var name to someone connecting an RDS box is #664
    // exactly - a placeholder that implies the system knows something it does not. These
    // carry human placeholders shaped like a real RDS endpoint instead.
    // #814 (ADR 0026): NO password field and no require_signin switch - these kinds are
    // _ALWAYS_DELEGATED: the server mints rds:generate-db-auth-token from the caller's
    // vaulted AWS keys and uses it AS the password for the db user named here. The user
    // field stays because the token is minted FOR that user (granted rds_iam /
    // AWSAuthenticationPlugin). A typed password remains legal only in a hand-written
    // self-host manifest (ADR 0010 form 2).
    rds_postgres:{label:"RDS Postgres", mono:"RDS", cap:"analytical", fields:[{k:"description",ph:"what this DB holds (routing signal) — e.g. orders: sku, qty, revenue",secret:false},{k:"host",ph:"your-db.abc123.ap-southeast-1.rds.amazonaws.com",secret:false},{k:"database",ph:"postgres",secret:false},{k:"user",ph:"db user granted rds_iam — connects with your AWS keys",secret:false},{k:"port",ph:"5432",secret:false},{k:"tables",ph:"optional: scope to these tables — e.g. public.orders, public.customers",secret:false}]},
    rds_mysql: {label:"RDS MySQL",    mono:"RDS", cap:"analytical", fields:[{k:"description",ph:"what this DB holds (routing signal) — e.g. orders: sku, qty, revenue",secret:false},{k:"host",ph:"your-db.abc123.ap-southeast-1.rds.amazonaws.com",secret:false},{k:"database",ph:"mysql",secret:false},{k:"user",ph:"db user with AWSAuthenticationPlugin — connects with your AWS keys",secret:false},{k:"port",ph:"3306",secret:false},{k:"tables",ph:"optional: scope to these tables — e.g. orders, customers",secret:false}]},
    // #673: S3 is a DOCUMENT source (semantic), not a database - it joins the folder /
    // sharepoint rail. No credential field: the crawl runs on the caller's OWN vaulted AWS
    // keys (ADR 0024), the same ones the Redshift rail uses, so there is nothing to type.
    // `note` is rendered as standing panel copy, NOT a placeholder, because it states what
    // will be TRUE of every document ingested here - S3 has no per-object ACL we can read,
    // so slice 1 ACLs everything to the linking user alone. Leaving that unsaid would look
    // like a broken search to anyone who shared the store with a colleague.
    s3:        {label:"S3",          mono:"S3",  cap:"documents", note:"Documents from this bucket will be visible only to you. S3 has no per-file permissions DBSearch can read, so nothing here is shared automatically.", fields:[{k:"description",ph:"what lives in this bucket (routing signal) — e.g. quarterly reports: revenue, headcount",secret:false},{k:"bucket",ph:"your-bucket-name",secret:false},{k:"prefix",ph:"optional: only this folder — e.g. reports/2026/",secret:false},{k:"region",ph:"ap-southeast-1",secret:false}]},
    // #712: a PUBLIC Google Drive folder ("Anyone with the link") - a DOCUMENT source, no
    // sign-in required, so it needs no delegation block (_GCP_KINDS is for the BigQuery rail's
    // OAuth seam and must stay untouched here). `note` states the honest limit out loud: the
    // folder's content is already public, so DBSearch cannot make it any more private than it
    // already is - leaving that unsaid would look like a broken/oversharing product later.
    gdrive:    {label:"Google Drive", mono:"GD",  cap:"documents", note:"Anything in this folder is already public on the internet. Documents will be visible to whoever you give this store to.", fields:[{k:"description",ph:"what lives in this folder (routing signal) — e.g. research papers: ML, quant finance",secret:false},{k:"link",ph:"https://drive.google.com/drive/folders/…  (shared as 'Anyone with the link')",secret:false}]},
    // #924: a SharePoint / OneDrive folder shared as "Anyone with the link". Deliberately NO
    // `needs`: the link mints its own anonymous badge (connectors/sharepoint_link.py), so no
    // Microsoft account is involved and demanding one would be the gate lying about its own
    // requirement (#920's defect). The consent-based `sharepoint` kind above keeps its gate -
    // it is the door for tenants whose IT disables anonymous sharing.
    sharepoint_link:{label:"SharePoint link", mono:"SPL", cap:"documents", note:"Anyone holding this link can already read the folder. Documents will be visible to whoever you give this store to.", fields:[{k:"description",ph:"what lives in this folder (routing signal) — e.g. HR policies: leave, onboarding",secret:false},{k:"link",ph:"https://<tenant>.sharepoint.com/:f:/…  (shared as 'Anyone with the link')",secret:false}]},
    postgres:  {label:"Postgres",    mono:"PG",  cap:"analytical", fields:[{k:"description",ph:"what this DB holds (routing signal) — e.g. support tickets: status, priority",secret:false},{k:"host",ph:"${AZURE_PG_HOST}"},{k:"database",ph:"${AZURE_PG_DATABASE}"},{k:"user",ph:"${AZURE_PG_USER}"},{k:"password",ph:"${AZURE_PG_PASSWORD}",secret:true},{k:"tables",ph:"optional: scope to these tables — e.g. SalesLT.SalesOrderHeader, SalesLT.Product",secret:false},{k:"require_signin",ph:"yes → queries run as the signed-in user (Entra)",secret:false}]},
    mysql:     {label:"MySQL",       mono:"MY",  cap:"analytical", fields:[{k:"description",ph:"what this DB holds (routing signal) — e.g. orders: sku, qty, revenue",secret:false},{k:"host",ph:"${AZURE_MYSQL_HOST}"},{k:"database",ph:"${AZURE_MYSQL_DATABASE}"},{k:"user",ph:"${AZURE_MYSQL_USER}"},{k:"password",ph:"${AZURE_MYSQL_PASSWORD}",secret:true},{k:"tables",ph:"optional: scope to these tables — e.g. SalesLT.SalesOrderHeader, SalesLT.Product",secret:false},{k:"require_signin",ph:"yes → queries run as the signed-in user (Entra)",secret:false}]},
    synapse:   {label:"Synapse",     mono:"SY",  cap:"analytical", fields:[{k:"description",ph:"what this warehouse holds (routing signal) — e.g. sales facts by region, quarter",secret:false},{k:"server",ph:"${SYNAPSE_SERVER}"},{k:"database",ph:"${SYNAPSE_POOL}"},{k:"user",ph:"${SYNAPSE_USER}"},{k:"password",ph:"${SYNAPSE_PASSWORD}",secret:true},{k:"tables",ph:"optional: scope to these tables — e.g. SalesLT.SalesOrderHeader, SalesLT.Product",secret:false},{k:"require_signin",ph:"yes → queries run as the signed-in user (Entra)",secret:false}]},
    cosmos_db: {label:"Cosmos DB",   mono:"CD",  cap:"analytical", fields:[{k:"description",ph:"what these docs hold (routing signal) — e.g. support tickets: status, priority, region",secret:false},{k:"endpoint",ph:"${COSMOS_ENDPOINT}"},{k:"database",ph:"${COSMOS_DATABASE}"},{k:"container",ph:"${COSMOS_CONTAINER}"},{k:"key",ph:"${COSMOS_KEY}",secret:true},{k:"require_signin",ph:"yes → queries run as the signed-in user (Entra data-plane RBAC)",secret:false}]},
    csv:       {label:"CSV / Files", mono:"CSV", cap:"analytical", fields:[{k:"path",ph:"./data/*.csv",secret:false}]},
    // #551: the folder connector is the only DOCUMENT connector a self-hoster can use today
    // (SharePoint needs a licensed tenant), and it had no card here at all - so the one way to
    // reach it was the setup agent or a hand-written manifest. It reads a directory, ACL-aware
    // and incrementally. Operator-only in the palette because compose refuses local file
    // sources for anyone else (router_api._reads_local_files) - a server-side path read from an
    // untrusted caller is a file-read primitive, and that gate must NOT be loosened to suit
    // this card. Showing a tile that always 403s would be worse than showing none.
    folder:    {label:"Folder", mono:"DIR", cap:"semantic", operatorOnly:true, fields:[
                 {k:"description",ph:"what lives in this folder (routing signal) — e.g. HR policies: leave, expenses, benefits",secret:false},
                 {k:"path",ph:"/data/hr-documents  (a directory on the server)",secret:false}]},
    // #561: the file door for everyone else. `folder` is correctly operator-only (it reads a
    // path on THIS server), which left a hosted user with a Connectors surface that could not
    // accept a document at all - upload existed, but only from the Admin console, which is not
    // where anyone looks to add a source.
    //
    // ACTION, not a store: `action:true` keeps it out of addNode's node-building path. A node
    // would enter liveManifest() and be composed, and there is no provider behind an upload -
    // it would compose as an empty store: green, and answering nothing (#200). It does not need
    // one. Since #255 the document bridge is asked on EVERY /router/ask, so a document is
    // answerable the moment it is indexed, with nothing to wire.
    upload:    {label:"Upload files", mono:"UP", cap:"documents", action:true, fields:[]}
  };
  // A kind this build's palette has no card for. Real ones exist TODAY: `folder` (the main
  // output of the conversational setup agent) and `databricks` are registered providers and
  // both are in PLANNED_KINDS server-side, but neither has a KINDS row here.
  //
  // The canvas used to paper over that with `KINDS[s.kind]?s.kind:"local"` on every load
  // path, which REWROTE the kind so the node could render. Before #368 that only corrupted
  // localStorage; now the canvas restores from the server's stored manifest and composeUp()
  // writes what it rebuilt straight back into it, so the downgrade destroys the real kind in
  // the system of record - and composes "successfully" as an empty local index: a green node
  // that answers nothing, exactly the affirmative-looking failure #200 exists to prevent.
  //
  // So: never rewrite the kind. Render an honest placeholder card instead and SAY the canvas
  // cannot edit this one (`unknown:true` drives that copy in renderPanel).
  function kindDef(kind){
    return KINDS[kind] || {label:String(kind||"(no kind)"), mono:"?", cap:"unknown to this build",
                           fields:[], unknown:true};
  }
  // `--k-<kind>` is only defined for palette kinds, so an unrenderable kind needs a fallback
  // or the node paints with an invalid custom property (no accent colour at all). And now
  // that an arbitrary kind survives to the renderer, it is untrusted text flowing into a CSS
  // custom-property NAME (renderPanel builds a style="" attribute as a string): allow only
  // the identifier shape the palette itself uses, and fall back to a neutral colour.
  function kindColor(kind){
    return /^[a-z0-9_]+$/.test(String(kind||"")) ? "var(--k-"+kind+", var(--faint))"
                                                 : "var(--faint)";
  }
  // Providers group the flat KINDS into brands (Azure has 5, Google/AWS will grow).
  // The rail shows one row per provider; hovering pops a flyout of its services.
  // #823: `link` is the VAULT's idp name for the credential this provider's kinds need, and
  // it is what /auth/me's `linked` reports. `flag` is whether the deployment can link it at
  // all, `who` is what a person calls it, `connect` is where the linking flow lives (null =
  // no URL to send anyone to; AWS vaults keys through a form in the account panel, ADR 0024).
  // The same four facts live in ROSTER in ../ui/account.js, which owns the account panel.
  // They are deliberately NOT imported: this surface has no cross-surface imports. They
  // cannot drift silently either, because selftest_823 asserts the two agree.
  // `files` has no link because upload/csv/local need only an account, never a third party.
  const PROVIDERS = [
    {key:"azure",  label:"Azure",         mono:"AZ",  color:"var(--k-azure_sql)",  kinds:["azure_sql","postgres","mysql","synapse","cosmos_db"],
     link:"entra",  who:"Microsoft", flag:"enabled",        connect:"/auth/entra/link"},
    {key:"google", label:"Google Cloud",  mono:"GC",  color:"var(--k-bigquery)",   kinds:["bigquery"],
     link:"google", who:"Google",    flag:"google_enabled", connect:"/auth/google/login"},
    // #672: RDS leads. A customer's "database on AWS" is overwhelmingly RDS/Aurora Postgres
    // or MySQL, not a warehouse - and this group used to offer Redshift alone, so the common
    // case looked unsupported while the engine had handled it since #155.
    {key:"aws",    label:"AWS",           mono:"AWS", color:"var(--k-redshift)",   kinds:["rds_postgres","rds_mysql","redshift","s3"],
     link:"aws",    who:"Amazon",    flag:"aws_enabled",    connect:null},
    // #920 (owner ruling, 260822): the DOCUMENT kinds left their cloud-brand rows. SharePoint
    // was the only kind under "Microsoft 365", so that row is gone rather than left
    // advertising nothing, and gdrive left "Google Cloud", which now carries BigQuery alone.
    // The brand rows that remain are exactly the ones whose query really does run as you.
    //
    // #561: upload leads. It is the only card in this group an ordinary hosted user can act on.
    {key:"files",  label:"Files & Links", mono:"FS",  color:"var(--k-csv)",
     // #918 (owner ruling, 260822): "csv can be absorbed by the file node - users can just
     // upload .csv". CSV LEFT THE RAIL. It was a dead end that any signed-in stranger could
     // reach: the panel collected `path` and the store only ever reads `tables` or `files`
     // (router/structured.py _make), so a CSV node configured through the UI could never
     // compose - the #654 hollow-offer shape. Nothing that ever worked is lost, and the
     // upload path already accepts .csv and .xlsx (app.py _EXT_MIME).
     // The KINDS entry deliberately STAYS, exactly as #816 left graph_search: an existing
     // csv node in someone's manifest must still render as itself, and a hand-written
     // manifest (ADR 0010 form 2) can still declare one with the keys the store reads.
     kinds:["upload","gdrive","sharepoint_link","sharepoint","local","folder"],
     link:null,     who:"",          flag:"",               connect:null},
  ];
  const BU_COLORS = {}; // assigned per business unit
  const BU_PALETTE = ["#6d7cff","#3fc9b0","#ff9d5c","#ef6a99","#8fd15a","#a08bff","#4d9fff"];
  function buColor(bu){
    if(!bu) return "var(--faint)";
    if(!(bu in BU_COLORS)) BU_COLORS[bu]=BU_PALETTE[Object.keys(BU_COLORS).length%BU_PALETTE.length];
    return BU_COLORS[bu];
  }

  const HUB = {x:1195, y:770};
  let state, selected=null, seq=0;

  function demo(){
    BU_COLORS.hr=BU_PALETTE[1]; BU_COLORS.sales=BU_PALETTE[0]; BU_COLORS.finance=BU_PALETTE[2];
    return [
      mk("hr-wiki","sharepoint","hr",["hr-staff"],{site_url:""},"connected",900,470),   // #262: not ${HR_SP_URL} — never set
      mk("sales-ledger","bigquery","sales",["sales-staff"],{project:"${GBQ_PROJECT}",dataset:"sales",key:"${GBQ_KEY}"},"connected",1470,470),
      mk("fin-warehouse","redshift","finance",["fin-staff"],{workgroup:"${RS_WORKGROUP}",database:"analytics"},"draft",1470,1030),   // #666: fields the engine actually reads
      mk("hr-tickets","postgres","hr",["hr-staff","it-staff"],{description:"support tickets: team, priority, status, hours",host:"${AZURE_PG_HOST}",database:"${AZURE_PG_DATABASE}",user:"${AZURE_PG_USER}",password:"${AZURE_PG_PASSWORD}"},"connected",900,1030)
    ];
  }
  function mk(id,kind,bu,acl,config,status,x,y){
    seq++; return {uid:"n"+seq,id,kind,bu,acl:acl.slice(),config:Object.assign({},config),status,x,y};
  }

  /* ---------------- palette (provider rows + hover flyout) ---------------- */
  // #551: ONE definition of which services a caller may see, used by the row COUNT and the
  // flyout alike - if they disagree, a row advertises "3 services" and lists two.
  function visibleKinds(p){
    return p.kinds.filter(k=>!(KINDS[k]||{}).operatorOnly || CFG_OPERATOR);
  }
  /**
   * #823: why this provider cannot be added right now, or null if it can.
   *
   * The owner's rule: you cannot add data without an account, because the data has to be
   * filed under an identity, and you cannot add a CLOUD source without that cloud linked,
   * because the query runs as you (ADR 0022/0024). The UX ruling is the opposite of
   * greyed-out, which the owner rejected outright: the row keeps its colour and the
   * affordance appears when you reach for it, so the answer is a returned CTA rather than
   * a disabled attribute.
   *
   * The first test is `realLoginConfigured()`, NOT `signed_in`. A dev rig has signed_in
   * permanently false and carries identity in X-DBSearch-User, so gating on signed_in would
   * lock every local rig and every selftest out of the palette. Same reasoning, and the same
   * trap, as the comment on openUploadPicker.
   */
  /**
   * #920: the four link facts for one idp, read back off the row that carries them.
   *
   * Derived rather than duplicated: a kind gate and a row gate that kept separate copies of
   * "what Microsoft is called and where you link it" is exactly the drift selftest_823 exists
   * to catch, and a second table here would put that drift INSIDE this file where 823's
   * rail-vs-panel comparison cannot see it. Each idp appears on exactly one row.
   */
  function idpFacts(link){
    const p=PROVIDERS.find(p=>p.link===link);
    return p ? {who:p.who, flag:p.flag, connect:p.connect} : null;
  }
  /**
   * #920: what THIS kind needs vaulted, or null if an account is enough.
   *
   * A kind may declare `needs` itself; otherwise it inherits the requirement of the row it
   * sits under, which keeps every database kind on exactly today's rule. hasOwnProperty, not
   * a truthiness test, so a kind can say "needs: null" out loud and override a branded row.
   */
  function kindNeeds(kind){
    const d=KINDS[kind]||{};
    if(Object.prototype.hasOwnProperty.call(d,"needs")) return d.needs;
    const p=PROVIDERS.find(p=>p.kinds.indexOf(kind)>=0);
    return p ? p.link : null;
  }
  /**
   * #823, re-homed by #920: why this KIND cannot be added right now, or null if it can.
   *
   * The owner's rule: you cannot add data without an account, because the data has to be
   * filed under an identity, and you cannot add a source whose data path runs on a cloud
   * credential without that cloud linked, because the query runs as you (ADR 0022/0024).
   * The UX ruling is the opposite of greyed-out, which the owner rejected outright: the row
   * keeps its colour and the affordance appears when you reach for it, so the answer is a
   * returned CTA rather than a disabled attribute.
   *
   * #920 moved the second test from the PROVIDER to the KIND. It used to read "which brand
   * row is this under", which is not the same question and answered wrongly for the two
   * document kinds: a public Drive folder needs no Google account, and a SharePoint share
   * needs Microsoft wherever the tile is filed.
   *
   * The first test is `realLoginConfigured()`, NOT `signed_in`. A dev rig has signed_in
   * permanently false and carries identity in X-DBSearch-User, so gating on signed_in would
   * lock every local rig and every selftest out of the palette. Same reasoning, and the same
   * trap, as the comment on openUploadPicker.
   */
  function kindGate(kind){
    if(!realLoginConfigured()) return null;         // dev rig: unchanged, deliberately
    if(!isLiveUser()) return {
      msg:"Adding a source needs an account, so your data is filed under you and kept apart "
          +"from everyone else's.",
      cta:"Sign in", href:"/signin"};
    const need=kindNeeds(kind);
    if(!need) return null;                          // Files & Links: an account is enough
    const f=idpFacts(need);
    if(!f) return null;
    if(!authState[f.flag]) return {                 // honest about a provider this box lacks
      msg:esc(f.who)+" sign-in is not configured on this deployment, so there is no account "
          +"to connect here.",
      cta:null, href:null};
    if((authState.linked||[]).indexOf(need)>=0) return null;
    return {
      msg:"DBSearch queries these as you, using your own "+esc(f.who)+" account, so it can "
          +"never show you more than "+esc(f.who)+" would.",
      cta:"Connect your "+esc(f.who)+" account", href:f.connect};
  }
  /**
   * The ROW's gate: the one reason that covers EVERY service it offers.
   *
   * A row takes the whole-flyout treatment only when nothing behind it is addable - which is
   * still every database row, whose kinds share one credential. A row with a mix (Files &
   * Links, where upload is always addable and SharePoint may not be) opens normally and each
   * gated tile carries its own affordance, so one blocked service can never hide five that
   * work. Returns the FIRST gate, which is the shared one in the all-gated case.
   */
  function providerGate(p){
    const kinds=visibleKinds(p);
    if(!kinds.length) return null;
    const gates=kinds.map(kindGate);
    return gates.every(Boolean) ? gates[0] : null;
  }
  function buildRail(){
    const rail=document.getElementById("rail");
    const note=rail.querySelector(".rail-note");
    // Idempotent: /config lands AFTER the first paint, so this re-runs once operator status
    // is known. Without the clear, the second run would duplicate every row.
    rail.querySelectorAll(".prov").forEach(r=>r.remove());
    PROVIDERS.forEach(p=>{
      const kinds=visibleKinds(p);
      if(!kinds.length) return;
      const row=document.createElement("div");
      row.className="prov"; row.style.setProperty("--k",p.color);
      const n=kinds.length;
      row.innerHTML='<span class="mono-chip">'+p.mono+'</span>'+
        '<span class="pn"><b>'+p.label+'</b><span>'+n+' service'+(n>1?'s':'')+'</span></span>'+
        '<span class="car">▸</span>';
      row.addEventListener("mouseenter",()=>openProvMenu(p,row));
      row.addEventListener("mouseleave",scheduleProvClose);
      row.addEventListener("click",()=>openProvMenu(p,row));   // click/tap also opens
      rail.insertBefore(row,note);
    });
  }

  /* provider flyout menu */
  const provmenu=document.getElementById("provmenu");
  let provCloseT=null, provActiveRow=null;
  function cancelProvClose(){ if(provCloseT){clearTimeout(provCloseT);provCloseT=null;} }
  function scheduleProvClose(){ cancelProvClose(); provCloseT=setTimeout(closeProvMenu,180); }
  function closeProvMenu(){ provmenu.classList.remove("show");
    if(provActiveRow) provActiveRow.classList.remove("active"); provActiveRow=null; }
  function openProvMenu(p,row){
    cancelProvClose();
    if(provActiveRow&&provActiveRow!==row) provActiveRow.classList.remove("active");
    provActiveRow=row; row.classList.add("active");
    provmenu.style.setProperty("--k",p.color);
    // #949 (owner ruling, 260824, supersedes #823): a gated brand row REVEALS its services
    // and puts the ONE connect action in a banner above them, instead of hiding the services
    // behind a single CTA. #823 hid them so nobody clicked a tile that would 403; the owner's
    // point is the opposite - a user cannot decide to connect Microsoft/Google/AWS if the
    // canvas never shows them what those unlock. So the services are always visible; the
    // banner (only on a fully-gated row) carries the connect, and clicking any gated tile
    // still routes to that same connect rather than 403-ing. `providerGate` returns the shared
    // gate when EVERY kind is gated (an all-Azure row shares one Microsoft account); a mixed
    // row (Files & Links) returns null and each gated tile keeps its own per-tile affordance.
    const gate=providerGate(p);
    const kinds=visibleKinds(p);
    const banner = gate
      ? '<div class="gate gate-banner"><div class="gate-msg">Connecting enables '
          +(kinds.length>1?('all '+kinds.length+' services'):'this service')
          +' below. '+gate.msg+'</div>'
        +(gate.cta
          ? (gate.href
              ? '<a class="gate-cta" href="'+esc(gate.href)+'">'+gate.cta+'</a>'
              // ADR 0024: AWS has no linking URL. Connect reveals a key form in the account
              // panel, so this hands the user to it rather than inventing a second copy.
              : '<button type="button" class="gate-cta">'+gate.cta+'</button>')
          : '')
        +'</div>'
      : '';
    // The tile sub-label: normally the capability ("documents", "analytical"). On a MIXED
    // row a gated tile shows the one thing that would unlock IT (#920), because there is no
    // shared banner to carry it. Under a brand banner the tiles show the capability instead -
    // the banner already says how to connect, and repeating it on every tile is noise that
    // buries the thing the owner wants seen: WHAT each service is.
    provmenu.innerHTML='<div class="mh">'+esc(p.label)+'</div>'+banner+kinds.map(k=>{
      const d=KINDS[k], g=kindGate(k);
      const sub = !g ? d.cap : (gate ? d.cap : esc(g.cta||g.msg));
      return '<div class="svc'+(g?' gated':'')+'" data-kind="'+k+'">'+
        '<span class="sc">'+d.mono+'</span>'+
        '<span class="snn"><b>'+esc(d.label)+'</b><span>'+sub+'</span></span>'+
        '<span class="plus">'+(g?'↗':'+')+'</span></div>';
    }).join("");
    provmenu.classList.add("show");
    const bcta=provmenu.querySelector(".gate-banner button.gate-cta");
    if(bcta) bcta.onclick=()=>{
      closeProvMenu();
      const acct=document.querySelector("#account button");
      if(acct) acct.click(); else toast("Open the account menu to connect "+p.who+".");
    };
    placeProvMenu(row);
    provmenu.querySelectorAll(".svc").forEach(el=>{
      el.onclick=()=>{
        const kind=el.dataset.kind, g=kindGate(kind);
        closeProvMenu();
        if(!g){ addNode(kind); return; }
        // Same three endings as the row-level gate: a link to follow, the account panel for
        // a provider that vaults keys through a form (ADR 0024), or an honest dead end.
        if(g.href){ location.href=g.href; return; }
        if(g.cta){
          const acct=document.querySelector("#account button");
          if(acct) acct.click(); else toast(g.cta+" to add this.");
          return;
        }
        toast(g.msg);
      };
    });
  }
  // Extracted so the gated branch above places itself the same way (#823): it returns early,
  // and a second copy of this arithmetic would be the thing that drifts.
  function placeProvMenu(row){
    const r=row.getBoundingClientRect(), mw=provmenu.offsetWidth, mh=provmenu.offsetHeight;
    let left=r.right+8, top=r.top;
    if(left+mw>window.innerWidth-8) left=r.left-mw-8;
    if(top+mh>window.innerHeight-8) top=window.innerHeight-mh-8;
    provmenu.style.left=Math.max(8,left)+"px"; provmenu.style.top=Math.max(8,top)+"px";
  }
  provmenu.addEventListener("mouseenter",cancelProvClose);
  provmenu.addEventListener("mouseleave",scheduleProvClose);

  function addNode(kind){
    // #561: checked BEFORE the demo branch, because a demo visitor asking to upload must be
    // told why they cannot (no session, /admin/upload answers 401) rather than silently
    // getting a fixture node - openUploadPicker carries that refusal.
    if(kind==="upload"){
      if(isDemoMode()) return openUploadPicker();
      // #923 (owner, 260821): adding "Upload files" adds the NODE - like any other source -
      // persistent at 0 docs and auto-selected so the panel is already open; the panel's
      // the panel's Upload files is a door to the modal (#950 gave the node's button the
      // same one). The modal is never the FIRST hop - adding still lands on the node.
      upNodeGone=false;
      const n=ensureUploadNode();
      selected=n.uid; renderAll(); saveCanvas();
      syncDocumentsNode();          // count + list from server truth (repaints the panel)
      return;
    }
    // #823: the flyout is the only way in today, and it already refuses a gated provider.
    // This is the second lock, for every other way a kind could reach here (a restored
    // draft, a keyboard path, a later caller): the rule belongs with the action, not only
    // with the menu that usually starts it.
    const gate=kindGate(kind);      // #920: the kind's own requirement, not its row's
    if(gate){ toast(gate.cta ? gate.cta+" to add this." : gate.msg); return; }
    if(isDemoMode()) return demoAddKind(kind);   // #279 (B): identical palette, local-fixture backend
    const def=KINDS[kind];
    const n=Object.keys(KINDS).length;
    const base=(kind[0]+"-source");
    // #953: FIRST FREE SUFFIX, never a count. Counting is only collision-free while nothing
    // is ever deleted: with gdrive-1 and gdrive-2 live, deleting gdrive-1 drops the count to 1
    // and the next add is named gdrive-2 - a DUPLICATE of a live node, welding two nodes to
    // one server store so that deleting either purges (#947) the data the other still shows.
    // A freed id may be reused - the #947 delete purged that store, so the name is genuinely
    // free - but a live one may never be taken.
    let idn=1; while(state.some(s=>s.id===kind+"-"+idn)) idn++;
    let id=kind+"-"+idn;
    // A placeholder is a HINT, not a value (#204). Seeding config with f.ph made every new
    // node ship with its own help text as real config — harmless-looking only because the
    // connection fields' placeholders happen to be valid ${ENV} refs, so they resolved BY
    // LUCK. A prose placeholder does not: `tables` became the allowlist
    // ["optional: scope to these tables — e.g. SalesLT.SalesOrderHeader", "SalesLT.Product"],
    // which matches no table, so the schema came back empty and a healthy DB reported
    // "no tables are visible to your grants".
    // #294: require_signin (OBO — query as the signed-in user) defaults to "no". It USED to
    // default "yes", but a self-serve user's sign-in token does not carry the target DB's
    // delegation scope (e.g. https://database.windows.net/user_impersonation), so OBO cannot
    // establish and the store silently drops → invisible → "no accessible store for this user".
    // The self-serve model (ADR 0009: a user connects THEIR OWN database) is service-cred: the
    // connection's own ${ENV} credentials, queried by the connection. OBO stays one toggle away
    // for an enterprise deployment that has consented the per-resource delegation scope.
    // #317 / ADR 0010: seeding ${ENV} refs is an OPERATOR affordance, not a user default.
    // It assumes whoever runs the server also sets AZURE_SQL_SERVER and friends - true on a
    // dev rig or self-host box, false for a self-serve user, who cannot set environment
    // variables on our server and should never be asked to. Seeding them anyway is what made
    // a signed-in user's first Test connection fail with "manifest references unset env var
    // 'AZURE_SQL_SERVER'" on the hosted deployment: the node arrived pre-wired to a variable
    // that exists only on someone else's machine.
    //
    // #320: the honest test is whether THIS SERVER can resolve the variable, which /config
    // reports as env_present (names only, never values). An operator rig has AZURE_SQL_* and
    // prefills exactly as before; a hosted box does not and leaves the field blank.
    //
    // #317 first gated on `!realLoginConfigured()`. That was wrong and broke the local demo:
    // it conflated "has a real login" with "is a hosted multi-user deployment", and the local
    // rig is BOTH - real Entra login AND operator-provisioned connection vars - so a
    // signed-in local user suddenly got empty fields and could not connect Azure SQL.
    const config={};
    def.fields.forEach(f=>{
      const ph=String(f.ph||"");
      const envRef=/^\$\{[A-Z0-9_]+\}$/.test(ph);
      const resolvable=envRef && CFG_OPERATOR && ENV_PRESENT.has(ph.slice(2,-1));
      // #660: "no" everywhere EXCEPT the GCP kinds, where it is the only value that can work.
      // #294's reasoning above is about ENTRA: a self-serve user's sign-in token does not carry
      // the target DB's delegation scope, so OBO cannot establish and the store silently drops.
      // Google is the opposite by construction - the bigquery scope is consented AT LINK TIME
      // (google_auth.CHANNEL_SCOPES) and the vaulted refresh token already carries it - while
      // "no" means the SERVER's ADC, which a hosted box does not have and cannot meaningfully
      // hold for a user's own GCP project (ADR 0022). So for GCP, "no" was a default that could
      // only ever fail: the owner dropped a BigQuery node, pressed Test connection, and got
      // "Cannot reach 'bigquery'" pointing at Application Default Credentials.
      const signinDefault = _GCP_KINDS.has(kind) ? "yes" : "no";
      config[f.k]= resolvable ? ph : (f.k==="require_signin" ? signinDefault : "");
    });
    const ang=(state.length*0.9), rad=250+(state.length%3)*40;
    const x=HUB.x+Math.cos(ang)*rad*1.5, y=HUB.y+Math.sin(ang)*rad;
    // #291: a signed-in user connecting THEIR OWN database shouldn't have to configure who can
    // see it - default the ACL to themselves, so "connect -> query" works out of the box (they
    // can still SHARE it via the panel). Without this the store composes with no ACL and LAW 2
    // (default-deny) leaves it invisible even to its owner, and the query answers "nothing found".
    const acl=(authState.signed_in && authState.oid) ? [authState.oid] : [];
    const node=mk(id,kind,"",acl,config,"draft",Math.max(40,x),Math.max(40,y));
    state.push(node); selected=node.uid; renderAll(); flashCenter(node.uid);
  }

  /* ---------------- render ---------------- */
  const world=document.getElementById("world");
  const edges=document.getElementById("edges");

  function renderAll(){
    if(!alive) return;                 // #643: a late callback, after unmount
    // remove existing nodes (keep svg + hub)
    world.querySelectorAll(".node").forEach(el=>el.remove());
    state.forEach(buildNode);
    positionHub();
    drawEdges(); renderPanel(); renderStatus();
    const y=document.getElementById("yaml"); if(document.getElementById("drawer").classList.contains("open")) y.innerHTML=yamlHTML();
    saveCanvas();                     // #199: every mutation renders, so persist here
  }

  function positionHub(){
    if(!alive) return;
    const h=document.getElementById("hub");
    h.style.left=(HUB.x-75)+"px"; h.style.top=(HUB.y-52)+"px";
  }

  function buildNode(node){
    const def=kindDef(node.kind);
    const el=document.createElement("div");
    el.className="node"+(selected===node.uid?" sel":"");
    el.style.setProperty("--k",kindColor(node.kind));
    el.style.left=node.x+"px"; el.style.top=node.y+"px";
    el.dataset.uid=node.uid;
    // #291: the owner's own oid renders as "You" (a GUID pill on your own store is meaningless).
    const aclLabel=a=>(authState.oid && a===authState.oid)?"You":(principalName(a)||a);
    const acls=node.acl.length?node.acl.map(a=>'<span class="pill acl">'+esc(aclLabel(a))+'</span>').join(""):
      (node.derived
        // #917: an uploads node has per-DOCUMENT audiences (#539 private / #575 org), so
        // "no ACL" would be a warning about a field the node cannot carry.
        ? '<span class="pill acl">You</span>'
        : '<span class="pill" style="color:var(--warn);border-color:color-mix(in srgb,var(--warn) 35%,transparent)">no ACL</span>');
    const bu=node.bu?'<span class="pill bu" style="--bu:'+buColor(node.bu)+'">'+esc(node.bu)+'</span>':
      '<span class="pill" style="color:var(--faint)">no unit</span>';
    el.innerHTML=
      '<div class="nhead">'+
        '<span class="mono-chip">'+def.mono+'</span>'+
        '<div style="min-width:0"><div class="nid">'+esc(node.id)+'</div>'+
        // esc(): the kind is no longer guaranteed to be one of ours (kindDef preserves an
        // unknown kind instead of rewriting it), so it is untrusted manifest text now.
        '<div class="nkind">'+esc(node.kind)+'</div></div>'+
        // #781: the tooltip used to be the status WORD - a red dot saying "planned" while
        // the compose response's reason sat unread on node.reason. Status stays first (it is
        // the one-glance answer), the reason follows. esc(): the reason is server text
        // landing in an ATTRIBUTE - `"` breaks out of an unescaped title (#786).
        // #941: the dot reports the CATALOG, not the probe. A store that answered a health
        // check but is not composed is a draft, and saying "connected" here is the sentence
        // the owner believed on prod.
        '<span class="status '+(isUncomposed(node)?"draft":node.status)+'" title="'+
          esc(isUncomposed(node) ? "draft: "+UNCOMPOSED_HINT
                                 : node.status+(node.reason?": "+node.reason:""))+'"></span>'+
      '</div>'+
      '<div class="nbody">'+bu+
        '<span class="pill cap">'+def.cap+'</span>'+acls+
        freshnessPill(node)+
      '</div>'+
      // #781: the card says WHY it is red without a hover and without opening the panel.
      // Gated on node.reason, not on status: demo-fleet nodes are legitimately "planned"
      // with nothing to explain. Clamped to two lines by .nreason; title carries the full
      // text. Both sinks escape - this is the same server string as the tooltip above.
      (node.status==="planned"&&node.reason
        ? '<div class="nreason" title="'+esc(node.reason)+'">'+esc(node.reason)+'</div>'
        : '')+
      // #941: the card says it WITHOUT a hover, for the same reason #781 put the failure
      // reason here. Amber like .nwarn, not red: nothing is broken, the store simply has not
      // been committed yet - and the text names the button, because honesty that leaves the
      // user hunting is a different failure.
      (isUncomposed(node)
        ? '<div class="nwarn" title="'+esc(UNCOMPOSED_HINT)+'">'+esc(UNCOMPOSED_HINT)+'</div>'
        : '')+
      // #808: a CONNECTED node can still be unusable, and until now the card had no way to
      // say so - `.nreason` above is gated on "planned", which is the refused-and-never-built
      // state. A store whose `tables:` allowlist matched nothing composes green, routes, and
      // then declines every question; #727 made that ANSWER honest, this makes the compose
      // that created it honest. Deliberately NOT the red `.nreason`: the store is connected
      // and working, so this is amber. esc() on both sinks - server text into an attribute
      // and a text node (#786).
      ((node.warnings&&node.warnings.length)
        ? '<div class="nwarn" title="'+esc(node.warnings.join(" "))+'">'+
            esc(node.warnings[0])+'</div>'
        : '')+
      // ARCHITECTURAL TEMPLATE (#154): a canvas node wired to a REAL connector backend.
      // SharePoint is the proven one — its node launches the live Microsoft OAuth flow
      // (/connectors/sharepoint/consent-url), the exact path verified end-to-end. Future
      // structured stores (Azure SQL, #153) follow this same node→live-endpoint pattern.
      (node.kind==='sharepoint' ? spNodeButton(node)
        : node.kind==='upload' ? upNodeButton(node) : dbGrantButton(node));
    world.appendChild(el);
    wireDrag(el,node);
    el.addEventListener("click",e=>{ if(!el._moved){ selected=node.uid; renderAll(); } });
    // #917 (owner, 260821): clicking the NODE lands on the overview panel rather than jumping
    // to the modal - the panel is where every document action lives (Upload files, Share,
    // Delete), so landing there keeps one door per surface. That rule is about the CARD, and
    // the card's own click handler above still honours it.
    // #950 (owner, 260824): the BUTTON is a different thing, and it used to do only what the
    // card does - `selected=node.uid; renderAll()`. Since #923 auto-selects the node on add,
    // the panel was already open and the click was a literal visual no-op: "i click upload
    // files, nth happens then im like huh?". A control whose label is an imperative has to
    // honour it, so this opens the picker (selecting first, so the panel behind it is the
    // right one when the modal closes). openUploadPicker carries the demo refusal itself.
    const upBtn=el.querySelector(".up-add");
    if(upBtn){ upBtn.addEventListener("click",e=>{ e.stopPropagation();
      selected=node.uid; renderAll(); openUploadPicker(); }); }
    el.addEventListener("contextmenu",e=>{ e.preventDefault(); e.stopPropagation();
      selected=node.uid; renderAll(); openCtxMenu(node,e.clientX,e.clientY); });
    const spBtn=el.querySelector(".sp-connect");
    if(spBtn){
      spBtn.addEventListener("click", async e=>{
        e.stopPropagation();
        spBtn.textContent="Connecting…"; spBtn.disabled=true;
        try{
          const cfg=await (await fetch("/config")).json();
          const user=(cfg.users&&cfg.users[0])||"";          // dev-auth identity (real Entra OID on the azure backend)
          const r=await fetch("/connectors/sharepoint/consent-url", user?{headers:{"X-DBSearch-User":user}}:{});
          // #297: 503 = this deployment never registered the multi-tenant connector app
          // (SP_CONNECTOR_*). It is an EXPECTED operator-setup state, not a user error, so say so
          // plainly instead of leaking the env-var name.
          if(r.status===503){ throw new Error("SharePoint isn't set up on this deployment yet — an operator needs to register the connector app (see docs/SHAREPOINT_CONNECTOR.md)."); }
          if(!r.ok){ throw new Error("Couldn't start the SharePoint sign-in (consent-url "+r.status+")."); }
          const {url}=await r.json();
          window.location=url;                                // → Microsoft admin-consent, then pick library → ingest → query
        // #297: a native blocking dialog froze the ENTIRE canvas on a known-failure path (an
        // unconfigured connector) — the worst possible response to an expected state. Use the
        // non-blocking toast the rest of the canvas already uses.
        }catch(err){ spBtn.disabled=false; spBtn.textContent="Connect with Microsoft (live)"; toast(String(err.message||err)); }
      });
    }
    const pickBtn=el.querySelector(".sp-pick");
    if(pickBtn){ pickBtn.addEventListener("click",e=>{ e.stopPropagation(); openSpPicker(node); }); }
    const grantBtn=el.querySelector(".db-grant");
    if(grantBtn){
      grantBtn.addEventListener("click", async e=>{
        e.stopPropagation();
        const label=grantBtn.textContent;
        grantBtn.textContent="Opening Microsoft…"; grantBtn.disabled=true;
        try{
          const cfg=await (await fetch("/config")).json();
          const user=(cfg.users&&cfg.users[0])||"";
          const r=await fetch("/auth/grant/db-url", user?{headers:{"X-DBSearch-User":user}}:{});
          // 401 = no session to upgrade; 503 = this deployment has no tenant app, so there is
          // no consented delegation to ask for. Both are expected states, not user errors.
          if(r.status===401){ throw new Error("Sign in with Microsoft first, then approve database access."); }
          if(r.status===503){ throw new Error("This deployment isn't set up for query-as-user database access yet."); }
          if(!r.ok){ throw new Error("Couldn't start the approval (grant/db-url "+r.status+")."); }
          const {url}=await r.json();
          window.location=url;
        }catch(err){ grantBtn.disabled=false; grantBtn.textContent=label; toast(String(err.message||err)); }
      });
    }
  }

  // #429: a query-as-user database needs its own consent round, because sign-in deliberately
  // no longer asks for the Azure SQL delegation (asking up front AADSTS650052'd every org that
  // has never provisioned an Azure SQL service principal — they could not even sign in). The
  // ask belongs here: the user has just chosen this database, so "approve database access" is
  // a sentence about something they did. Only for require_signin nodes — a node using a stored
  // credential delegates nothing and needs no grant.
  const DB_DELEGATED_KINDS=["azure_sql","postgres","mysql","synapse","cosmos_db"];
  function dbGrantButton(node){
    if(DB_DELEGATED_KINDS.indexOf(node.kind)<0) return '';
    const rs=String((node.config&&node.config.require_signin)||"").toLowerCase();
    if(rs!=="yes"&&rs!=="true"&&rs!=="1") return '';
    return '<div class="nbody"><button class="btn db-grant" style="flex:1;justify-content:center" '+
      'title="Approve Microsoft database access for your organization — needed once, so queries can run as you">'+
      'Approve database access</button></div>';
  }

  // SharePoint node CTA reflects its lifecycle: connect → pick library → ingested (#167/#169).
  function spNodeButton(node){
    const t=node.config && node.config.tenant;
    const ing=t && spIngested[t];
    // #301: even after ingesting, keep a way BACK to the picker (the ＋) — otherwise there is no
    // way to ingest another library or paste another folder sharing link once the first ran.
    if(ing) return '<div class="nbody"><span class="pill" style="flex:1;justify-content:center;'+
      'color:var(--ok);border-color:color-mix(in srgb,var(--ok) 35%,transparent)">✓ '+
      (ing.docs?ing.docs+' docs':'ingested')+' · ask below</span>'+
      '<button class="btn sp-pick" title="Ingest another library, or paste a folder sharing link" '+
      'style="flex:none;padding:0 11px" aria-label="ingest more">＋</button></div>';
    if(t)   return '<div class="nbody"><button class="btn primary sp-pick" style="flex:1;justify-content:center">Pick library &amp; ingest</button></div>';
    return '<div class="nbody"><button class="btn primary sp-connect" style="flex:1;justify-content:center">Connect with Microsoft (live)</button></div>';
  }

  // #917: the uploaded-documents node's action row. The node itself is DERIVED from
  // /admin/documents (syncDocumentsNode) - this button is how it grows.
  function upNodeButton(node){
    return '<div class="nbody"><button class="btn primary up-add" '+
      'style="flex:1;justify-content:center">Upload files</button></div>';
  }

  // #561: upload a document from the Connectors surface, which is where a person looks for
  // "add a source". The endpoint is the one that already exists (POST /admin/upload): the ACL
  // defaults to the uploader (#539), so the document is private to them and sharing stays a
  // separate, deliberate act (#538). Nothing here widens a read path.
  function openUploadPicker(){
    const modal=document.getElementById("spPicker");
    const body=document.getElementById("spPickerBody");
    document.getElementById("spPickerTitle").textContent="Upload a document";
    modal.classList.add("show");
    // The honest refusal, up front. A demo visitor has no session and /admin/upload answers
    // 401; letting them pick a file first and fail afterwards is the tile-that-always-403s
    // that #551 rejected, just moved one click later.
    //
    // The test is isDemoMode(), NOT `!authState.signed_in`. They are not the same thing and
    // the difference is a whole class of deployment: on a dev rig no real login is configured,
    // signed_in is permanently false, and the X-DBSearch-User switcher IS the identity - so
    // gating on signed_in refuses the upload on every rig the product is developed against.
    // isDemoMode() means precisely "a real login exists and this visitor has not used it",
    // which is the one case with no identity to upload as.
    if(isDemoMode()){
      body.innerHTML='<div class="qmeta">Sign in to upload your own documents. '+
        'The sample databases above are already connected and answerable.</div>';
      return;
    }
    // #575: a third audience choice - "My organization" - alongside the #539 private
    // default. "Specific people" stays OUT of this list on purpose: it is not an upload-time
    // value, it is the existing per-document Share flow (ADR 0017 grants) reached afterwards,
    // so upload itself stays two-valued. "My organization" only appears when this session
    // carries a verified Entra tid (authState.has_org, /auth/me) - a Google or local-account
    // session has no organization to publish into, and offering the option would just be a
    // tile that always 400s (#551's rule).
    const orgOption=authState.has_org ? '<option value="org">My organization</option>' : "";
    body.innerHTML=
      // #upOut FIRST: it carries the live stage stepper / outcome, which is what the person
      // is watching once they click Upload - at the bottom it sat below the fold (260821 bug).
      '<div id="upOut"></div>'+
      '<div class="up-row"><input id="upFile" type="file"></div>'+
      '<div class="up-row"><input id="upTitle" type="text" spellcheck="false" '+
        'placeholder="Title (optional) — defaults to the filename"></div>'+
      '<div class="up-row"><label class="qmeta" for="upAudience">Who can read this</label></div>'+
      '<div class="up-row"><select id="upAudience"><option value="">Only me</option>'+
        orgOption+'</select></div>'+
      '<div class="up-row"><span class="qmeta">To share with specific people, upload '+
        'privately, then use Share on the document.</span></div>'+
      '<div class="up-row"><button class="btn primary" id="upGo">Upload</button>'+
        '<span class="qmeta" id="upNote">Max 10MB. '+
        'PDF, Word, PowerPoint, Excel, CSV, Markdown, text or JSON.</span></div>';
    const out=document.getElementById("upOut");
    const go=document.getElementById("upGo");
    go.onclick=async ()=>{
      const f=document.getElementById("upFile").files[0];
      if(!f){ out.innerHTML='<div class="qerr">Choose a file first.</div>'; return; }
      const audience=document.getElementById("upAudience").value;
      go.disabled=true; const label=go.textContent; go.textContent="Uploading…";
      out.innerHTML='<div class="qmeta">Uploading…</div>';
      // #917: the upload is a SUBMIT now (202 + a real ingest job), so the picker follows
      // the job with the SAME stage stepper the SharePoint flow renders - the phases are
      // the runner's own (extracting/embedding/indexing), not a fake bar. The synchronous
      // refusals (size, type, quota, org-audience) still land in the catch below at click
      // time, exactly as before (#551).
      let sub;
      try{
        sub=await uploadDoc(f, document.getElementById("upTitle").value.trim(), audience);
      }catch(e){
        out.innerHTML='<div class="qerr">'+esc(e.message||e)+'</div>';
        go.disabled=false; go.textContent=label; return;
      }
      if(!sub || !sub.job_id){ go.disabled=false; go.textContent=label; return; }  // abandoned surface
      document.getElementById("upFile").value="";
      document.getElementById("upTitle").value="";
      followUploadJob(sub, audience, out, ()=>{ go.disabled=false; go.textContent=label; });
    };
  }

  /** #917: follow one upload's ingest job to its terminal state, painting the SharePoint
   *  stage stepper into the picker. Same poll resilience rules as the SP watcher: a 429 is
   *  a statement about the poller, not the job; only sustained silence becomes "lost sight
   *  of" - never "failed", because we do not know that it failed. */
  let upActive=null;   // #917: the in-flight upload the panel overview shows as a live row
  function followUploadJob(sub, audience, out, doneCb){
    const run={jobId:sub.job_id, status:sub.job_status||"queued", phase:"", error:"", misses:0};
    const paint=()=>{
      upActive=(run.status==="succeeded"||run.status==="failed"||run.status==="unknown")
        ? null : {title:sub.title, phase:run.phase, status:run.status};
      const sel=state.find(s=>s.uid===selected);
      if(sel && sel.derived){
        const pp=document.getElementById("panel");
        if(pp) renderDocsPanel(pp, sel);
      }
      if(!out.isConnected){ return; }                 // picker torn down; the job carries on
      if(run.status==="succeeded"){
        // "Ask below" is correct, and was verified by driving this page rather than by
        // reading it (#563): the canvas asks TWO surfaces per question - the router AND
        // the document bridge (#170/#255) - so an upload is answerable from the bar below.
        const audienceNote=audience==="org"
          ? "readable by everyone in your organization."
          : "private to you.";
        out.innerHTML='<div class="qmeta" style="color:var(--ok)">✓ '+esc(sub.title)+
          ' indexed, '+audienceNote+'</div>'+
          '<div class="qmeta">Ask about it in the question box below. Nothing to wire: '+
          'your documents are searchable as soon as they are indexed.</div>';
        return;
      }
      if(run.status==="failed"||run.status==="unknown"){
        // #181's contract, async edition: a file that parsed to nothing FAILS loudly.
        const friendly=/ParseProducedNoText/.test(run.error||"")
          ? "no extractable text in that file"
          : /UnsupportedMedia/.test(run.error||"")
            ? "unsupported file type" : (run.error||"see server logs");
        out.innerHTML='<div class="qerr">That upload did not finish: '+esc(friendly)+
          '. Nothing already indexed was lost - you can try again.</div>';
        return;
      }
      const cur=SP_STEP_OF[run.phase]===undefined?-1:SP_STEP_OF[run.phase];
      const word=SP_PHASE[run.phase]||run.phase||"Working";
      out.innerHTML='<div class="sp-progress show">'+
        '<div class="sp-steps">'+SP_STEPS.map((st,i)=>
          '<span class="sp-step'+(i<cur?" done":"")+(i===cur?" active":"")+'">'+
          '<i class="sp-dot"></i>'+st[1]+'</span>').join('<i class="sp-sep"></i>')+'</div>'+
        '<div class="sp-prog-label">'+esc(word)+'</div>'+
        '<div class="sp-prog-bar indet"><i style="width:25%"></i></div>'+
        '<div class="sp-prog-note">This keeps running if you close this - the document '+
          'appears on Your documents when it finishes.</div></div>';
    };
    paint();
    const poll=track(setInterval(async ()=>{
      let j;
      try{ j=await api("/ingest/jobs/"+encodeURIComponent(run.jobId)); }
      catch(e){
        const msg=String((e&&e.message)||e);
        if(/\b429\b|too many/i.test(msg)) return;
        run.misses+=1;
        if(run.misses>=25){
          clearInterval(poll); run.status="unknown";
          run.error="lost contact with the job while it was running"; paint(); doneCb();
        }
        return;
      }
      run.misses=0; run.status=j.status; run.phase=j.phase; run.error=j.error||"";
      if(j.status==="succeeded"||j.status==="failed"){
        clearInterval(poll);
        if(j.status==="succeeded") syncDocumentsNode();   // the node grows from server truth
        paint(); doneCb(); return;
      }
      paint();
    },700));
  }

  // #169: in-canvas library picker → ingest — list the connected tenant's SharePoint libraries
  // and ingest the chosen one into the queryable index, without leaving the canvas.
  /* ================= #880: an ingest is a JOB, and the surface has to outlive it ===========
     What the owner did, and what the UI told him: he pasted a folder link, the stage modal
     flashed and vanished, the node read "0 documents", so he concluded the ingest had failed
     and ran the entire crawl a second time. Both runs were almost certainly fine.

     The cause was one line. Since #569 (LAW 4) POST /connectors/sharepoint/finish returns 202
     the instant the job is QUEUED, carrying {job_id, job_status} and deliberately NO
     docs_indexed - and `api()` treats any r.ok, 202 included, as success. So the old
     `succeed(r)` fired on the acknowledgement rather than on the outcome: it closed the modal
     before the crawl had started, cleared the poller that would have reported it, and wrote
     `r.docs_indexed` - undefined - onto the node. "Ingested undefined docs" was the surface
     honestly reporting a field the 202 never had.

     So the run state lives HERE, at surface scope, not inside the picker's closure:

       - it outlives the modal (dismissing is allowed - LAW 4 means the job is server-side and
         does not care whether anyone is watching), and
       - it outlives the LIST, which re-renders when the lazily-fetched libraries land (#879).

     Polling moved from /connectors/sharepoint/ingest-progress to GET /ingest/jobs/{job_id}.
     Both existed; only one is per-JOB. The tenant progress store is keyed by tenant alone, so
     the owner's second run silently overwrote the first one's phase, and it never receives
     "indexing" at all (the runner writes that phase to the job checkpoint only). The job
     endpoint reports every phase, belongs to one run, and carries the terminal status the
     whole surface was missing. The job_id was already in the 202 and was being thrown away.  */

  const SP_STEPS=[["discovering","Find"],["fetching","Fetch"],["extracting","Extract"],
                  ["embedding","Embed"],["indexing","Index"]];
  // Every phase the pipeline can actually emit, in the user's words rather than the runner's.
  // `skipping` is in here because of a real defect: the runner emits it for an unchanged
  // document, STEP_OF["skipping"] was undefined, and paint(-1) then cleared every dot - so a
  // resumed crawl looked like it had restarted from nothing. It belongs to the fetch stage,
  // which is where the runner emits it from.
  const SP_PHASE={queued:"Queued", starting:"Starting", discovering:"Finding your documents",
    fetching:"Fetching documents", skipping:"Skipping unchanged documents",
    extracting:"Extracting text", embedding:"Embedding vectors", indexing:"Indexing",
    done:"Finishing up"};
  const SP_STEP_OF={starting:-1, queued:-1, discovering:0, fetching:1, skipping:1,
    extracting:2, embedding:3, indexing:4, done:5};

  // The live run, or null. One at a time on purpose: see spRunIsLive's use in the picker.
  let spRun=null;

  function spRunIsLive(){ return !!spRun && spRun.status!=="succeeded" && spRun.status!=="failed"; }

  /** Watch a queued job to its terminal state, whether or not anyone has the modal open.
   *  Tracked, so leaving Connectors stops the poll (#643) - the JOB continues regardless. */
  function spWatchJob(run){
    spRun=run;
    renderSpRun();
    const poll=track(setInterval(async()=>{
      if(spRun!==run){ clearInterval(poll); return; }
      // Backoff is counted in SKIPPED TICKS rather than a new interval, so there is still
      // exactly one timer to clear and `track()` still owns it (#643).
      if(run.backoff){ run.skip=(run.skip||0)+1; if(run.skip<run.backoff) return; run.skip=0; }        // superseded by a newer run
      let j;
      try{ j=await api("/ingest/jobs/"+encodeURIComponent(run.jobId)); }
      catch(e){
        const msg=String((e&&e.message)||e);
        // A 429 is the server saying SLOW DOWN, which is a statement about this poller and
        // says nothing whatever about the job. Counting it as a miss is how the first version
        // of this told the owner "That ingest did not finish" about a crawl that finished with
        // 5 documents: it had merely stopped being ALLOWED to look. Back off instead, and let
        // the run keep its state. (The server side of the same defect is METER_EXEMPT in
        // rate_limit.py - /ingest/jobs is a status read, not an ingest.)
        if(/\b429\b|too many/i.test(msg)){ run.backoff=Math.min((run.backoff||0)+1,10); renderSpRun(); return; }
        // Any other miss is not a failure either. The job is durable and the box may simply be
        // busy or redeploying; only a sustained silence is worth reporting, and even then the
        // honest word is "lost sight of", never "failed" - we do not know that it failed.
        run.misses=(run.misses||0)+1;
        if(run.misses>=25){ clearInterval(poll); run.status="unknown";
                            run.error="lost contact with the job while it was running"; }
        renderSpRun(); return;
      }
      run.misses=0; run.backoff=0;
      run.status=j.status; run.phase=j.phase;
      run.done=j.docs_done||0; run.total=j.docs_total||0; run.skipped=j.docs_skipped||0;
      if(j.status==="succeeded"||j.status==="failed"){
        clearInterval(poll);
        run.error=j.error||"";
        await spSettleRun(run);
      }
      renderSpRun();
    },1200));
    run.poll=poll;
  }

  /** Terminal handling. The count is RE-READ from the server that just finished the job -
   *  never carried over from the response that started it, which is the mistake that put
   *  "0 documents" (and then "undefined docs") on the node while the crawl was still running.
   *  syncSharePointNodes reads /admin/sources, whose doc_count the crawl commits BEFORE the
   *  job is published as succeeded, so by the time we are here it cannot be stale. */
  async function spSettleRun(run){
    if(run.status!=="succeeded") return;
    await syncSharePointNodes(); syncDocumentsNode();
    const ing=spIngested[run.tenant];
    run.docs=ing?ing.docs:0;
    const n=state.find(x=>x.uid===run.uid);
    if(n) flashCenter(n.uid);
    const bar=document.getElementById("statusbar");
    if(bar) bar.textContent="✓ "+run.docs+" document"+(run.docs===1?"":"s")+" from "+run.label+
      " — now ask across every composed store below.";
  }

  /** Paint the run into the modal, if the modal happens to be open. A closed modal is not an
   *  error state and not a reason to stop: the job is server-side (LAW 4) and the node updates
   *  either way. This is the only function that knows what a run LOOKS like. */
  function renderSpRun(){
    const host=document.getElementById("spProgress");
    if(!host) return;                        // modal is down; the watcher carries on regardless
    if(!spRun){ host.innerHTML=""; host.classList.remove("show"); return; }
    const r=spRun;
    host.classList.add("show");

    if(r.status==="succeeded"){
      // #880 P3: say what happened, in words, and let the PERSON close it. The old surface
      // closed itself on the 202 - the user never saw a completion at all, which is why the
      // node's stale "0" read as a failed ingest rather than as a count not yet refreshed.
      host.innerHTML='<div class="sp-done">'+
        '<div class="sp-done-head">✓ All documents from '+esc(r.label)+' ingested.</div>'+
        '<div class="sp-done-sub">'+r.docs+' document'+(r.docs===1?"":"s")+
          ' indexed and searchable'+(r.skipped?', '+r.skipped+' unchanged and skipped':'')+
          '. Ask about them from the bar below.</div>'+
        '<button class="btn primary sp-done-ok" type="button">Done</button></div>';
      host.querySelector(".sp-done-ok").onclick=()=>{ spRun=null; closeSpPicker(); };
      focusFirstIn(host);
      return;
    }
    if(r.status==="failed"||r.status==="unknown"){
      host.innerHTML='<div class="sp-done sp-done-bad">'+
        '<div class="sp-done-head">That ingest did not finish.</div>'+
        '<div class="sp-done-sub">'+esc(r.error||"see server logs")+
          '. Nothing already indexed was lost - you can start it again.</div>'+
        '<button class="btn sp-done-ok" type="button">Close</button></div>';
      host.querySelector(".sp-done-ok").onclick=()=>{ spRun=null; renderSpRun(); renderSpList(); };
      focusFirstIn(host);
      return;
    }

    // Running. A stage stepper, not a spinner: the point is WHICH of five things is happening.
    const cur=SP_STEP_OF[r.phase]===undefined?-1:SP_STEP_OF[r.phase];
    const pct=r.total?Math.round(100*r.done/r.total):0;
    const word=SP_PHASE[r.phase]||r.phase||"Working";
    host.innerHTML='<div class="sp-progress">'+
      '<div class="sp-steps">'+SP_STEPS.map((st,i)=>
        '<span class="sp-step'+(i<cur?" done":"")+(i===cur?" active":"")+'">'+
        '<i class="sp-dot"></i>'+st[1]+'</span>').join('<i class="sp-sep"></i>')+'</div>'+
      '<div class="sp-prog-label">'+esc(word)+(r.total?" "+r.done+"/"+r.total:"")+'</div>'+
      '<div class="sp-prog-bar'+(r.total?"":" indet")+'"><i style="width:'+
        (r.total?pct:25)+'%"></i></div>'+
      '<div class="sp-prog-note">This keeps running if you close this - the documents will '+
        'appear on the source when it finishes.</div></div>';
  }

  function closeSpPicker(){
    const p=document.getElementById("spPicker");
    if(p) p.classList.remove("show");
  }

  /* ---------------- #879: the picker opens INSTANTLY, and reads as a choice ----------------
     Measured on prod before this change: 11.2 seconds of blank modal. `list_drives` walks
     /sites?search=* then /sites/root then one /sites/{id}/drives PER SITE, serially, and the
     old picker awaited all of it before painting anything at all - including the folder-link
     field, which is the path the owner actually used and needs none of that enumeration.

     And when it did paint, all seven rows read "Documents" in bold: that is the DRIVE name,
     and every default library in every tenant is called Documents. The site name - the only
     thing distinguishing them - was in the payload the whole time, demoted to grey 11px. The
     owner read a list of tenant sites as a statement about his own files and asked "since
     when i have so many docs?". That reaction is the bug report.                            */

  let spDrives=null, spDrivesErr="", spShowSystem=false;

  function spRowLabel(d){
    // <Site> — <Library>, because the site is what distinguishes one row from another. The
    // library name is kept rather than dropped: a site can genuinely have several.
    const site=(d.siteName||"").trim(), lib=(d.driveName||"Documents").trim();
    if(site && lib && site.toLowerCase()!==lib.toLowerCase()) return site+" — "+lib;
    return site||lib;
  }

  function renderSpList(){
    const body=document.getElementById("spPickerBody");
    if(!body) return;
    const listEl=body.querySelector(".sp-list");
    if(!listEl) return;
    if(spDrivesErr){
      listEl.innerHTML='<div class="qerr">'+esc(spDrivesErr)+'</div>'; return;
    }
    if(spDrives===null){
      listEl.innerHTML='<div class="qmeta">Finding the libraries in your tenant…</div>'; return;
    }
    if(!spDrives.length){
      listEl.innerHTML='<div class="qmeta">No document libraries found for this tenant.</div>'; return;
    }
    const real=spDrives.filter(d=>!d.system), sys=spDrives.filter(d=>d.system);
    const row=(d)=>'<div class="sp-drive"><div style="min-width:0"><b>'+esc(spRowLabel(d))+'</b>'+
      '<div class="sp-sub">'+esc(d.web||"")+'</div></div>'+
      '<button class="btn sp-ingest" data-id="'+esc(d.driveId)+'"'+
      (spRunIsLive()?' disabled':'')+'>Ingest</button></div>';
    let html=(real.length?real:sys).map(row).join("");
    // #879: system sites are DE-EMPHASISED, not hidden. Nobody ingests contentTypeHub, but a
    // list that silently drops rows is its own defect - the owner would have no way to tell
    // "we filtered it" from "your tenant does not have it".
    if(real.length && sys.length){
      html+='<button class="sp-sysmore" type="button">'+
        (spShowSystem?"Hide":"Show")+" "+sys.length+" system site"+(sys.length===1?"":"s")+
        '</button>'+(spShowSystem?'<div class="sp-sys">'+sys.map(row).join("")+'</div>':'');
    }
    listEl.innerHTML=html;
    const more=listEl.querySelector(".sp-sysmore");
    if(more) more.onclick=()=>{ spShowSystem=!spShowSystem; renderSpList(); };
    listEl.querySelectorAll(".sp-ingest").forEach(btn=>{
      btn.addEventListener("click",()=>{
        const d=(spDrives||[]).find(x=>String(x.driveId)===btn.dataset.id);
        if(d) spStartIngest({drive_id:d.driveId}, "“"+spRowLabel(d)+"”");
      });
    });
  }

  /** Queue the crawl and hand it to the watcher. This function deliberately does NOT decide
   *  that anything succeeded: a 202 means "queued", and that is all it is allowed to mean. */
  async function spStartIngest(payload, label){
    const node=state.find(x=>x.uid===spPickerUid);
    const tenant=node && node.config && node.config.tenant;
    if(!tenant) return;
    spRun={tenant:tenant, uid:spPickerUid, label:label, status:"queued", phase:"queued",
           done:0, total:0, skipped:0, docs:0, error:"", jobId:""};
    renderSpRun(); renderSpList();                 // buttons disable the moment it is queued
    let r;
    try{
      r=await api("/connectors/sharepoint/finish",{method:"POST",
        body:JSON.stringify(Object.assign({tenant:tenant},payload))});
    }catch(e){
      spRun.status="failed";
      spRun.error=String((e&&e.message)||e);
      renderSpRun(); renderSpList(); return;
    }
    if(!r||!r.job_id){
      // Older server, or a shape change. Say so rather than pretending to watch nothing.
      spRun.status="failed";
      spRun.error="the server did not return a job to follow";
      renderSpRun(); renderSpList(); return;
    }
    spRun.jobId=r.job_id;
    spWatchJob(spRun);
  }

  let spPickerUid="";

  function openSpPicker(node){
    const tenant=node.config && node.config.tenant; if(!tenant) return;
    spPickerUid=node.uid;
    const modal=document.getElementById("spPicker");
    const body=document.getElementById("spPickerBody");
    // #561: this shell serves two dialogs, so each one OWNS the title.
    document.getElementById("spPickerTitle").textContent="Add SharePoint documents";
    modal.classList.add("show");

    // The dialog, in one function so "when is it painted" is a single decision rather than a
    // property of where the code happens to sit.
    function paintShell(){
      body.innerHTML=
        '<div class="sp-linkrow"><input id="spLink" type="text" spellcheck="false" '+
          'placeholder="Paste a SharePoint folder or file link"'+(spRunIsLive()?" disabled":"")+'>'+
          '<button class="btn primary sp-link-ingest"'+(spRunIsLive()?" disabled":"")+
          '>Ingest folder</button></div>'+
        '<div class="sp-hint">Right-click a folder in SharePoint or OneDrive and copy its link. '+
          'Everything inside it is ingested, keeping each file\'s existing permissions.</div>'+
        '<div class="sp-or">— or pick a whole library —</div>'+
        '<div class="sp-list"></div>';
      renderSpRun();
      renderSpList();
      const linkInput=document.getElementById("spLink");
      if(linkInput && !spRunIsLive()) linkInput.focus();
      body.querySelector(".sp-link-ingest").addEventListener("click",()=>{
        const link=(linkInput.value||"").trim();
        if(!link){ toast("Paste a SharePoint sharing link first."); return; }
        spStartIngest({share_link:link}, "the shared folder");
      });
    }
    paintShell();   // #879: BEFORE any network call — the folder-link path needs no enumeration

    // Fetched, not awaited. The list fills in beside a modal the user can already use.
    if(spDrives===null && !spDrivesErr){
      api("/connectors/sharepoint/drives?tenant="+encodeURIComponent(tenant))
        .then(d=>{ spDrives=Array.isArray(d)?d:[]; spDrivesErr=""; })
        .catch(e=>{ spDrivesErr="Could not list your libraries: "+((e&&e.message)||e)+
                                ". The folder link above still works."; })
        .then(()=>renderSpList());
    }
  }

  function center(el){ return {x:el.offsetLeft+el.offsetWidth/2, y:el.offsetTop+el.offsetHeight/2}; }

  function drawEdges(){
    if(!alive) return;
    const hub=document.getElementById("hub");
    const hc=center(hub);
    let s='';
    state.forEach(node=>{
      const el=world.querySelector('.node[data-uid="'+node.uid+'"]'); if(!el) return;
      const c=center(el);
      // #941: a solid line to the router means queries reach this store. An uncomposed one
      // is not in the catalog, so the dashed draft edge is the true drawing.
      const connected=node.status==="connected" && !isUncomposed(node);
      const col=connected?"var(--accent-line)":"var(--faint)";
      const mx=(c.x+hc.x)/2;
      const d="M "+c.x+" "+c.y+" C "+mx+" "+c.y+", "+mx+" "+hc.y+", "+hc.x+" "+hc.y;
      s+='<path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="'+(connected?2:1.4)+'" '+
         (connected?'opacity="0.9"':'stroke-dasharray="5 6" opacity="0.55"')+'/>';
      s+='<circle cx="'+c.x+'" cy="'+c.y+'" r="3.2" fill="'+col+'"/>';
    });
    edges.innerHTML=s;
  }

  /* ---------------- pan & zoom (Figma-style) ---------------- */
  const canvasEl=document.getElementById("canvas");
  const view={x:0,y:0,z:1};
  const Z_MIN=0.2, Z_MAX=3;
  function applyView(animate){
    world.classList.toggle("animating",!!animate);
    world.style.transform="translate("+view.x+"px,"+view.y+"px) scale("+view.z+")";
    const pct=document.getElementById("zoomPct"); if(pct) pct.textContent=Math.round(view.z*100)+"%";
    if(animate) setTimeout(()=>world.classList.remove("animating"),300);
  }
  function screenToWorld(sx,sy){
    const r=canvasEl.getBoundingClientRect();
    return {x:(sx-r.left-view.x)/view.z, y:(sy-r.top-view.y)/view.z};
  }
  function zoomAt(clientX,clientY,factor){
    const r=canvasEl.getBoundingClientRect();
    const nz=Math.min(Z_MAX,Math.max(Z_MIN,view.z*factor));
    const px=clientX-r.left, py=clientY-r.top;
    const wx=(px-view.x)/view.z, wy=(py-view.y)/view.z;
    view.z=nz; view.x=px-wx*nz; view.y=py-wy*nz; applyView();
  }
  function zoomBtn(f){ const r=canvasEl.getBoundingClientRect();
    zoomAt(r.left+canvasEl.clientWidth/2, r.top+canvasEl.clientHeight/2, f); }
  // ---------- the dock floats OVER the graph (#345) ----------
  // .qdock is absolutely positioned over .canvas and GROWS upward as an answer renders. So the
  // space actually free for the graph is shorter than clientHeight, and any fit measured against
  // clientHeight parks nodes underneath the very answer that is explaining them. That matters
  // more here than it would elsewhere: the routed graph is the thing that SHOWS permission-aware
  // routing happened, so covering it hides the product's whole point.
  //
  // Every fit below therefore measures the CLEAR band above the dock instead of the full canvas.
  function dockRect(){
    const d=document.getElementById("qdock");
    if(!d||!d.offsetParent) return null;
    const r=d.getBoundingClientRect(), c=canvasEl.getBoundingClientRect();
    return {l:r.left-c.left, t:r.top-c.top, r:r.right-c.left, b:r.bottom-c.top};
  }
  // 14px so a node never sits flush against the dock edge; the floor keeps a very tall answer
  // on a short window from collapsing the band to nothing and driving the zoom math to zero.
  function clearH(){
    const d=dockRect();
    return Math.max(140, d ? d.t-14 : canvasEl.clientHeight);
  }
  // Every box on the canvas in SCREEN coordinates, so occlusion is a plain rect test.
  function screenBoxes(){
    return [[HUB.x-90,HUB.y-70,180,140]].concat((state||[]).map(n=>[n.x,n.y,212,120]))
      .map(([wx,wy,w,h])=>{
        const l=wx*view.z+view.x, t=wy*view.z+view.y;
        return {l:l, t:t, r:l+w*view.z, b:t+h*view.z};
      });
  }
  // `band` is passed explicitly rather than re-measured: fitView sizes the zoom against the band
  // it measured, and the dock can grow between the two calls (the document half resolves a beat
  // after the answer). Measuring twice meant zooming for a tall band and centring in a short one,
  // which pushed the top row of nodes off the canvas - visible as clipped cards, not a clean fit.
  function centerOn(wx,wy,z,animate,band){
    if(z) view.z=Math.min(Z_MAX,Math.max(Z_MIN,z));
    const h=band||clearH();
    view.x=canvasEl.clientWidth/2 - wx*view.z;
    view.y=h/2 - wy*view.z; applyView(animate);
  }
  function fitView(animate){
    let minx=HUB.x-90,miny=HUB.y-70,maxx=HUB.x+90,maxy=HUB.y+70;
    state.forEach(n=>{ minx=Math.min(minx,n.x); miny=Math.min(miny,n.y);
                       maxx=Math.max(maxx,n.x+212); maxy=Math.max(maxy,n.y+120); });
    const pad=90, bw=(maxx-minx)+pad*2, bh=(maxy-miny)+pad*2;
    const h=clearH();
    const z=Math.min(Z_MAX,Math.max(Z_MIN,Math.min(canvasEl.clientWidth/bw, h/bh)));
    centerOn((minx+maxx)/2,(miny+maxy)/2,z,animate,h);
  }
  // #345: nudge the graph out from under the dock by the MINIMUM amount, keeping the zoom the
  // user is on. An earlier version re-fitted instead, which zoomed the whole canvas on every
  // answer - correct about the occlusion, far too heavy-handed about fixing it. Zooming out is
  // the fallback for when panning alone cannot clear the dock without clipping the top row.
  function keepGraphClear(){
    const d=dockRect(); if(!d||!state||!state.length) return;
    let need=0, top=Infinity;
    screenBoxes().forEach(b=>{
      top=Math.min(top,b.t);
      if(b.l<d.r && b.r>d.l && b.b>d.t) need=Math.max(need, b.b-d.t+14);
    });
    // The band can SHRINK under a graph that already fitted, because the answer keeps growing
    // after the fit was computed. Nothing is then overlapping the dock - the graph has simply
    // outgrown the space above it and spilled off the top. Measured live: cards clipped at the
    // canvas edge while the dock itself was clear. Re-fit against the band as it is now.
    if(top<0){ fitView(true); return; }
    if(need<=0) return;
    const room=top-14;                    // how far up we can pan before clipping the top row
    if(need<=room){ view.y-=need; applyView(true); }
    else fitView(true);
  }
  // wheel / trackpad-pinch = zoom toward cursor — but let overlays (dock/menus/panels)
  // scroll their own content natively instead of hijacking the wheel to zoom (#173).
  canvasEl.addEventListener("wheel",e=>{
    if(e.target.closest(".qdock,.provmenu,.ctxmenu,.panel")) return;
    e.preventDefault();
    zoomAt(e.clientX,e.clientY, Math.exp(-e.deltaY*0.0015)); },{passive:false});
  // drag empty space = pan
  canvasEl.addEventListener("pointerdown",e=>{
    if(e.button!==0) return;
    if(e.target.closest(".node,.qdock,.zoomctl,.hub,.provmenu,.ctxmenu,.panel")) return;
    closeCtxMenu();
    const sx=e.clientX, sy=e.clientY, ox=view.x, oy=view.y; let moved=false;
    canvasEl.classList.add("panning"); canvasEl.setPointerCapture(e.pointerId);
    const move=ev=>{ view.x=ox+(ev.clientX-sx); view.y=oy+(ev.clientY-sy);
      if(Math.abs(ev.clientX-sx)+Math.abs(ev.clientY-sy)>3) moved=true; applyView(); };
    const up=()=>{ try{canvasEl.releasePointerCapture(e.pointerId);}catch(_){}
      canvasEl.classList.remove("panning");
      canvasEl.removeEventListener("pointermove",move); canvasEl.removeEventListener("pointerup",up);
      if(!moved && selected){ selected=null; renderAll(); } };   // click empty = deselect
    canvasEl.addEventListener("pointermove",move); canvasEl.addEventListener("pointerup",up);
  });
  document.getElementById("zoomIn").onclick=()=>zoomBtn(1.2);
  document.getElementById("zoomOut").onclick=()=>zoomBtn(1/1.2);
  document.getElementById("zoomFit").onclick=()=>fitView(true);
  // #345: the dock's height changes on every render path - Route, Ask, the document half that
  // resolves a beat later, and closing the panel. Watching the DOCK covers all of them at once,
  // where a keep-clear call per call site is something the next render path forgets to add.
  // keepGraphClear is a no-op unless something is GENUINELY covered, so an answer that never
  // reached the graph leaves the user's arrangement exactly where they put it.
  if(window.ResizeObserver){
    let pending=false;
    new ResizeObserver(()=>{
      if(pending) return;
      pending=true;                       // coalesce: the dock can resize twice in one frame
      requestAnimationFrame(()=>{ pending=false; keepGraphClear(); });
    }).observe(document.getElementById("qdock"));
  }
  document.getElementById("zoomPct").onclick=()=>{
    const c=screenToWorld(canvasEl.getBoundingClientRect().left+canvasEl.clientWidth/2,
                          canvasEl.getBoundingClientRect().top+canvasEl.clientHeight/2);
    centerOn(c.x,c.y,1,true);
  };

  /* ---------------- drag ---------------- */
  function wireDrag(el,node){
    const head=el.querySelector(".nhead");
    head.addEventListener("pointerdown",e=>{
      if(e.button!==0) return;
      el._moved=false; el.classList.add("dragging");
      const sx=e.clientX, sy=e.clientY, ox=node.x, oy=node.y;
      head.setPointerCapture(e.pointerId);
      const move=ev=>{
        const dx=ev.clientX-sx, dy=ev.clientY-sy;
        if(Math.abs(dx)+Math.abs(dy)>3) el._moved=true;
        node.x=Math.max(10,ox+dx/view.z); node.y=Math.max(10,oy+dy/view.z);
        el.style.left=node.x+"px"; el.style.top=node.y+"px"; drawEdges();
      };
      const up=ev=>{
        head.releasePointerCapture(e.pointerId);
        head.removeEventListener("pointermove",move);
        head.removeEventListener("pointerup",up);
        el.classList.remove("dragging");
        // #818: the drop IS the mutation - nothing else renders after a pure move, so
        // without this a move-then-reload lost the position even from localStorage
        // (persistence used to depend on some LATER render happening to fire saveCanvas).
        if(el._moved) saveCanvas();
        setTimeout(()=>{el._moved=false;},0);
      };
      head.addEventListener("pointermove",move);
      head.addEventListener("pointerup",up);
    });
  }

  /* ---------------- config panel ---------------- */
  /* ---------------- #258 named ACL principals ----------------
     The ACL is the one field where a typo is silent AND consequential: a wrong oid does not
     error, it just means nobody (or the wrong body) can read the store. So the operator picks
     a NAME, and we keep showing the oids — the name is the check, the oid is the truth. */
  let principalDir={available:false, principals:[], reason:"directory not loaded yet"};
  function loadPrincipals(){
    return api("/admin/principals")
      .then(d=>{ if(d) principalDir=d; })
      .catch(e=>{ principalDir={available:false, principals:[],
                                reason:"directory lookup failed: "+(e.message||e)}; });
  }
  function principalName(oid){
    const p=(principalDir.principals||[]).find(x=>x.oid===oid);
    return p?p.name:"";
  }
  // #881: the directory's own vocabulary is not the operator's. Graph calls a role a
  // "directoryRole", which is the wrong word to put beside a name someone is about to grant
  // access to - and before #881 the kind was not even carried, so a role read "(group)".
  // Sharing with "everyone holding Global Administrator" is a real and reasonable choice; it
  // just has to SAY that is what it is.
  const KIND_NOUN={user:"person", group:"group", directoryRole:"admin role"};
  function kindNoun(kind){ return KIND_NOUN[kind]||kind||"principal"; }
  function aclPickerHtml(node){
    // available=false is NOT "no groups exist" — say which it is, and keep the oid field usable.
    if(!principalDir.available)
      return '<div class="hint">'+esc(principalDir.reason||"directory unavailable")+'</div>';
    const chosen=new Set(node.acl);
    const opts=(principalDir.principals||[]).filter(p=>!chosen.has(p.oid))
      .map(p=>'<option value="'+esc(p.oid)+'">'+esc(p.name)+' ('+esc(kindNoun(p.kind))+')</option>').join("");
    if(!opts) return '<div class="hint">everyone in your directory already has access</div>';
    return '<select data-aclpick><option value="">+ add a group or user…</option>'+opts+'</select>';
  }
  function refreshAclNames(inp,node){
    // The resolution line is a VERIFICATION aid: left stale it would vouch for a principal
    // you just deleted. Replace it in place rather than re-rendering the panel, which would
    // steal focus mid-edit.
    const el=inp.parentElement.querySelector("[data-aclnames]");
    if(el) el.outerHTML=aclNamesHtml(node);
  }
  function aclNamesHtml(node){
    // always render the element (even empty) so the input handler has a node to replace
    if(!node.acl.length) return '<div class="hint" data-aclnames></div>';
    // An oid we cannot resolve is called out rather than shown as if understood: it may be a
    // typo, a deleted group, or simply a tenant we cannot enumerate — all worth seeing.
    return '<div class="hint" data-aclnames>'+node.acl.map(o=>{
      const n=principalName(o);
      return n?'✓ '+esc(n):'? '+esc(o.slice(0,8))+'… <i>unresolved</i>';
    }).join(' · ')+'</div>';
  }

  /* ---------------- #917: the uploads OVERVIEW (owner's spec, 260821) ------------------
   * "The user has to have an overview of ALL FILES uploaded within the canvas... if they
   * upload 100 files they can't have 100 nodes... node just shows N docs; when they
   * click, it shows all the files, then Upload files."
   * One aggregated node; clicking it fills THIS panel: Upload files, a filter past 8 rows,
   * and per-file Share (grant by email, #582 D5) + Delete (#594, owner-only server-side).
   * Delete confirms INLINE - a native confirm() would block the page (and the browser
   * automation that verifies it). Rows render from upDocsCache; every mutation funnels
   * through syncDocumentsNode so the node count and this list cannot disagree. */
  let upFilter="", upConfirmDel="", upShareOpen="", upBusy="";
  function upAudience(d){
    const acl=d.allowed_principals||[];
    if(acl.some(p=>String(p).startsWith("tenant:"))) return ["org","var(--k-csv)"];
    if(acl.length>1 || (d.shared_with_you===false && acl.length===1
        && authState.oid && acl[0]!==authState.oid)) return ["shared","var(--warn)"];
    return ["private","var(--faint)"];
  }
  function renderDocsPanel(p, node){
    const all=upDocsCache;
    const f=upFilter.trim().toLowerCase();
    const docs=f?all.filter(d=>String(d.title||d.doc_external_id).toLowerCase().includes(f)):all;
    const rows=docs.map(d=>{
      const id=d.doc_external_id, t=d.title||id;
      const [aud,color]=upAudience(d);
      const busy=upBusy===id;
      const del=upConfirmDel===id
        ? '<button class="btn updoc-del2" data-id="'+esc(id)+'"'+(busy?' disabled':'')+
          ' style="color:var(--err,#c33)">'+(busy?"Deleting…":"Really delete?")+'</button>'
        : '<button class="btn updoc-del" data-id="'+esc(id)+'" title="Delete this document '+
          'and everything that belonged to it">Delete</button>';
      const share=upShareOpen===id
        ? '<div class="up-row" style="margin-top:6px"><input type="email" class="updoc-mail" '+
          'data-id="'+esc(id)+'" placeholder="colleague@company.com" style="flex:1">'+
          '<button class="btn primary updoc-share2" data-id="'+esc(id)+'"'+
          (busy?' disabled':'')+'>'+(busy?"Sharing…":"Share")+'</button></div>'
        : '';
      return '<div class="updoc-row" style="border-bottom:1px solid '+
        'color-mix(in srgb,var(--faint) 25%,transparent);padding:7px 0">'+
        '<div style="display:flex;align-items:center;gap:8px;min-width:0">'+
        '<span class="updoc-title" style="flex:1;min-width:0;overflow:hidden;'+
          'text-overflow:ellipsis;white-space:nowrap" title="'+esc(t)+'">'+esc(t)+'</span>'+
        '<span class="pill" style="color:'+color+'">'+aud+'</span>'+
        '<button class="btn updoc-sharebtn" data-id="'+esc(id)+'">Share</button>'+del+
        '</div>'+share+'</div>';
    }).join("");
    const active=(upActive && upActive.status && upActive.status!=="succeeded"
                  && upActive.status!=="failed")
      ? '<div class="updoc-row" style="padding:7px 0;color:var(--faint)">⟳ '+
        esc(upActive.title||"uploading")+' — '+esc(SP_PHASE[upActive.phase]||upActive.phase||
        "working")+'…</div>'
      : '';
    // NOT class="ph": .ph is the full-height CENTERED empty-state placeholder, and using it
    // as a header consumed the whole panel and shoved the rows below the fold (260821 bug).
    // This panel is a node panel like any other, so it gets the same .pcard/.phead shell.
    const def=kindDef(node.kind);
    p.innerHTML=
      '<div class="pcard" style="--k:'+kindColor(node.kind)+'">'+
      '<div class="phead"><span class="mono-chip">'+def.mono+'</span>'+
        '<div><b>Your documents</b><div class="pk">'+all.length+
        ' document'+(all.length===1?"":"s")+' · private to you unless shared</div></div></div>'+
      // #950 (owner ruling, 260824): NO upload button here. Once #950 gave the node's own
      // button the picker, this was a SECOND "Upload files" on screen at the same time - the
      // duplication the owner had just been confused by, arriving from the other side. One
      // action, one affordance, and it lives on the canvas node; this panel only SHOWS what is
      // present (and Share/Delete per row, which are per-document and belong to a listing).
      (all.length>8
        ? '<div class="up-row"><input type="text" class="updoc-filter" placeholder="Filter '+
          all.length+' documents…" value="'+esc(upFilter)+'" style="flex:1"></div>'
        : '')+
      '<div class="updoc-list" style="overflow-y:auto;max-height:calc(100vh - 260px)">'+
        active+(rows||'<div style="color:var(--faint);padding:8px 0">'+
        (f?"Nothing matches that filter.":"Nothing uploaded yet.")+'</div>')+'</div>'+
      '<div style="color:var(--faint);font-size:11px;margin-top:8px">Sharing with specific '+
        'people grants read access to the one document only. Full management stays in '+
        'Admin → Your data.</div>'+
      '</div>';
    const flt=p.querySelector(".updoc-filter");
    if(flt){
      flt.oninput=()=>{ upFilter=flt.value; renderDocsPanel(p, node);
        const nf=p.querySelector(".updoc-filter"); if(nf){ nf.focus(); nf.setSelectionRange(nf.value.length,nf.value.length); } };
    }
    p.querySelectorAll(".updoc-sharebtn").forEach(b=>{ b.onclick=()=>{
      upShareOpen=(upShareOpen===b.dataset.id?"":b.dataset.id); upConfirmDel="";
      renderDocsPanel(p, node);
    };});
    p.querySelectorAll(".updoc-share2").forEach(b=>{ b.onclick=async ()=>{
      const id=b.dataset.id;
      const mail=(p.querySelector('.updoc-mail[data-id="'+CSS.escape(id)+'"]')||{}).value||"";
      if(!mail.trim()){ toast("Enter the colleague's email first."); return; }
      upBusy=id; renderDocsPanel(p, node);
      try{
        await api("/documents/"+encodeURIComponent(id)+"/grants",
                  {method:"POST",body:JSON.stringify({grantee_email:mail.trim()})});
        toast("Shared with "+mail.trim()+".");
        upShareOpen="";
      }catch(e){ toast("Could not share - "+(e.message||e)); }
      upBusy=""; syncDocumentsNode(); renderDocsPanel(p, node);
    };});
    p.querySelectorAll(".updoc-del").forEach(b=>{ b.onclick=()=>{
      upConfirmDel=b.dataset.id; upShareOpen=""; renderDocsPanel(p, node);
    };});
    p.querySelectorAll(".updoc-del2").forEach(b=>{ b.onclick=async ()=>{
      const id=b.dataset.id;
      upBusy=id; renderDocsPanel(p, node);
      try{
        await api("/documents/"+encodeURIComponent(id),{method:"DELETE"});
        toast("Deleted.");
      }catch(e){ toast("Could not delete - "+(e.message||e)); }
      upBusy=""; upConfirmDel="";
      await syncDocumentsNode();          // count + list from server truth, one funnel
      const still=state.find(s=>s.derived);
      if(still){ renderDocsPanel(p, still); }
      else { selected=null; renderPanel(); }   // last doc gone -> node gone -> empty panel
    };});
  }

  function renderPanel(){
    if(!alive) return;
    const p=document.getElementById("panel");
    if(setupMode){ renderSetupPanel(p); return; }
    const node=state.find(s=>s.uid===selected);
    if(!node){
      p.innerHTML='<div class="ph"><div class="pg">◇</div><div><b>No source selected</b>'+
        '<div style="color:var(--faint);font-size:12px;margin-top:4px">Add a source from the left,'+
        ' or click a node to configure its connection, business unit, and permissions.</div></div></div>';
      return;
    }
    if(node.derived){ renderDocsPanel(p, node); return; }   // #917: the uploads overview
    const def=kindDef(node.kind);
    let fields='';
    if(def.unknown){
      // Honest, and deliberately NOT a downgrade: this build has no editor for the kind, but
      // the node keeps it and composes with it. Saying so beats silently rewriting it to
      // `local` and composing an empty index that looks healthy (#200).
      fields+='<div class="probe-line warn"><span class="dot"></span>This canvas has no '+
        'editor for kind <b>'+esc(node.kind)+'</b>, so its connection fields are not shown. '+
        'The store is kept exactly as composed - edit it through the setup chat.</div>';
    }
    // #673: standing panel copy for a kind whose BEHAVIOUR needs stating, not just its fields.
    // S3's is that every ingested document is visible to the linking user alone, because S3
    // has no per-object permission DBSearch can read. A narrow ACL nobody was told about
    // reads as a broken search; saying it here is the difference between a limit and a bug.
    if(def.note){
      fields+='<div class="probe-line" style="align-items:flex-start;line-height:1.45">'+
        '<span class="dot"></span><span>'+esc(def.note)+'</span></div>';
    }
    def.fields.forEach(f=>{
      const v=node.config[f.k]!==undefined?node.config[f.k]:"";
      // ADR 0010 s3 (#417): a signed-in self-serve user's credential goes through /secrets
      // ONCE and only the returned handle ever lands in config. A prefilled-and-working
      // ${ENV} ref keeps today's rendering (legal form 2, operator affordance) - the
      // credential control appears for the empty field and for a stored handle.
      if(f.secret && selfServeSecrets() && !isEnvRef(v)){
        if(isSecretHandle(v) && !(node._replacing&&node._replacing[f.k])){
          fields+='<div class="field"><label>'+f.k+' <span class="hint">credential</span></label>'+
            '<div class="secretrow"><input value="'+f.k+' is set" disabled data-sechint="'+esc(v)+'">'+
            '<button class="btn" data-secreplace="'+f.k+'" style="position:absolute;right:6px;top:50%;'+
            'transform:translateY(-50%);padding:2px 9px;font-size:11px">Replace</button></div></div>';
        }else{
          // type=password: the value is masked while typed and NEVER bound to [data-cfg], so
          // no keystroke reaches node.config or localStorage - only the handle does, on Set.
          fields+='<div class="field"><label>'+f.k+' <span class="hint">credential</span></label>'+
            '<div class="secretrow"><input type="password" data-secretfield="'+f.k+'" '+
            'placeholder="paste your '+f.k+' — stored encrypted, shown never again" '+
            'autocomplete="new-password" spellcheck="false">'+
            '<button class="btn" data-secsave="'+f.k+'" style="position:absolute;right:6px;top:50%;'+
            'transform:translateY(-50%);padding:2px 9px;font-size:11px">Set</button></div></div>';
        }
        return;
      }
      // ADR 0011 s4: the "env secret" label and the ENV tag are an OPERATOR affordance -
      // they tell you the value is meant to be a ${ENV} ref this server resolves. A
      // non-operator has no env refs to label (the server now refuses them at compose,
      // #423), so showing the tag advertised a capability they will be told they don't
      // have. They get the plain "connection" label instead.
      const envTag = f.secret && CFG_OPERATOR;
      fields+='<div class="field"><label>'+f.k+' <span class="hint">'+(envTag?"env secret":"connection")+'</span></label>'+
        '<div class="secretrow"><input data-cfg="'+f.k+'" value="'+esc(v)+'" placeholder="'+esc(f.ph)+'" spellcheck="false">'+
        (envTag?'<span class="tag">ENV</span>':'')+'</div></div>';
    });
    // #941: the probe line reported REACHABILITY and the reader took it for readiness. The
    // uncomposed branch goes FIRST because the probe genuinely did succeed - this is not a
    // failure to report, it is a second fact the old line had no room for.
    const probe = isUncomposed(node)
      ? '<div class="probe-line warn" id="probe"><span class="dot"></span>Reachable, but '+
          esc(UNCOMPOSED_HINT)+' - this source holds no data until you do.</div>'
      : node.status==="connected"
      ? '<div class="probe-line ok" id="probe"><span class="dot"></span>Connected · live probe ok</div>'
      : node.status==="planned"
      ? '<div class="probe-line err" id="probe"><span class="dot"></span>'+esc(node.reason||"provider lands in E4/E9")+'</div>'
      : '<div class="probe-line" id="probe"><span class="dot"></span>Not connected yet</div>';
    p.innerHTML=
      '<div class="pcard" style="--k:'+kindColor(node.kind)+'">'+
        '<div class="phead"><span class="mono-chip">'+def.mono+'</span>'+
          '<div><b>'+esc(node.id)+'</b><div class="pk">'+esc(node.kind)+' · '+def.cap+'</div></div></div>'+
        '<div class="field"><label>store id</label><input data-fld="id" value="'+esc(node.id)+'" spellcheck="false"></div>'+
        '<div class="field"><label>business unit</label><input data-fld="bu" value="'+esc(node.bu)+'" placeholder="hr / sales / finance" spellcheck="false"></div>'+
        // "ACL" is our word, not the operator's. Name the OUTCOME (who can see this), and
        // label the two controls distinctly — one picks from the org directory, the other
        // takes a raw id — so it is obvious which you are using and what it costs you.
        '<div class="field"><label>Who can see this store</label>'+
          // NB: these label the two CONTROLS, deliberately not access levels — the access
          // level is whatever the picked group means, and naming a control after a tier
          // would collide with the tier names in the list below it.
          '<label class="sublabel">Pick from your directory</label>'+
          aclPickerHtml(node)+
          '<label class="sublabel">Or paste an ID '+
            '<span class="hint">advanced</span></label>'+
          '<input data-fld="acl" value="'+esc(node.acl.join(", "))+'" placeholder="paste a group or user ID" spellcheck="false">'+
          aclNamesHtml(node)+'</div>'+
        '<div class="divider"></div>'+
        '<div class="eyebrow" style="margin-bottom:2px">Connection · '+esc(def.label)+'</div>'+
        fields+ probe+ storeDocsHtml(node)+
        '<div class="prow">'+
          '<button class="btn primary" id="testConn">'+(node.status==="connected"?"Re-test":"Test connection")+'</button>'+
          '<button class="btn danger" id="delNode" style="flex:0 0 auto">Remove</button>'+
        '</div>'+
      '</div>';

    // #939: the store file-list filter, same threshold and same behaviour as the uploads
    // panel's. Re-rendering the panel would steal focus mid-word, so this filters the list
    // in place and leaves the input alone.
    const sdf=p.querySelector(".storedoc-filter");
    if(sdf) sdf.oninput=()=>{
      node.docFilter=sdf.value;
      const list=p.querySelector("#storeDocs");
      if(!list) return;
      const f=sdf.value.trim().toLowerCase();
      list.querySelectorAll(".updoc-row").forEach(r=>{
        const t=(r.querySelector(".updoc-title")||{}).textContent||r.textContent||"";
        r.style.display=(!f||t.toLowerCase().includes(f))?"":"none";
      });
    };

    // #258: picking a named tier appends its OID — the oid is still what lands in the ACL and
    // what LAW 2 compares; the name only makes it verifiable by a human.
    const pick=p.querySelector("[data-aclpick]");
    if(pick) pick.onchange=()=>{
      const oid=pick.value; if(!oid) return;
      if(!node.acl.includes(oid)) node.acl.push(oid);
      renderPanel(); refreshNodeCard(node); drawEdges(); renderStatus(); syncYaml(); saveCanvas();
    };
    p.querySelectorAll("[data-fld]").forEach(inp=>{
      inp.addEventListener("input",()=>{
        const f=inp.dataset.fld;
        if(f==="id") node.id=inp.value.trim()||node.id;
        else if(f==="bu"){ node.bu=inp.value.trim(); }
        else if(f==="acl"){ node.acl=inp.value.split(",").map(s=>s.trim()).filter(Boolean);
                            refreshAclNames(inp,node); }
        // live-update node card + edges without stealing focus
        refreshNodeCard(node); drawEdges(); renderStatus(); syncYaml();
        saveCanvas();     // #199: these edits bypass renderAll() (focus), so persist here too
      });
    });
    // Config fields that change the NODE's own chrome, not just its stored value. Editing one
    // has to re-render the node or the canvas silently disagrees with the panel: #429's
    // "Approve database access" button is gated on require_signin, and without this the button
    // only appeared after a Compose-up AND a reload — three steps to see a control that is
    // supposed to be the one obvious next thing to do.
    const CHROME_FIELDS=["require_signin"];
    p.querySelectorAll("[data-cfg]").forEach(inp=>{
      inp.addEventListener("input",()=>{
        node.config[inp.dataset.cfg]=inp.value; syncYaml();
        saveCanvas();     // #199: the connection fields are the work most painful to lose
      });
      // On `change` (blur/commit), not on every keystroke: renderAll() rebuilds the DOM and
      // would steal focus mid-word.
      if(CHROME_FIELDS.indexOf(inp.dataset.cfg)>=0)
        inp.addEventListener("change",()=>{ renderAll(); });
    });
    // #417 credential controls (ADR 0010 s3). The plaintext makes exactly one trip: input
    // element -> POST /secrets -> gone. Only the returned handle enters node.config.
    p.querySelectorAll("[data-sechint]").forEach(inp=>{
      api("/secrets/"+inp.dataset.sechint).then(d=>{
        if(d && d.exists && d.readable===false)
          // #832: the blob is THERE but no configured key decrypts it (rotation went
          // wrong, or _OLD was dropped too early). Distinct from missing on purpose:
          // "missing" means store it again; this means the KEY needs fixing first, and a
          // silent "is set" here is exactly how a rotation hides its own damage.
          inp.value="stored credential unreadable (key changed?) - set it again";
        else if(d && d.exists && d.hint) inp.value=inp.value+" (····"+d.hint+")";
        else if(d && d.exists===false)
          // The manifest holds a handle whose secret is GONE (key rotated, store wiped).
          // Say so - a silent "is set" here means an unexplainable compose failure later.
          inp.value="stored credential missing — set it again";
      }).catch(()=>{});   // 503 key-unset / offline: leave the plain "is set" label
    });
    function commitSecret(field,inp,btn){
      const raw=inp.value; if(!raw){ inp.focus(); return; }
      btn.disabled=true; btn.textContent="…";
      api("/secrets",{method:"POST",body:JSON.stringify({store_id:node.id,field:field,value:raw})})
        .then(d=>{ inp.value=""; node.config[field]=d.handle;
                   if(node._replacing) delete node._replacing[field];
                   syncYaml(); saveCanvas(); renderPanel(); })
        .catch(err=>{ btn.disabled=false; btn.textContent="Set";
                      toast(String(err.message||err)); });
    }
    p.querySelectorAll("[data-secsave]").forEach(btn=>{
      const inp=p.querySelector('[data-secretfield="'+btn.dataset.secsave+'"]');
      btn.onclick=()=>commitSecret(btn.dataset.secsave,inp,btn);
      inp.addEventListener("keydown",e=>{
        if(e.key==="Enter") commitSecret(btn.dataset.secsave,inp,btn);
        // Escape abandons a replace-in-progress; the stored handle was never touched.
        if(e.key==="Escape"&&node._replacing&&node._replacing[btn.dataset.secsave]){
          delete node._replacing[btn.dataset.secsave]; renderPanel();
        }
      });
    });
    p.querySelectorAll("[data-secreplace]").forEach(btn=>{
      btn.onclick=()=>{
        // Replace, never edit-in-place: values are not readable back (ADR 0010 s3), so the
        // only edit is a fresh value through the same one-way door. Config keeps the OLD
        // handle until the new Set succeeds - abandoning (Escape) leaves the store working.
        (node._replacing=node._replacing||{})[btn.dataset.secreplace]=true;
        renderPanel();
        const inp=p.querySelector('[data-secretfield="'+btn.dataset.secreplace+'"]');
        if(inp) inp.focus();
      };
    });
    p.querySelector("#testConn").onclick=()=>testConn(node);
    p.querySelector("#delNode").onclick=()=>{ removeNode(node); };   // #731: one delete path
  }

  function refreshNodeCard(node){
    const el=world.querySelector('.node[data-uid="'+node.uid+'"]');
    if(!el) return;
    const old=el.style.left, oldt=el.style.top;
    el.remove(); buildNode(node);
    const ne=world.querySelector('.node[data-uid="'+node.uid+'"]');
    if(ne){ ne.style.left=old; ne.style.top=oldt; }
  }

  function renderVerdict(v){
    const cls = v.status==="healthy" ? "ok" : (v.status==="failed" ? "err" : "warn");
    const stages=(v.stages||[]).map(s=>
      '<div class="hstage '+(s.ok?"ok":"bad")+'">'+(s.ok?'✓':'✗')+' '+esc(s.name)+' · '+s.ms+
      'ms<span class="hdetail">'+esc(s.detail||"")+'</span></div>').join("");
    const rem = v.remediation ? '<div class="hrem">'+esc(v.remediation)+'</div>' : "";
    return {cls, html:'<span class="dot"></span>'+esc(v.summary||v.status)+
                      '<div class="hstages">'+stages+rem+'</div>'};
  }

  function testConn(node){
    // #130 Phase G: a graded ROUND-TRIP health check, not just reachability.
    const probe=document.getElementById("probe");
    if(probe){ probe.className="probe-line busy"; probe.innerHTML='<span class="dot"></span>Health-checking '+esc(node.kind)+' via /router/health…'; }
    node.status="draft";
    const el=world.querySelector('.node[data-uid="'+node.uid+'"] .status');
    if(el) el.className="status draft";
    drawEdges();
    api("/router/health",{method:"POST",body:JSON.stringify({entry:entryOf(node)})})
      .then(v=>{
        // healthy|degraded are both reachable -> connected; failed -> planned
        if(v.status==="failed"){ node.status="planned"; node.reason=v.remediation||v.summary; }
        else { node.status="connected"; node.reason=(v.status==="degraded")?v.summary:""; }
        renderPanel(); renderAll();
        const p2=document.getElementById("probe");
        if(p2){ const rv=renderVerdict(v); p2.className="probe-line "+rv.cls; p2.innerHTML=rv.html; }
        // #941: a reachable source is the moment the user's intent stops being ambiguous, so
        // compose here rather than leaving them to find a button called "Compose up". This is
        // the half that makes the draft state RARE; the draft state is the half that keeps
        // this honest when compose cannot run (a refused store comes back `planned` with its
        // reason, which is the existing path and is not swallowed).
        //
        // Only on a reachable verdict: composing a store the probe just refused would submit
        // a crawl we already know fails, and bury the remediation under a compose error.
        if(v.status!=="failed") composeUp();
      })
      .catch(e=>{
        node.status="draft"; renderAll();
        const p2=document.getElementById("probe");
        if(p2){ p2.className="probe-line err"; p2.innerHTML='<span class="dot"></span>health check failed: '+esc(e.message||e); }
      });
  }

  /* ---------------- status bar ---------------- */
  function renderStatus(){
    if(!alive) return;
    const bar=document.getElementById("statusbar");
    const bus=new Set(state.map(s=>s.bu).filter(Boolean));
    // #941: "connected" in this bar has always meant "askable", and an uncomposed store is
    // not. On prod it read "1 connected" beside an answer saying nothing was connected.
    const conn=state.filter(s=>s.status==="connected"&&!isUncomposed(s)).length;
    const draftIds=state.filter(s=>isUncomposed(s)).map(s=>s.id);
    // #917: action-kind nodes (Your documents) carry per-DOCUMENT audiences, not a node
    // ACL - counting them as "without ACL" would warn about a node that cannot have one.
    const noacl=state.filter(s=>s.acl.length===0 && !s.derived).length;
    // #356: in demo mode the askable set is the pre-composed sample catalog, NOT what the
    // visitor has dragged onto the canvas. Reporting a bare "0 sources / 0 connected" on
    // arrival contradicted the six sample stores that were already answering questions.
    const demoPool=(isDemoMode()&&DEMO_POOL.length)?DEMO_POOL.length:0;
    // #448: this used to print `tenant acme` unconditionally - the SAMPLE tenant's name -
    // even for a signed-in owner whose header says YOUR DATABASES. We do not actually know
    // the customer's organisation name (the session carries a tid, not a label), and an
    // invented one is worse than none, so say the true thing instead: this is your own
    // workspace. The sample fleet keeps its real name, which is the useful fact there.
    const scope=authState.signed_in
      ? '<span><span class="s-dot"></span>your workspace</span>'
      : '<span><span class="s-dot"></span>tenant <b>'+esc(state.tenant||"acme-demo")+'</b></span>';
    // #781: "5 sources · 3 connected" named neither the two failures nor their cause. Any
    // store compose/test marked planned WITH a reason is a known failure - name it here, so
    // the bar points at the red node instead of leaving the owner to hunt for it. Gated on
    // s.reason like the node card: demo "planned" nodes have nothing to explain. The visible
    // segment carries the ids (capped at three); the title carries every id with its full
    // reason. esc() on both - the reason is server text, and this is one attribute sink and
    // one content sink (#786).
    const failed=state.filter(s=>s.status==="planned"&&s.reason);
    const failedIds=failed.map(n=>n.id);
    const failedSeg=failed.length
      ? '<span style="color:var(--err)" title="'+
          esc(failed.map(n=>n.id+": "+n.reason).join("\n"))+'">✗ '+failed.length+
          ' not connected: '+esc(failedIds.slice(0,3).join(", "))+
          (failedIds.length>3?" +"+(failedIds.length-3)+" more":"")+'</span>'
      : '';
    bar.innerHTML=scope+
      '<span><b>'+(demoPool||state.length)+'</b> source'+((demoPool||state.length)===1?"":"s")+
      (demoPool?' <span style="color:var(--faint)">(sample)</span>':'')+'</span>'+
      '<span><b>'+bus.size+'</b> business unit'+(bus.size===1?"":"s")+'</span>'+
      '<span><b>'+(demoPool?demoPool:conn)+'</b> connected</span>'+
      // #941: named, not just subtracted. A count that silently drops one is how the user
      // ends up hunting; this points at the node AND at the button.
      (draftIds.length&&!demoPool
        ? '<span style="color:var(--warn)" title="'+esc(draftIds.join(", ")+" - "+UNCOMPOSED_HINT)+
            '">⚠ '+draftIds.length+' draft: press Compose up</span>'
        : '')+
      failedSeg+
      (noacl?'<span style="color:var(--warn)">⚠ '+noacl+' without ACL</span>':'<span style="color:var(--ok)">✓ all permissioned</span>')+
      '<span class="hint">drag to arrange · click a node to configure</span>';
  }

  /* ---------------- delegated auth (#156/#188/#189/#190) ---------------- */
  // require_signin -> entra_refresh delegation. Azure SQL + Synapse present the token to the
  // SQL audience (database.windows.net, the server-side default, so `resource` is omitted);
  // Postgres + MySQL use the Azure OSSRDBMS audience. The broker redeems the vaulted refresh
  // token per (user, resource) — see exchange_from_config.
  const _DELEG_RESOURCE={postgres:"https://ossrdbms-aad.database.windows.net",
                         mysql:"https://ossrdbms-aad.database.windows.net",
                         cosmos_db:"https://cosmos.azure.com"};
  // GCP kinds delegate through Google (google_refresh, #193): the vaulted Google refresh
  // token is redeemed for an access token and the query runs as the user, with BigQuery IAM
  // + row-access policies enforcing. Azure kinds keep entra_refresh. Wiring a GCP store with
  // an Entra delegation would redeem a Microsoft token against Google - fail-closed forever.
  const _GCP_KINDS=new Set(["bigquery"]);
  // AWS kinds delegate through the caller's own vaulted access keys (aws_keys, ADR 0024):
  // the broker redeems them via STS GetSessionToken and the query runs as the caller's own
  // IAM principal. The block carries NO credential fields - the keys are per-account in the
  // vault (account menu -> Amazon -> Connect), never in a manifest.
  const _AWS_KINDS=new Set(["redshift","s3","rds_postgres","rds_mysql"]);
  // `resource` is only the aws_keys exchange's cache key (one STS session serves every
  // AWS service), so the AWS kinds legitimately share a credential without sharing a store.
  const _AWS_RESOURCE={s3:"s3",redshift:"redshift",rds_postgres:"rds",rds_mysql:"rds"};
  // #673: kinds whose identity is not a user choice - see entryOf.
  // #809: redshift joined s3 here. A hosted box has no ambient AWS identity, so an
  // undelegated redshift entry composed to "Unable to locate credentials" every time - the
  // caller's vaulted keys (ADR 0024) are the only identity that exists. Dev rigs are safe:
  // a fake-oid mint falls back to ambient credentials (provisioning.py).
  // #814 (ADR 0026): the RDS kinds joined too - their IAM auth token is minted
  // server-side from the same vaulted keys, so nobody types a database password here.
  const _ALWAYS_DELEGATED=new Set(["s3","redshift","rds_postgres","rds_mysql"]);
  function delegationFor(kind){
    if(_AWS_KINDS.has(kind)){
      return {kind:"aws_keys",resource:_AWS_RESOURCE[kind]};
    }
    if(_GCP_KINDS.has(kind)){
      return {kind:"google_refresh",client_id:"${GOOGLE_CLIENT_ID}",
              client_secret:"${GOOGLE_CLIENT_SECRET}",resource:"bigquery"};
    }
    const d={kind:"entra_refresh",tenant_id:"${AUTH_TENANT_ID}",
             client_id:"${AUTH_CLIENT_ID}",client_secret:"${AUTH_CLIENT_SECRET}"};
    if(_DELEG_RESOURCE[kind]) d.resource=_DELEG_RESOURCE[kind];
    return d;
  }

  /* ---------------- yaml export ---------------- */
  function manifest(){
    const stores=state.map(n=>{
      const cfg=Object.entries(n.config).filter(([k,v])=>v!==""&&k!=="require_signin")
        .map(([k,v])=>k+": "+(typeof v==="string"?v:JSON.stringify(v).slice(0,48)+"…")).join(", ");
      const signin=String(n.config.require_signin||"").trim().toLowerCase().startsWith("y");
      // Same rule as entryOf - INCLUDING _ALWAYS_DELEGATED (#809): before that fix this
      // preview was signin-only, so it showed `delegation: null` for an s3 store that
      // composes WITH one. The YAML the user reads must be the YAML that will be composed.
      return {id:n.id,kind:n.kind,bu:n.bu||"unassigned",acl:n.acl,cfg:cfg,
              delegation:(signin||_ALWAYS_DELEGATED.has(n.kind))?(n.delegation||delegationFor(n.kind)):null};
    });
    return {tenant:state.tenant||"acme",stores};
  }

  /* ---------------- live wiring (#109) ---------------- */
  function currentUser(){ const s=document.getElementById("quser"); return (s&&s.value)||"alice"; }
  // #279 (ADR 0009): three modes, selected by auth state.
  //   dev  — no real login configured: the alice/bob picker is the dev switcher (X-DBSearch-User).
  //   demo — real login configured but NOT signed in: the picker acts as a DEMO principal
  //          (X-DBSearch-Demo-User -> demo:alice/bob over the local demo fleet). This is the
  //          before-login product; the dev header would be refused here (#183).
  //   live — real login configured AND signed in: identity is the session cookie; send no header.
  function realLoginConfigured(){ return !!(authState.enabled || authState.google_enabled); }
  function isDemoMode(){ return realLoginConfigured() && !authState.signed_in; }
  // #417 (ADR 0010 s3): the credential panel is for the signed-in self-serve user. Dev rigs
  // keep the ${ENV} input - their operator IS the credential owner (form 2 stays legal).
  function selfServeSecrets(){ return realLoginConfigured() && !!authState.signed_in; }
  function isEnvRef(v){ return /^\$\{[A-Z0-9_]+\}$/.test(String(v||"")); }
  function isSecretHandle(v){ return String(v||"").startsWith("secret://"); }
  // How THIS page proves who it is. Extracted so the multipart upload (#561) identifies the
  // caller exactly as every JSON call does - a second, hand-rolled copy is how one of them
  // ends up authenticating differently from the rest.
  function idHeaders(){
    if(isDemoMode())               return {"X-DBSearch-Demo-User":currentUser()};
    if(realLoginConfigured())      return {};   // live: the verified session cookie is the identity
    return {"X-DBSearch-User":currentUser()};   // dev switcher
  }
  // #561: multipart, so Content-Type is DELIBERATELY absent - the browser must set it itself
  // to include the multipart boundary. Setting it by hand produces a body FastAPI cannot parse.
  function uploadDoc(file,title,audience){
    const fd=new FormData();
    fd.append("file",file);
    if(title) fd.append("title",title);
    if(audience) fd.append("audience",audience);
    if(!alive) return ABANDONED;
    return fetch("/admin/upload",{method:"POST",headers:idHeaders(),body:fd}).then(r=>{
      if(!r.ok) return r.json().catch(()=>({detail:r.status})).then(b=>{
        throw new Error(b.detail||("HTTP "+r.status)); });
      return r.json();
    });
  }
  /** #643: abandon the chain if the surface has gone.
   *
   * THE ROOT of a class of bug, rather than another of its symptoms. Three separate crashes
   * were reported from prod after the teardown was already in - `positionHub -> null.style`,
   * `renderPanel -> null.innerHTML`, `composeUp -> null.textContent` - and all three were the
   * same thing: a continuation of a request issued before the user left, running against a
   * DOM that no longer exists. Guarding the three functions one at a time was fixing the
   * places the cascade happened to surface, and the cascade is long (bootCanvas -> restore ->
   * composeUp + syncSharePointNodes + loadPrincipals, each with their own .then).
   *
   * A promise that never settles, not a rejection: there IS no answer to give a surface that
   * has been unmounted, and rejecting would only move the problem into every .catch on the
   * chain, several of which recover by drawing something.
   *
   * The guards on renderAll and friends stay as defence in depth - this stops the chain at
   * its source, they stop anything that reaches the DOM by another route. */
  const ABANDONED = new Promise(() => {});

  function api(path,opts){
    if(!alive) return ABANDONED;
    opts=opts||{};
    opts.headers=Object.assign({"Content-Type":"application/json"}, idHeaders(), opts.headers||{});
    return fetch(path,opts).then(r=>{
      if(!r.ok) return r.json().catch(()=>({detail:r.status})).then(b=>{
        throw new Error(b.detail||("HTTP "+r.status)); });
      return r.json();
    });
  }
  function entryOf(node){
    const cfg=Object.assign({},node.config||{});
    const signin=String(cfg.require_signin||"").trim().toLowerCase().startsWith("y");
    delete cfg.require_signin;
    // `tables` is an ALLOWLIST the SQL providers already understand (azure_sql.py etc read
    // config["tables"]) — it scopes the store to the tables that matter. The panel takes it as
    // a comma-separated string; the providers want a list. Unscoped, a real DB leaves the
    // health canary and the SQL generator free to pick ANY table — on AdventureWorksLT that
    // meant COUNT(*)ing an empty dbo.ErrorLog and reporting a healthy DB as degraded (#202).
    if(typeof cfg.tables==="string"){
      const t=cfg.tables.split(",").map(s=>s.trim()).filter(Boolean);
      if(t.length) cfg.tables=t; else delete cfg.tables;
    }
    const e={id:node.id,kind:node.kind,business_unit:node.bu||"unassigned",
             acl:node.acl,title:node.id,
             description:cfg.description||"",config:cfg};
    // The node's OWN delegation block wins over the canvas default. A manifest restored from
    // the server (nodeFromEntry) may legitimately carry entra_obo, gcp_wif, an explicit
    // `resource`, or a secret:// client_secret that delegationFor(kind) knows nothing about;
    // re-deriving it here would silently rewrite the user's identity wiring on every reload.
    // #673: some kinds have no CHOICE about identity. An S3 store reads the CALLER's own
    // bucket with the CALLER's own vaulted keys - there is no server identity on a hosted box
    // that could stand in - so it carries no require_signin switch and its delegation is not
    // conditional on one. Offering the switch would imply a working "run as the server" mode
    // that does not exist, which is the hollow-offer shape (#654/#656/#660).
    if(signin||_ALWAYS_DELEGATED.has(node.kind)) e.delegation=node.delegation||delegationFor(node.kind);
    // `mode` is not a palette field, so nothing on the canvas can author it - but a manifest
    // from the setup agent can, and since #368 the canvas writes what it holds back into the
    // system of record. Dropping it here would durably downgrade e.g. a zero-copy native
    // store into an indexed copy.
    if(node.mode) e.mode=node.mode;
    return e;
  }
  // The documented INVERSE of entryOf(): a canvas node rebuilt from a stored manifest entry,
  // carrying back everything entryOf moved OUT of `config`. #368 makes this load-bearing -
  // loadLiveUser restores from the server's manifest and then calls composeUp(), which POSTs
  // the result to /router/compose and _persisting_compose OVERWRITES the stored row with it.
  // Anything this mapper drops is therefore destroyed in the system of record after one page
  // reload, and survives restarts and localStorage clearing.
  //
  // The one that bit us (CRITICAL 2): entryOf DELETES config.require_signin and emits a
  // sibling `delegation:` block. Reading only id/kind/business_unit/acl/config/description
  // lost OBO entirely - a user who had turned on "queries run as the signed-in user" silently
  // reverted to the service credential, so the database's row-level security stopped applying
  // and the store returned every row the service principal can see, while the panel rendered
  // require_signin blank so the UI agreed with the wrong state (#156 regression, LAW 2).
  function nodeFromEntry(s,x,y){
    const cfg=Object.assign({description:s.description||""},s.config||{});
    if(s.delegation) cfg.require_signin="yes";   // the exact inverse of entryOf's `delete`
    // s.kind verbatim: NEVER KINDS[s.kind]?s.kind:"local" - see kindDef().
    const n=mk(s.id,s.kind||"",s.business_unit||"",s.acl||[],cfg,"draft",x,y);
    if(s.delegation) n.delegation=s.delegation;  // kept verbatim, not re-derived from kind
    if(s.mode) n.mode=s.mode;
    return n;
  }
  function liveManifest(){
    // entryOf's result verbatim. It used to be re-copied field by field here, which silently
    // dropped anything entryOf grew that the whitelist did not know about - and since #368
    // this manifest IS the system of record (_persisting_compose writes it to Postgres), so a
    // dropped field is a durable loss, not a display glitch. One shape, one place.
    // #818: `layout` rides beside `stores` - a MOVE is a mutation too, and the row is where
    // mutations live now. It is here (not only in rowManifest) so a compose - which also
    // overwrites the row - cannot silently drop the layout the autosave just wrote.
    // #917: a DERIVED node (n.derived - the uploads node) renders on the canvas but has
    // no provider behind it (the canvas.js:282 rule) - composing one would degrade the
    // whole manifest with "no provider for this kind". It is re-derived from server truth
    // on every load (syncDocumentsNode, the syncSharePointNodes pattern), so leaving it
    // out of stores loses nothing durable. Filtered on the node FLAG, not on KINDS: this
    // function is extracted standalone by the manifest-roundtrip selftest.
    return {tenant:state.tenant||"acme",
            stores:state.filter(n=>!n.derived).map(n=>entryOf(n)),
            layout:layoutOf()};
  }
  function layoutOf(){
    const l={}; state.forEach(n=>{ l[n.id]=[Math.round(n.x),Math.round(n.y)]; }); return l;
  }
  // ---- #941: "probed" and "composed" are two facts, and node.status carried both --------
  // `testConn` writes status="connected" when the ENDPOINT answers; `composeUp` writes the
  // same value when the store is in the CATALOG. On prod the owner re-added a Drive folder,
  // pressed Test connection, and watched the honest "draft" dot turn green on a store holding
  // nothing - the probe was right (the folder was reachable) and the conclusion was wrong.
  //
  // This set is the second fact, and it has exactly ONE writer per compose response
  // (composeUp below and the setup agent's handler) so the two cannot drift - the #799
  // lesson: count the rule's homes. It is runtime truth, never persisted: a reload composes,
  // and a set restored from storage would assert catalog membership nobody re-checked.
  const composedIds = new Set();
  function noteComposed(stores){
    composedIds.clear();
    (stores||[]).forEach(s=>{ if(s && s.store_id) composedIds.add(s.store_id); });
  }
  // A node that LOOKS connected and is not in the catalog. Derived nodes (#917 uploads) are
  // never composed and must not be accused; a node that is planned or draft already says so.
  function isUncomposed(node){
    return !node.derived && node.status==="connected" && !composedIds.has(node.id);
  }
  const UNCOMPOSED_HINT="not composed yet - press Compose up";

  // ---- #939 / #895: what a store actually HOLDS, as of now ------------------------------
  // The gate asks a connected node to show "synced + doc count". It could show neither: the
  // node's freshness is a snapshot taken at COMPOSE, and a crawl finishes afterwards - so on
  // prod the catalog read `ingested@08:58:31` while the badge still read `syncing`, forever.
  // GET /router/stores/{id}/documents answers both from the LIVE descriptor, permission
  // trimmed (see QueryService.document_inventory - the listing it is built from is an admin
  // surface and would otherwise publish filenames the caller cannot read).
  function loadStoreDocs(node){
    if(!node || node.derived || !node.id) return Promise.resolve(null);
    return api("/router/stores/"+encodeURIComponent(node.id)+"/documents")
      .then(r=>{
        node.docsKnown = !!(r && r.known);
        node.docCount  = (r && typeof r.doc_count === "number") ? r.doc_count : null;
        node.docFresh  = (r && r.freshness) || "";
        node.docs      = (r && r.documents) || [];
        node.unreadable= (r && r.unreadable) || 0;
        return r;
      })
      // A store that cannot answer right now is UNKNOWN, never empty (#392). Leaving the
      // previous values alone is deliberate: overwriting them with nulls on one failed poll
      // would blank a count the user is looking at.
      .catch(()=>{ if(node.docsKnown===undefined) node.docsKnown=false; return null; });
  }
  // Refresh every composed store, then paint. Used after a compose and while a crawl runs.
  function loadAllStoreDocs(){
    const live=state.filter(n=>!n.derived && composedIds.has(n.id));
    if(!live.length) return Promise.resolve();
    return Promise.all(live.map(loadStoreDocs)).then(()=>{ if(alive){ renderAll(); renderPanel(); } });
  }
  // #939: poll only while something is SYNCING, and stop. A crawl is the one state that
  // changes without the user doing anything, and it is also the state the old code froze in.
  let _docPollT=null;
  function pollStoreDocsWhileSyncing(tries){
    if(_docPollT){ clearTimeout(_docPollT); _docPollT=null; }
    const n=(tries===undefined)?12:tries;
    if(n<=0 || !alive) return;
    const syncing=state.some(x=>!x.derived && composedIds.has(x.id)
                               && /^syncing/.test(x.docFresh||x.freshness||""));
    if(!syncing) return;
    _docPollT=setTimeout(()=>{ _docPollT=null;
      loadAllStoreDocs().then(()=>pollStoreDocsWhileSyncing(n-1)); }, 4000);
  }
  // The freshness/count pill. Prefers the LIVE answer over the compose snapshot, and shows a
  // COUNT only once the crawl has settled: mid-crawl the count is 0 and will not be 0 in a
  // minute, so printing "0 docs" there states a moving number as the answer.
  function freshnessPill(node){
    const live=node.docFresh || node.freshness || "";
    const syncing=/^syncing/.test(live);
    if(node.docsKnown && !syncing && typeof node.docCount==="number"){
      return '<span class="pill" title="'+esc(live)+'">'+node.docCount+
             ' doc'+(node.docCount===1?'':'s')+'</span>';
    }
    return live ? '<span class="pill" title="freshness">'+esc(live)+'</span>' : '';
  }
  // The panel's file list - the owner's actual question, "did DBSNotes.txt land?", answerable
  // by reading rather than by asking a question and inspecting citations.
  function storeDocsHtml(node){
    if(!node || node.derived) return '';
    if(!node.docsKnown) return '';        // unknown says nothing at all (#392)
    const all=node.docs||[];
    const f=(node.docFilter||"").trim().toLowerCase();
    const shown=f?all.filter(d=>String(d.title||d.doc).toLowerCase().includes(f)):all;
    // The SAME shell the uploads panel uses (renderDocsPanel) - .updoc-list / .updoc-row /
    // .updoc-title / .pill / .btn - so the two document lists are one design rather than two
    // that resemble each other. Owner's ask 260823: "similar font and UI".
    const rows=shown.map(d=>{
      const t=(d.title||d.doc||"").trim()||d.doc;
      // The row's action is OPEN, not Delete, and that is a decision rather than an omission:
      // DELETE /documents/{id} reaches the EDITION's index by owner_oid, and a connector
      // document lives in its store's own index - so the button would 404. Worse, a delete
      // that did land would be undone by the next crawl, which is the #731/#941 family
      // (a gesture that looks like it worked and silently does not stick). See #943.
      const open=d.uri
        ? '<a class="btn" href="'+esc(d.uri)+'" target="_blank" rel="noopener" '+
          'title="Open this file at the source">Open</a>'
        : '';
      return '<div class="updoc-row" style="border-bottom:1px solid '+
        'color-mix(in srgb,var(--faint) 25%,transparent);padding:7px 0">'+
        '<div style="display:flex;align-items:center;gap:8px;min-width:0">'+
        '<span class="updoc-title docrow" style="flex:1;min-width:0;overflow:hidden;'+
          'text-overflow:ellipsis;white-space:nowrap" title="'+esc(d.uri||t)+'">'+esc(t)+'</span>'+
        '<span class="pill">'+esc(kindDef(node.kind).cap||"document")+'</span>'+
        open+'</div></div>';
    }).join("");
    // #725: files the crawl LISTED and could not fetch. A list that silently omits them is a
    // new way to mislead - the file is in the folder, absent here, and nothing says why.
    const unread=node.unreadable
      ? '<div class="updoc-row docrow warn" style="padding:7px 0;color:var(--warn)">⚠ '+
          node.unreadable+" couldn't read (a per-file permission, or an export cap)</div>"
      : '';
    const n=(typeof node.docCount==="number")?node.docCount:all.length;
    const when=(node.docFresh||"").startsWith("ingested@")
      ? ' · synced '+esc(node.docFresh.slice("ingested@".length,"ingested@".length+16).replace("T"," "))
      : '';
    // The filter appears at the same threshold the uploads panel uses, for the same reason.
    const filter=(all.length>8)
      ? '<div class="up-row" style="margin:8px 0"><input type="text" class="storedoc-filter" '+
        'placeholder="Filter '+all.length+' files…" value="'+esc(node.docFilter||"")+'" '+
        'style="flex:1"></div>'
      : '';
    return '<div class="divider"></div>'+
      '<div class="eyebrow" style="margin-bottom:2px">Documents</div>'+
      '<div class="pk" style="margin-bottom:4px">'+n+' file'+(n===1?'':'s')+esc(when)+'</div>'+
      filter+
      '<div id="storeDocs" class="updoc-list">'+
        (rows||'<div class="docrow muted" style="padding:8px 0">'+
          (f?"Nothing matches that filter.":"Nothing indexed yet.")+'</div>')+
        unread+'</div>';
  }

  function composeUp(){
    if(!alive) return;                   // #643: entered synchronously from a boot continuation
    const btn=document.getElementById("compose");
    btn.textContent="Composing…";
    api("/router/compose",{method:"POST",body:JSON.stringify({manifest:liveManifest()})})
      .then(r=>{
        const byId={}; r.stores.forEach(s=>{byId[s.store_id]=s;});
        const skip={}; (r.skipped||[]).forEach(s=>{skip[s.id]=s.reason;});
        noteComposed(r.stores);   // #941: what the CATALOG holds, as distinct from what probed
        state.forEach(n=>{
          // #810: n.reason is per-node state that OUTLIVES a compose (the node object is
          // reused), and the dot tooltip renders status+reason - so a success must clear
          // the failure it fixed or the tooltip reads "connected: build/probe failed...".
          // (testConn's connected-degraded reason is a different, deliberate state.)
          // #808: warnings are per-compose too, and follow the SAME #810 rule as reason -
          // a store the owner just fixed must not keep last compose's warning, so both
          // branches assign rather than merge.
          if(byId[n.id]){ n.status="connected"; n.freshness=byId[n.id].freshness||""; n.reason="";
                          n.warnings=byId[n.id].warnings||[]; }
          else if(skip[n.id]!==undefined){ n.status="planned"; n.reason=skip[n.id]; n.warnings=[]; }
        });
        state.composed=true;
        markRowClean();   // #818: this compose just persisted the row - no follow-up PUT
        renderAll();
        // #808: a warned store is LIVE, so it is counted in "live" and called out separately -
        // never folded into "planned", which means refused-and-never-built.
        const warned=state.filter(n=>n.warnings&&n.warnings.length).length;
        // #939: the compose response's freshness is already stale for any store whose crawl
        // is still queued, so ask the live endpoint rather than painting the snapshot.
        loadAllStoreDocs().then(()=>pollStoreDocsWhileSyncing());
        btn.textContent="Composed ✓ "+r.stores.length+" live"+
          (warned?" · "+warned+" needs attention":"")+
          ((r.skipped||[]).length?" · "+r.skipped.length+" planned":"");
        setTimeout(restoreComposeBtn,2600);
      })
      // #804: the failure is SHOWN (the detail is real information) and then RELEASED -
      // this branch used to rename the button permanently, so one failed compose left
      // "Compose failed: ..." where the retry affordance belongs until a full reload.
      // Longer than the success restore on purpose: an error deserves reading time.
      .catch(e=>{ btn.textContent="Compose failed: "+(e.message||e);
                  setTimeout(restoreComposeBtn,4000); });
  }
  function restoreComposeBtn(){
    const btn=document.getElementById("compose");
    if(btn) btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 19V5M6 11l6-6 6 6"/></svg>Compose up';
  }
  // ---- canvas persistence (#199) --------------------------------------------------------
  // The canvas used to be rebuilt from /router/demo on every load, so ANY navigation threw
  // the user's work away. The cruel case: configure a source, hit "Test connection", get told
  // "sign in required" (correct, #183), obey it — and the OAuth redirect back to /canvas wipes
  // the node you just built. The flow require_signin forces you into destroyed your work.
  // Passwords/secrets are never in here (LAW 1): secret-typed fields legally hold only an
  // ${ENV} ref (resolved server-side) or a secret:// handle (#417, resolved through
  // SecretsPort). saveCanvas STRIPS anything else from them before persisting - a literal
  // typed into a dev rig's ENV input must not outlive the page in browser storage.
  const SAVE_KEY="dbsearch.canvas.v1";
  // #295: boot paints ONCE (line ~1921 renderAll) before the async refreshAuth().then(bootCanvas)
  // has resolved and chosen a mode. That first paint runs with the empty initial state=[], and
  // renderAll persists on every call — so it used to overwrite the saved canvas with [] BEFORE
  // restoreCanvas could read it, wiping the user's connected stores on every reload (and, with no
  // nodes to compose, leaving "no catalog composed yet"). The state is not authoritative until a
  // bootCanvas loader has run, so persistence is suppressed until then.
  let booting=true;
  function saveCanvas(){
    if(booting) return;              // #295: don't let the pre-auth boot paint clobber the save
    try{
      localStorage.setItem(SAVE_KEY, JSON.stringify({
        tenant: state.tenant||"acme",
        // n.delegation / n.mode ride along so the localStorage fallback path
        // (loadLiveUserFromLocal) is as lossless as the server restore: without them a
        // reload with the manifest store unavailable would drop a custom delegation block
        // and then composeUp() would write the loss back. No secret is added by doing so -
        // a delegation block's credential fields are ${ENV} refs or secret:// handles by
        // construction, and the compose guard (secret_fields.py) refuses a literal.
        // #917: derived nodes are NOT saved - the field-mapper below would drop the
        // `derived` flag, and the restore would resurrect the uploads node as a real
        // store that then composes ("no provider for this kind") and pollutes the row.
        // It is re-derived from /admin/documents on every load instead.
        nodes: state.filter(n=>!n.derived).map(n=>({id:n.id,kind:n.kind,bu:n.bu,acl:n.acl,
                              config:scrubSecrets(n.kind,n.config),
                              delegation:n.delegation||null,mode:n.mode||"",
                              x:n.x,y:n.y})),
      }));
    }catch(e){}                       // storage disabled/full: degrade to the old behaviour
    rowSaveSoon();                    // #818: the server row mirrors every mutation too
  }
  // ---- #818: mirror mutations into the SERVER row ---------------------------------------
  // The row is the system of record (#368) but was written only by compose, so an
  // added-but-never-composed node was durably lost on every reload: loadLiveUser rebuilds
  // exclusively from the row, and the remount's own saveCanvas destroyed the local copy.
  // The owner hit exactly this on prod (added postgres-1, hard refresh, gone). Every
  // mutation now also schedules a debounced, dirty-checked PUT /router/manifest - a
  // guarded row WRITE, no compose, drafts included. Cmd/Ctrl+S flushes it immediately.
  let lastRowSave=null, rowSaveT=null;
  // #951: has THIS mount actually learned what the row holds? state=[] is set synchronously at
  // wire-up and the boot flag clears the moment AUTH resolves, but the real stores arrive later
  // from GET /router/manifest - and unmountCanvas flushes a row save BEFORE alive drops (#818,
  // deliberately, so a just-added node survives navigating away). An unmount inside that window
  // therefore wrote `stores: []` over a full row, with keepalive, and destroyed the workspace.
  // Measured on prod 260824: the owner's gdrive + sharepoint_link nodes vanished while Admin
  // still listed their documents, because the warm catalog outlived the row.
  // Set ONLY where authoritative state has been established, and deliberately NOT on the read
  // FAILURE path: a row we could not read is a row we must not overwrite.
  let rowHydrated=false;
  function rowManifest(){
    // entryOf verbatim (the #368 one-shape rule), then the same secret scrub the local
    // save applies: a literal typed into a secret field never leaves the page - the row
    // legally holds only ${ENV} refs / secret:// handles, exactly like localStorage
    // (LAW 1; the server guard 400s a literal anyway, this just never sends one).
    const m=liveManifest();
    // By the ENTRY's own kind, not state[i]: liveManifest FILTERS derived nodes (#917),
    // so positional parallelism with `state` no longer holds - indexing state[i] here
    // scrubbed with the wrong node's kind for every store after a filtered one.
    m.stores=m.stores.map(e=>{ e=Object.assign({},e);
      e.config=scrubSecrets(e.kind, e.config||{}); return e; });
    return m;
  }
  function markRowClean(){ try{ lastRowSave=JSON.stringify(rowManifest()); }catch(e){} }
  function pushRowSave(){
    if(!isLiveUser()) return Promise.resolve(false);
    // #951: never persist state this mount never loaded. Checked HERE rather than at the call
    // sites for the same reason the isLiveUser gate lives here (#799): every path - debounce,
    // Cmd+S, unmount, pagehide - funnels through this one function, and a second copy would be
    // an equivalent-mutant home no guard could tell apart.
    if(!rowHydrated) return Promise.resolve(false);
    const m=rowManifest(); let snap;
    try{ snap=JSON.stringify(m); }catch(e){ return Promise.resolve(false); }
    if(snap===lastRowSave) return Promise.resolve(true);   // saved IS true - nothing changed
    // Raw fetch with keepalive (the #731 delete precedent), not api(): the save must
    // survive an unmount and the tab closing - which is the user's exact repro (add a
    // node, hard refresh). api() abandons on !alive and cannot ride out a navigation.
    return fetch("/router/manifest",{method:"PUT",keepalive:true,
        headers:Object.assign({"Content-Type":"application/json"},idHeaders()),
        body:JSON.stringify({manifest:m})})
      .then(r=>{
        if(!r.ok) return r.json().catch(()=>({detail:"HTTP "+r.status}))
          .then(b=>{ throw new Error(b.detail||("HTTP "+r.status)); });
        lastRowSave=snap; return true;
      })
      .catch(e=>{
        // Failure must be VISIBLE (an empty success hides an outage) - but only while
        // the surface is up; a teardown-time flush has nowhere to toast.
        if(alive) toast("Could not save workspace - "+(e.message||e));
        return false;
      });
  }
  function rowSaveSoon(){
    // No gate here on purpose: "never PUT for a non-live user" lives in ONE home,
    // pushRowSave (which every path - debounce, Cmd+S, unmount, pagehide - goes through).
    // A second copy would be an equivalent-mutant home no guard could discriminate (#799).
    if(rowSaveT) clearTimeout(rowSaveT);
    rowSaveT=setTimeout(()=>{ rowSaveT=null; pushRowSave(); },800);
  }
  function flushRowSave(){
    if(rowSaveT){ clearTimeout(rowSaveT); rowSaveT=null; }
    return pushRowSave();
  }
  // #417: the persisted copy of a secret-typed field may hold an ${ENV} ref or a secret://
  // handle - never a literal. The in-memory node is untouched (the dev-rig compose still
  // sends what was typed, and the server guard 400s a literal there with a pointer here).
  function scrubSecrets(kind,config){
    const def=KINDS[kind]; if(!def||!def.fields) return config;
    const out=Object.assign({},config);
    def.fields.forEach(f=>{
      if(!f.secret) return;
      const v=out[f.k];
      if(v && !isEnvRef(v) && !isSecretHandle(v)) delete out[f.k];
    });
    return out;
  }
  function restoreCanvas(){
    try{
      const raw=localStorage.getItem(SAVE_KEY); if(!raw) return false;
      const saved=JSON.parse(raw);
      // #731: an EMPTY nodes array is a valid save (the user deleted everything), not an
      // absent one - the old `.length` guard made a deliberately-emptied canvas
      // indistinguishable from no save at all.
      if(!saved || !Array.isArray(saved.nodes)) return false;
      seq=0; selected=null;
      state=saved.nodes.map(n=>{
        const node=mk(n.id,n.kind,n.bu||"",n.acl||[],n.config||{},"draft",n.x,n.y);
        if(n.delegation) node.delegation=n.delegation;
        if(n.mode) node.mode=n.mode;
        return node;
      });
      state.tenant=saved.tenant||"acme";
      return true;
    }catch(e){ return false; }
  }

  // #279 (ADR 0009 / B): the before-login demo. The demo catalog is PRE-COMPOSED server-side
  // (each "Azure" store is really a local fixture, LAW 7), so the canvas starts EMPTY and the
  // visitor "connects" sample databases from the rail - a real-feeling gesture that reveals a
  // store already backed locally. It never composes/loads-principals/touches SharePoint (all
  // live-only, 403 for a demo identity). Questions run against the FULL sample either way.
  let DEMO_POOL=[];                         // connectable sample stores (id/kind/title/bu/acl)
  function loadDemoFleet(){
    api("/router/demo").then(r=>{
      DEMO_POOL=(r.demo_fleet&&r.demo_fleet.length)?r.demo_fleet
                : ((r.manifest&&r.manifest.stores)||[]).map(s=>({id:s.id,kind:s.kind,
                    business_unit:s.business_unit,title:s.title||s.id,acl:s.acl||[],
                    description:s.description||""}));
      seq=0; selected=null; state=[]; state.tenant=r.tenant||"acme-demo";
      state.composed=true;                  // pre-composed: the Ask box is live from the start
      renderAll();
      centerOn(HUB.x, HUB.y, 1.0, false);   // empty canvas: a calm 100% on the hub (not Z_MAX)
      renderDemoHint();                     // rail = the IDENTICAL live palette (buildRail)
    }).catch(()=>{ seq=0; state=demo(); state.tenant="acme"; renderAll(); fitView(true); });
  }

  // #279 (B): the demo sidebar is the SAME provider palette as the live product - only the
  // backend differs. Picking a connector (addNode) maps, in demo mode, to a locally-backed
  // sample store of that kind: the four Azure SQL-family kinds + the local doc stores have
  // sample data; other kinds honestly report "sign in to connect your own" (no fake data).
  function demoAddKind(kind){
    const store=DEMO_POOL.find(s=>s.kind===kind && !state.some(n=>n.id===s.id));
    if(store){ connectDemoStore(store.id); return; }
    const label=(KINDS[kind]||{}).label||kind;
    if(DEMO_POOL.some(s=>s.kind===kind)) toast("Every sample "+label+" database is already on the canvas.");
    else toast("No sample data for "+label+" in the demo — sign in to connect your own.");
  }

  function connectDemoStore(id,opts){
    opts=opts||{};
    const existing=state.find(n=>n.id===id);
    if(existing){ flashCenter(existing.uid); return; }
    const s=DEMO_POOL.find(x=>x.id===id); if(!s) return;
    const i=state.length, x=900+(i%2)*570, y=470+Math.floor(i/2)*300;
    // s.kind verbatim (kindDef renders whatever it is). Behaviourally identical for the demo
    // fleet - every kind the server offers there IS in KINDS (asserted by
    // selftest_canvas_kind_coverage) - but the downgrade idiom does not belong on any path
    // that builds a node.
    const node=mk(s.id, s.kind, s.business_unit||"", s.acl||[],
                  {description:s.description||""}, "draft", x, y);   // draft(amber) = "testing…"
    state.push(node); clearDemoHint(); renderAll(); fitView(true); flashCenter(node.uid);
    const settle=()=>{ node.status="connected"; renderAll(); };      // →green "healthy"
    if(opts.instant) settle(); else setTimeout(settle,750);
  }

  // lightweight transient toast (top-centre of the canvas) for demo connect messages.
  let _toastT=null;
  function toast(msg){
    if(!alive) return;
    let t=document.getElementById("toast");
    if(!t){ t=document.createElement("div"); t.id="toast"; t.className="toast";
            document.getElementById("canvas").appendChild(t); }
    t.textContent=msg; t.classList.add("show");
    if(_toastT) clearTimeout(_toastT);
    // Dwell scales with length: 3.2s is right for "Saved" and unreadable for a full AADSTS
    // reason (#430), and a message that vanishes before it can be read is not a message.
    // ~55ms/char past the first 60, capped at 12s so it still clears itself.
    const ms=Math.min(12000, Math.max(3200, 3200+Math.max(0,String(msg).length-60)*55));
    _toastT=setTimeout(()=>t.classList.remove("show"),ms);
  }

  // #279 (B): after an answer, reveal any database that answered but isn't on the canvas yet -
  // the full sample is always queryable, so this makes the canvas SHOW the multi-DB routing.
  function revealAnswered(routing,citations){
    if(!isDemoMode()) return;
    const ids=new Set();
    ((routing&&routing.stores)||[]).forEach(s=>ids.add(s.store_id));
    ((routing&&routing.sub_queries)||[]).forEach(sq=>(sq.stores||[]).forEach(s=>ids.add(s.store_id)));
    (citations||[]).forEach(c=>{ if(c.store_id) ids.add(c.store_id); });
    ids.forEach(sid=>{ if(DEMO_POOL.some(p=>p.id===sid) && !state.some(n=>n.id===sid))
      connectDemoStore(sid,{instant:true}); });
  }

  function renderDemoHint(){
    clearDemoHint();
    if(!isDemoMode() || state.length) return;
    const c=document.getElementById("canvas");
    const h=document.createElement("div"); h.id="demoHint"; h.className="demo-hint";
    // #356: the canvas starting empty is deliberate (#279 - the visitor connects a sample
    // store themselves). The old copy was not: it said "add a database ... to begin, then
    // ask", which told a public visitor they had to connect something before they could ask.
    // Untrue - the demo catalog is pre-composed server-side (state.composed=true) and every
    // sample store is askable on arrival - and it pointed them at self-serve connect, the one
    // path that is not finished (#319). Say what is actually true, and keep the rail as the
    // optional gesture it was designed to be.
    const n=DEMO_POOL.length;
    // #386: this hint is dead-centre - exactly where a visitor's eye lands - while the only
    // sign-in affordance was a corner chip the operator himself could not find. So the hint
    // carries the CTA. Safe to gate on authState: boot is refreshAuth().then(bootCanvas),
    // so auth is resolved before this renders. Demo mode implies not signed in.
    // #446: to /signin, not /auth/login. From here the visitor has not chosen a provider
    // yet, and a Google-primary one would be sent to the wrong IdP by a hardcoded label.
    const cta=(authState.enabled||authState.google_enabled)
      ? '<br><a class="hint-signin" href="/signin">Sign in</a> to connect your own.'
      : '';
    h.innerHTML='<b>'+(n||"Sample")+' sample database'+(n===1?'':'s')+' are already connected'+
      '</b> and permission-trimmed.<br>Ask a question below right now, '+
      'or add them from the left &larr; to see them on the canvas.'+cta;
    c.appendChild(h);
  }
  function clearDemoHint(){ const h=document.getElementById("demoHint"); if(h) h.remove(); }

  // Hide the live-only build/admin controls when acting as a demo visitor - the demo is a
  // read-only "chat with these connected databases", with sign-in as the path to the live product.
  function applyModeChrome(){
    const demo=isDemoMode();
    // #643: on the SURFACE, not <body>. A class on <body> outlives the surface that set it,
    // so a visitor who saw Connectors in demo mode and moved to Ask left `demo-mode` behind
    // on the document for everything after it.
    host.classList.toggle("demo-mode", demo);            // collapses the panel grid track
    // #279 (B): the compose/setup/export controls are live-only; the connect RAIL stays (the
    // demo visitor builds the canvas by connecting sample databases), the config PANEL drops.
    ["setupChat","compose","export"].forEach(id=>{
      const b=document.getElementById(id); if(b) b.style.display=demo?"none":""; });
    // #803: the reset button ("Live demo") is dev/self-host chrome ONLY. In demo mode it is
    // dead weight like the rest; for a SIGNED-IN user it is a one-click workspace overwrite:
    // loadLiveDemo({fresh:true}) composes the DEMO_MANIFEST through _persisting_compose,
    // destroying their stored row - for stores whose demo-group ACLs their real identity
    // cannot even see (#293). Hidden whenever a real login is configured, which is exactly
    // demo-or-signed-in; the dev rig (no login) keeps its #199 escape hatch.
    const rb=document.getElementById("reset");
    if(rb) rb.style.display=realLoginConfigured()?"none":"";
    const panel=document.getElementById("panel"); if(panel) panel.style.display=demo?"none":"";
    const sub=host.querySelector(".cv-title .sub");   // #643: .brand was the old topbar's
    if(sub) sub.textContent=demo
      ? "Sample databases · sign in for your own"
      : (realLoginConfigured() && authState.signed_in)
        ? "Your databases"
        : "Connect your databases";
  }

  function isLiveUser(){ return realLoginConfigured() && authState.signed_in; }

  function bootCanvas(){
    booting=false;   // #295: auth resolved — the loader below sets the authoritative state, and
                     // its renderAll() SHOULD persist; the destructive pre-auth paint is behind us.
    applyModeChrome();
    if(isDemoMode()) loadDemoFleet();      // before-login demo: pre-composed badged fleet
    else if(isLiveUser()) loadLiveUser();  // #293: signed-in REAL user - THEIR own clean canvas
    else loadLiveDemo();                   // dev/self-host: the DEMO_MANIFEST + dev switcher
  }

  // #293: a signed-in real user must NOT get the DEMO_MANIFEST auto-composed - its demo-group
  // ACLs (all-staff/deal-team) are meaningless to a real Microsoft identity, so EVERY default
  // store is invisible and every ask answers "no accessible store for this user". They start
  // with a clean canvas and connect THEIR OWN databases (each auto-ACLs to them, #291); their
  // previously-composed canvas is their work, so it is restored across reloads/re-logins.
  function loadLiveUser(){
    // #368: the SERVER is the system of record for a signed-in user's stores - the stored
    // manifest survives restarts and other devices, and localStorage (which kept serving stale
    // "" configs after env fixes - the 260728 gotcha) demotes to a display cache for node
    // POSITIONS only. The manifest is already scoped to this caller's own workspace (the
    // endpoint keys on the caller's oid), so no further ACL filtering is needed here.
    api("/router/manifest").then(r=>{
      const m=r&&r.manifest;
      // #731: `stores: []` is AUTHORITATIVE empty - the owner deleted their last store and
      // that deletion is the truth. The old `m.stores.length` gate read empty as ABSENT and
      // fell through to localStorage, so delete-all could never persist: the stale local
      // copy resurrected the nodes and composeUp re-committed them. Only a genuinely
      // missing manifest (null - first visit, or store unavailable) falls back now.
      if(m&&Array.isArray(m.stores)){
        // #923: the uploads node rides in the row as a LAYOUT entry only (it is not a store -
        // no provider, liveManifest filters it), so its presence in layout IS the durable
        // "this user added the node" marker, position included.
        const lay=(m.layout&&typeof m.layout==="object")?m.layout:{};
        const upPos=Array.isArray(lay["your-documents"])?lay["your-documents"]:null;
        if(!m.stores.length){
          seq=0; selected=null; state=[]; state.tenant=m.tenant||"acme";
          if(upPos) ensureUploadNode(upPos[0], upPos[1]);
          rowHydrated=true;   // #951: the row WAS read - empty is its real content
          markRowClean();   // #818: hydration must not re-save the row it just read
          renderAll();
          if(state.length) fitView(true); else centerOn(HUB.x, HUB.y, 1.0, false);
          renderLiveHint();
          // #921: this branch never synced, so a user whose ONLY source was uploads lost the
          // node on every refresh until their next upload resurrected it. Empty stores is
          // not empty canvas - uploads exist without any composed store (#917).
          syncSharePointNodes(); syncDocumentsNode();
          loadPrincipals().then(renderPanel);
          return;
        }
        const saved=restoreCanvas()?state:[];   // reuse saved x/y for matching ids only
        // #818: the row's own layout OUTRANKS the localStorage cache - it is the copy that
        // followed the user from another device/browser; local x/y is the fallback.
        // (`lay` is hoisted above the empty-stores branch - #923 reads the uploads marker.)
        const posOf=id=>{
          const p=lay[id];
          if(Array.isArray(p)&&p.length===2&&isFinite(p[0])&&isFinite(p[1]))
            return {x:p[0],y:p[1]};
          const n=saved.find&&saved.find(o=>o.id===id);return n?{x:n.x,y:n.y}:null;};
        seq=0; selected=null;
        state=m.stores.map((s,i)=>{
          const p=posOf(s.id)||{x:900+(i%2)*570,y:470+Math.floor(i/2)*560};
          return nodeFromEntry(s,p.x,p.y);       // the inverse of entryOf - lossless
        });
        state.tenant=m.tenant||"acme";
        if(upPos) ensureUploadNode(upPos[0], upPos[1]);   // #923: the layout marker restores the node
        rowHydrated=true;   // #951: this mount now knows what the row holds
        markRowClean();   // #818: hydration must not re-save the row it just read
        renderAll(); fitView(true);
        composeUp();
        syncSharePointNodes(); syncDocumentsNode();
        loadPrincipals().then(renderPanel);
        return;
      }
      // NOT markRowClean on the fallback below: a localStorage-only canvas is exactly the
      // state the row should ADOPT, so the first autosave migrating it is the point (#818).
      // #951: the read SUCCEEDED and returned no row - a first visit. Adopting the local
      // copy into the row is #818's point, so this mount is hydrated.
      rowHydrated=true;
      loadLiveUserFromLocal();   // no server manifest yet - first visit
    }).catch(()=>{
      // #951: the read FAILED (store outage, network). rowHydrated stays FALSE, so this mount
      // renders from localStorage but may never write the row back - overwriting a row we
      // could not read is how a transient outage becomes permanent data loss.
      loadLiveUserFromLocal();
    });
  }
  function loadLiveUserFromLocal(){
    // pre-#368 fallback: restore from localStorage, keeping ONLY this user's own stores (ACL'd
    // to them). This drops any DEMO_MANIFEST / other-identity nodes a prior session persisted
    // (whose demo-group ACLs would be invisible and re-trigger "no accessible store"), while
    // their own connected DBs survive reloads and re-logins even when the manifest store is
    // unavailable or this is a first visit with nothing composed on the server yet.
    if(restoreCanvas()){
      const tenant=state.tenant;
      const mine=state.filter(n=>(n.acl||[]).includes(authState.oid));
      if(mine.length){
        state=mine; state.tenant=tenant;
        renderAll(); fitView(true);
        composeUp();
        syncSharePointNodes(); syncDocumentsNode();
        loadPrincipals().then(renderPanel);
        return;
      }
    }
    seq=0; selected=null; state=[]; state.tenant="acme";
    renderAll(); centerOn(HUB.x, HUB.y, 1.0, false); renderLiveHint();
    syncDocumentsNode();   // #917: uploads exist without any composed store
  }
  function renderLiveHint(){
    clearDemoHint();
    if(!isLiveUser() || state.length) return;
    const c=document.getElementById("canvas");
    const h=document.createElement("div"); h.id="demoHint"; h.className="demo-hint";
    h.innerHTML='&larr; Connect your database from the left to begin.<br>It stays private to you.';
    c.appendChild(h);
  }

  function loadLiveDemo(opts){
    // #803: NEVER for a signed-in real user. The demo manifest is not merely useless to them
    // (#293) - the composeUp below would write it over their stored workspace row. The button
    // is hidden for them (applyModeChrome), but hiding is chrome; this is the guard at the
    // point where the destruction would start, so stale chrome, a queued handler, or a future
    // caller cannot reach it. Their canvas is loadLiveUser's job.
    if(isLiveUser()) return;
    // A saved canvas is the user's work and OUTRANKS the demo manifest, so BOOT restores it.
    // The "Live demo" button passes {fresh:true}: that is the user explicitly asking for the
    // demo back, and it doubles as the escape hatch if a saved canvas ever goes bad.
    if(opts && opts.fresh){ try{ localStorage.removeItem(SAVE_KEY); }catch(e){} }
    else if(restoreCanvas()){
      renderAll(); fitView(true);
      composeUp();
      syncSharePointNodes(); syncDocumentsNode();
      loadPrincipals().then(renderPanel);   // #258: names for the ACL picker
      return;
    }
    api("/router/demo").then(r=>{
      const m=r.manifest; seq=0; selected=null;
      state=m.stores.map((s,i)=>nodeFromEntry(s,900+(i%2)*570,470+Math.floor(i/2)*560));
      state.tenant=m.tenant;
      renderAll(); fitView(true);
      composeUp();
      syncSharePointNodes(); syncDocumentsNode();          // #167: reflect any already-connected SharePoint tenant
      loadPrincipals().then(renderPanel);   // #258: names for the ACL picker
    }).catch(()=>{ seq=0; state=demo(); state.tenant="acme"; renderAll(); fitView(true); });
  }

  // #167: the canvas is otherwise stateless (reloads /router/demo each time), so a SharePoint
  // connection made through the OAuth round-trip would vanish. Derive it from the backend instead:
  // /connectors/sharepoint/status is the source of truth for "which tenants are connected".
  const spIngested = {};   // tenant -> {drive_id, docs} once a library is ingested (slice 2)
  /** #917: the "Your documents" node, DERIVED from server truth exactly the way
   *  syncSharePointNodes derives SharePoint nodes from /admin/sources. The node renders
   *  the count of the CALLER's uploads (uri upload://... in the ACL-trimmed
   *  /admin/documents), never a deployment-wide number (#550's trap), and it is excluded
   *  from liveManifest's stores - an action kind has no provider to compose. */
  let upDocsCache=[];   // #917: the panel overview renders from this; refreshed by every sync
  // #923: an explicit node delete must not be undone by the next sync's auto-adopt. Session
  // tombstone only - durable absence is the row's layout no longer carrying "your-documents".
  let upNodeGone=false;
  function ensureUploadNode(x,y){
    let n=state.find(s=>s.kind==="upload");
    if(!n){
      seq++;
      n={uid:"n"+seq,id:"your-documents",kind:"upload",bu:"",acl:[],derived:true,
         config:{description:"Documents you uploaded - private to you unless shared"},
         status:"connected", freshness:"0 docs",
         x:(isFinite(x)?x:Math.max(40,HUB.x-580)), y:(isFinite(y)?y:HUB.y+330)};
      state.push(n);
    }
    return n;
  }
  async function syncDocumentsNode(){
    let docs; try{ docs=await api("/admin/documents"); }catch(_){ return; }
    // Not `docs||[]`: an error body or a mocked route hands back an OBJECT, and .filter
    // on it throws inside a boot continuation (found by selftest_818's DOM harness).
    const mine=(Array.isArray(docs)?docs:[]).filter(d=>String(d.uri||"").startsWith("upload://"));
    upDocsCache=mine;
    let n=state.find(s=>s.kind==="upload");
    // #923: the node is FIRST-CLASS - it persists at 0 docs (the user added it; only their
    // explicit delete removes it, and THAT deletes the documents too). Docs without a node
    // still auto-adopt one, so canvases from before the node existed keep working - unless
    // this session just deleted the node (the tombstone above).
    if(!n && mine.length && !upNodeGone) n=ensureUploadNode();
    if(!n) return;
    n.status="connected";
    n.freshness=mine.length+" doc"+(mine.length===1?"":"s");
    renderAll();
  }

  async function syncSharePointNodes(){
    let st; try{ st=await api("/connectors/sharepoint/status"); }catch(_){ return; }
    if(!st || !st.configured || !(st.connected||[]).length) return;
    // detect already-ingested SharePoint sources so the ingested state + Ask bridge survive reloads
    let srcs=[]; try{ srcs=await api("/admin/sources"); }catch(_){}
    (srcs||[]).forEach(s=>{
      const sid=String(s.source_id||"");
      if(s.kind==="sharepoint" || sid.startsWith("sharepoint:")){
        const t=sid.split(":")[1]||"";
        if(t) spIngested[t]={source_id:sid, docs:(s.doc_count||s.docs||s.chunk_count||0)};
      }
    });
    let added=false;   // #941: did this sync ADOPT a node boot's compose never saw?
    st.connected.forEach(c=>{
      let n=state.find(s=>s.kind==="sharepoint" && s.config && s.config.tenant===c.tenant)
          || state.find(s=>s.kind==="sharepoint" && !(s.config && s.config.tenant));  // adopt a fresh node
      if(!n){
        seq++; added=true;
        n={uid:"n"+seq,id:"sharepoint",kind:"sharepoint",bu:"",acl:[],
           config:{description:"SharePoint documents (Entra-trimmed)",tenant:c.tenant,
                   site_url:"quantifymeai.sharepoint.com"},
           status:"connected", x:Math.max(40,HUB.x-580), y:Math.max(40,HUB.y-330)};
        state.push(n);
      } else { n.config=n.config||{}; n.config.tenant=c.tenant; n.status="connected"; }
      const ing=spIngested[c.tenant]; if(ing) n.freshness=ing.docs+" docs";
    });
    renderAll();
    // #941: this runs AFTER boot's composeUp, so a tenant it just adopted is in `state` and
    // not in `composedIds` - which would read as a draft the user never created. Compose it,
    // for the same reason testConn does: the node exists because the backend says the
    // connection is real, so leaving it uncomposed is the defect, not the honest state.
    if(added) composeUp();
  }
  /* ---------------- conversational setup (#116 C2) ---------------- */
  let setupMode=false, setupLog=[], setupState="gathering", setupErrors=true;
  let setupConv="c-"+Math.random().toString(16).slice(2);
  function toggleSetup(){
    setupMode=!setupMode;
    document.getElementById("setupChat").classList.toggle("primary",setupMode);
    renderPanel();
    if(setupMode && !setupLog.length){
      setupLog.push({who:"bot",text:"Describe a source in plain language — e.g. "+
        "“folder at /mnt/contracts for the legal team, visible to legal-staff”. "+
        "Then “Ready” to review, “Apply” to compose."});
      renderPanel();
    }
  }
  function renderSetupPanel(p){
    const msgs=setupLog.map(m=>'<div class="smsg '+m.who+'">'+esc(m.text)+'</div>').join("");
    p.innerHTML=
      '<div class="setup-head">⚡ Setup by chat <span class="hint">('+esc(setupState)+')</span></div>'+
      '<div class="setup-log" id="setupLogEl">'+msgs+'</div>'+
      '<div class="setup-row"><input id="setupInput" placeholder="Describe a source…" spellcheck="false">'+
      '<button class="btn" id="setupSend">Send</button></div>'+
      '<div class="setup-row">'+
      '<button class="btn" id="setupReady">Ready ▸</button>'+
      '<button class="btn primary" id="setupApply"'+((setupState!=="confirming"||setupErrors)?" disabled":"")+'>Apply ⚡</button>'+
      '<button class="btn" id="setupVerify"'+(setupState!=="applied"?" disabled":"")+
      ' title="Ask the new federation a test question (type one, or let it suggest)">Verify ✓</button>'+
      '<button class="btn" id="setupNew" title="Start over">New</button></div>';
    const log=p.querySelector("#setupLogEl"); log.scrollTop=log.scrollHeight;
    const inp=p.querySelector("#setupInput");
    p.querySelector("#setupSend").onclick=()=>sendSetup("chat",inp.value);
    inp.addEventListener("keydown",e=>{ if(e.key==="Enter") sendSetup("chat",inp.value); });
    p.querySelector("#setupReady").onclick=()=>sendSetup("ready",inp.value);
    p.querySelector("#setupApply").onclick=()=>sendSetup("apply","");
    p.querySelector("#setupVerify").onclick=()=>sendSetup("verify",inp.value);
    p.querySelector("#setupNew").onclick=()=>{ setupLog=[]; setupState="gathering";
      setupErrors=true; setupConv="c-"+Math.random().toString(16).slice(2); toggleSetup(); toggleSetup(); };
    inp.focus();
  }
  function sendSetup(intent,message){
    message=(message||"").trim();
    if(intent==="chat" && !message) return;
    if(message && intent!=="verify") setupLog.push({who:"me",text:message});
    const inpEl=document.getElementById("setupInput"); if(inpEl) inpEl.value="";
    api("/router/setup/turn",{method:"POST",
        body:JSON.stringify({conv_id:setupConv,message:message,intent:intent})})
      .then(t=>{
        setupState=t.state;
        if(t.reply) setupLog.push({who:"bot",text:t.reply});
        if(t.state==="confirming" && t.manifest){
          const lines=t.manifest.stores.map(s=>"  - "+s.id+" ("+s.kind+
            (s.business_unit?" · "+s.business_unit:"")+") acl: "+(s.acl.join(",")||"—"));
          setupLog.push({who:"bot",text:"Manifest:\n"+lines.join("\n")});
          (t.validation||[]).forEach(v=>setupLog.push({who:"bot",
            text:(v.level==="error"?"✗ ":"◌ ")+v.message}));
          setupErrors=(t.validation||[]).some(v=>v.level==="error");
        }
        if(t.state==="applied" && t.result){
          adoptApplied(t.manifest,t.result);
          setupLog.push({who:"bot",text:"Composed ✓ "+t.result.stores.length+" live"+
            ((t.result.skipped||[]).length?" · "+t.result.skipped.length+" skipped":"")+
            ". Ask it something in the dock below!"});
        }
        renderPanel();
      })
      .catch(e=>{ setupLog.push({who:"bot",text:"Error: "+(e.message||e)}); renderPanel(); });
  }
  function adoptApplied(manifest,result){
    // mirror loadLiveDemo: the applied manifest becomes the canvas nodes
    const byId={}; (result.stores||[]).forEach(s=>{byId[s.store_id]=s;});
    const skip={}; (result.skipped||[]).forEach(s=>{skip[s.id]=s.reason;});
    seq=0; selected=null;
    // #368 review (IMPORTANT 1): the SAME inverse mapper as the restore path. This is the
    // setup agent's output, which is where a `folder` store - a real provider with no KINDS
    // row - actually comes from, so the downgrade-to-`local` bug was reachable here FIRST:
    // adopt it as `local`, and the next composeUp() writes that into the stored manifest.
    state=manifest.stores.map((s,i)=>nodeFromEntry(s,900+(i%2)*570,470+Math.floor(i/2)*560));
    state.tenant=manifest.tenant;
    state.forEach(n=>{
      // #808: same warning mapping as composeUp - the setup agent composes for real, so a
      // store it stands up can be just as unusable, and this path must not be the one that
      // stays silent (the #799 lesson: fix every home of a rule, not the one you looked at).
      if(byId[n.id]){ n.status="connected"; n.freshness=byId[n.id].freshness||"";
                      n.warnings=byId[n.id].warnings||[]; }
      else if(skip[n.id]!==undefined){ n.status="planned"; n.reason=skip[n.id]; n.warnings=[]; }
    });
    noteComposed(result.stores);   // #941: the OTHER home of the compose response - see isUncomposed
    state.composed=true;
    renderAll();
  }

  function routeLive(){
    // E7 advisor: which store WOULD answer — ranked candidates with score + why,
    // no execution. Click ⊙ pin to ask that store directly (manual override).
    const q=document.getElementById("qtext").value.trim();
    const out=document.getElementById("qresult");
    if(!q){ toast("Type a question first, then Route to see which store would answer."); return; }  // #308: never a silent no-op
    out.className="qresult proof-host open";
    out.innerHTML='<div class="qmeta">advising…</div>';
    Promise.all([
      api("/router/route",{method:"POST",body:JSON.stringify({question:q})}),
      api("/router/catalog").catch(()=>null),
    ]).then(([r,cat])=>{
      const fresh={};
      if(cat)(cat.business_units||[]).forEach(b=>(b.sources||[]).forEach(s=>
        (s.stores||[]).forEach(st=>{fresh[st.store_id]=st.freshness;})));
      const picked=new Set((r.stores||[]).map(s=>s.store_id));
      const max=Math.max(0.0001,...(r.candidates||[]).map(c=>c.score));
      const rows=(r.candidates||[]).map(c=>
        '<div class="qcand">'+
          '<span style="min-width:110px'+(picked.has(c.store_id)?';color:var(--accent)':'')+'">'+
            (picked.has(c.store_id)?"▸ ":"")+esc(c.store_id)+'</span>'+
          '<span class="qbar"><i style="width:'+Math.round(100*c.score/max)+'%"></i></span>'+
          '<span>'+c.score.toFixed(2)+'</span>'+
          '<span class="qwhy">'+esc(c.why||"")+(fresh[c.store_id]?" · "+esc(fresh[c.store_id]):"")+'</span>'+
          '<button class="qpin" data-pin="'+esc(c.store_id)+'" title="ask this store (manual override)">⊙ pin</button>'+
        '</div>').join("");
      // #759: the THIRD consumer of `reason`, and the one #753 broke. Until #753 the compound
      // reason spelled out every sub-question and its target store, and this panel printed it —
      // so removing the enumeration server-side left the advisor saying "decomposed into 2
      // sub-queries" with the question→store mapping unrecoverable, while its candidate rows are
      // the UNION across sub-questions with no mapping at all. The structured field was already
      // on this wire (`/router/route` returns the full RoutingDecision), so it is rendered the
      // way the ask trace renders it rather than by putting the prose back.
      const rsubs=(r.sub_queries||[]).map(sq=>{
        const tgt=(sq.stores&&sq.stores.length)?sq.stores.map(s=>s.store_id).join(", ")
                                               :"no accessible source";
        return '<div class="qmeta">↳ <i>'+esc(sq.question)+'</i> → '+esc(tgt)+
               ' <span style="opacity:.6">('+esc(sq.query_type)+')</span></div>';
      }).join("");
      out.innerHTML=
        // #307: Route replaces the answer area, so give it an explicit way out — a ✕ that closes
        // the advisor panel (the whole-catalog Ask is one click away; ⊙ pin runs a specific store).
        '<div class="qmeta qroutehdr"><span><b>'+esc(r.query_type)+'</b> · '+esc(r.method)+
        ' · conf '+(r.confidence||0).toFixed(2)+' · '+esc(r.reason||"")+'</span>'+
        '<button class="qroute-close" title="close the route advisor">✕ close</button></div>'+
        rsubs+
        (rows||'<div class="qmeta">no visible candidates</div>');
      out.querySelectorAll(".qpin").forEach(b=>{ b.onclick=()=>askLive(b.dataset.pin); });
      const rc=out.querySelector(".qroute-close");
      if(rc) rc.onclick=()=>{ out.className="qresult proof-host"; out.innerHTML=""; };
    }).catch(e=>{ out.innerHTML='<div class="qerr">'+esc(e.message||e)+'</div>'; });
  }
  function askLive(pin){
    const q=document.getElementById("qtext").value.trim();
    const out=document.getElementById("qresult");
    if(!q){ return; }
    out.className="qresult proof-host open";
    // #724 review: THIS RESET IS LOAD-BEARING. `out` is the persistent #qresult element and
    // assigning innerHTML does not clear `dataset`, so both values below survived from the
    // PREVIOUS question. They are written only on the router's success path, so an ask whose
    // router call errored inherited the last one's: the document half then numbered its sources
    // [3] and [4] with no [1] or [2] anywhere on screen. Reproduced in a real DOM with two asks.
    // Cleared at the START of every ask, so every path — success, error, pinned — begins from
    // "the router has said nothing yet", which is the only true statement at this moment.
    delete out.dataset.fnCount;
    delete out.dataset.routerEvidence;
    out.innerHTML='<div class="qmeta">'+(pin?"asking "+esc(pin)+" (pinned)…":"routing…")+'</div>';
    const body={question:q}; if(pin) body.store=pin;
    // #255: the document bridge is asked on EVERY ask (see askSharePoint below) — it used to
    // be gated on SharePoint-connector state, which made upload-borne docs unaskable here.
    api("/router/ask",{method:"POST",body:JSON.stringify(body)})
      .then(r=>{
        // #177: the numbered Sources list below is the single provenance surface — no cryptic pills.
        // #729(b): these per-sub-question lines are the SAME engineer telemetry `tele` was moved
        // for — a store id, a query_type and an arrow — and until now they were the only thing
        // rendered ABOVE the answer. So "the telemetry is not the first thing on screen" held for
        // simple asks and quietly failed for compound ones: the reader who asked the hardest kind
        // of question got the most diagnostics before the answer to it. They now go where the
        // telemetry went, and `subs` is consumed inside `trace` below, not in the layout.
        const subq=(r.routing.sub_queries||[]);
        const subs=subq.map(sq=>{
          const tgt=(sq.stores&&sq.stores.length)?sq.stores.map(s=>s.store_id).join(", "):"no accessible source";
          return '<div class="qmeta">↳ <i>'+esc(sq.question)+'</i> → '+esc(tgt)+' <span style="opacity:.6">('+esc(sq.query_type)+')</span></div>';
        }).join("");
        const fns=(r.footnotes||[]);
        const oc=(r.outcomes||[]);
        // #715: the stores that ALSO matched this question and were NOT consulted. `candidates`
        // is every visible store the router scored — never one the caller cannot see (gate #1) —
        // so this discloses a choice the router was already making silently. Live example: "what
        // is our parental leave policy" answered out of a demo store while the owner's real HR
        // documents sat unconsulted, with nothing on screen to say a rival had even matched.
        //
        // The selector's behaviour is UNCHANGED. NEAR_TIE is a DISPLAY threshold: at thirteen
        // stores, listing every candidate would be noise, and the routing thresholds are
        // load-bearing and belong to #715/#718's own design pass. Moving this number changes
        // what is DISCLOSED, never what is ASKED.
        const sel={}; (r.routing.stores||[]).forEach(s=>{ sel[s.store_id]=1; });
        const top=(r.routing.stores||[]).reduce((m,s)=>Math.max(m,s.score||0),0);
        const rivals=(r.routing.candidates||[])
          .filter(c=>!sel[c.store_id]&&(c.score||0)>=top-NEAR_TIE)
          .sort((a,b)=>(b.score||0)-(a.score||0)).slice(0,4);
        // "unassigned" is the placeholder a store carries when nobody has given it a business
        // unit - it names nothing, and printing "bigquery-1 (unassigned)" spends the reader's
        // attention on a word that cannot help them decide whether the rival mattered.
        const bu=c=>(c.business_unit&&c.business_unit!=="unassigned")?' ('+esc(c.business_unit)+')':'';
        const also=rivals.length?'<div class="qalso">also matched: '+
          rivals.map(c=>esc(c.store_id)+bu(c)).join(', ')+
          ' — not consulted</div>':'';
        // #729: this telemetry ("analytical · prefilter · conf 0.72 · matched revenue columns")
        // was the FIRST LINE of every answer — engineer diagnostics printed above the thing the
        // user actually asked for. It is provenance, so it moves in with the provenance.
        const tele='<div class="qmeta"><b>'+esc(r.routing.query_type)+'</b> · '+esc(r.routing.method)+
          ' · conf '+(r.routing.confidence||0).toFixed(2)+
          (r.routing.reason?' · '+esc(r.routing.reason):'')+'</div>';
        // #729: the summary counted OUTCOMES (stores that ran) while the rail beside it numbered
        // FOOTNOTES (citations), so "(1 source)" sat next to [1][2][3]. Two true counts of two
        // different things, reading on screen as one wrong count. Name both, or neither.
        // #729, second review: SCOPED, because the count went wrong again the moment #724's
        // numbering fix put the document half's [2] on the same screen — "1 citation" beside a
        // visible [1] AND [2] is exactly the shape this line was written to kill. The trace
        // describes the router's own rail and nothing else, so it now SAYS so rather than
        // implying a total it does not count. The document half carries its own "drew on N
        // docs" header, which is the matching statement for its half.
        // "from your databases" was hardcoded, and on a compound ask it sat four lines above a
        // citation card reading "Folder · folder-1 · leave-policy.txt". A folder of text files
        // is not a database — the same shape of defect as #728, a label contradicted by the
        // evidence directly beneath it, and on the surface whose job is to be checkable. The
        // router's rail carries whatever the router reached, so the noun is read off the
        // citations rather than assumed. The SCOPE the #729 review added is kept: this still
        // says whose rail it is counting, which is what stops it reading as a total.
        const kinds=new Set(fns.map(f=>f.kind).filter(Boolean));
        const whose=!kinds.size?'':
          (kinds.size===1&&kinds.has('sql'))?' from your databases':
          (kinds.size===1&&kinds.has('document'))?' from your documents':' from your sources';
        // #765: the two halves of this parenthetical used to be joined by different separators -
        // `nsub` below appends " · " and this appended ", " - so one summary carried a middot and
        // a comma doing the same job three words apart. Mine, from #729(b). The middot wins: it
        // is what the telemetry line and the outcome rows beside this already use.
        const cnt=oc.length
          ? oc.length+' source'+(oc.length===1?'':'s')+
            (fns.length?' · '+fns.length+' citation'+(fns.length===1?'':'s')+whose:'')
          : 'no source answered';
        // #729(b): a compound ask was BROKEN UP, and that is the one fact about it the summary
        // has to carry — the sub-questions are now folded away inside, so a reader with no hint
        // that their question became two has no reason to open the trace and find out. Kept as a
        // prefix rather than woven into `cnt` so the segments the existing count test pins
        // ("1 source", "2 citations", "from your databases") are untouched on both paths.
        const nsub=subq.length>1?subq.length+' questions · ':'';
        // Always rendered now: it carries the telemetry above, which must not vanish with it.
        // #745 round 2: the glyph is wrapped so it can ROTATE. Round 1 suppressed the native
        // marker and left this as a static character — which removed the only open/closed
        // signal the control had, because the native marker was the thing that turned. A
        // duplicate arrow is cosmetic; an arrow that never moves is a control that lies.
        const perStore={}; oc.forEach(o=>{ perStore[o.store_id]=(perStore[o.store_id]||0)+1; });
        const trace='<details class="tracefoot"><summary><span class="tri">▸</span> How this was answered ('+nsub+cnt+')</summary>'+
          tele+
          subs+
          oc.map(o=>{ const fo=fns.find(f=>f.store_id===o.store_id);
            // #753 round 2. The label used to be `origin`'s first two segments — which is
            // `system · location`, a name for a human and NOT the store id the ↳ lines above
            // print. So the join my own #753 comment claimed ("the store name is enough to join
            // the two") did not exist: on a compound ask, `azure_sql-1` appeared nowhere in its
            // own outcome row.
            //
            // #783: what stood here next was FALSE, and correcting it is worth more than
            // deleting it. It said the quote "was dropped exactly where the join failed and kept
            // where it worked." There is no mechanism for that: the old predicate read `subs`
            // and `perStore` only and never consulted the footnote, so quote-retention and
            // label-usefulness were INDEPENDENT. Worked through the two compound fixtures they
            // land OPPOSITE — compound_same_store KEPT the quote while its label carried no
            // store_id (the join failed), and compound_covered's folder-1 DROPPED it while its
            // label did carry the id (the join worked). The fallback case that sentence invoked,
            // a store with no footnote at all, is in no fixture. The id leads now, so the join
            // holds for every row regardless of any of this.
            //
            // #729: the same "unassigned" placeholder the disclosure line drops — cleaning it off
            // one surface and leaving it on the two beside it is worse than leaving it
            // everywhere, because the reader sees the product disagreeing with itself about
            // whether the word means anything.
            //
            // #761: composed as SEGMENTS and de-duplicated once. The old form appended the
            // business unit to a label that could already contain it, and printed
            // "Azure SQL · finance · finance · ok" for a year of fixtures with nothing checking.
            const seg=[o.store_id].concat(fo?fo.origin.split(" · ").slice(0,2):[]);
            if(o.business_unit&&o.business_unit!=="unassigned") seg.push(o.business_unit);
            const lbl=seg.filter((s,i)=>s&&seg.indexOf(s)===i).join(" · ");
            return '<div class="qmeta">'+(o.status==="ok"?"✓":"✗")+' '+esc(lbl)+
            ' · '+esc(o.status)+(o.status==="ok"?' · '+o.count+' result'+(o.count===1?'':'s'):'')+
            // #753, the last of the three copies. Once `subs` renders above, "question → store"
            // is already on screen, so quoting the question here says it a second time — and now
            // that the row leads with the store id, the line above genuinely joins to it. It is
            // kept in the ONE case where the id cannot join them: two sub-questions dispatched to
            // the SAME store produce two rows that are otherwise identical, and dropping the
            // quote would make them unreadable.
            ((o.sub_question && (!subs || perStore[o.store_id]>1))
              ? ' · ↳ "'+esc(o.sub_question)+'"' : '')+
            (o.error?' · '+esc(o.error):'')+'</div>'; }).join("")+'</details>';
        const sources=sourcesBlockHTML(fns);   // #689: ui/proofs.js, shared with /ask
        out.innerHTML=
          // #729(b): `subs` used to sit here, and it was the ONLY compound-conditional node
          // before the answer. The answer is now the first thing on screen for every shape of
          // ask, not just the simple one.
          '<div class="qanswer">'+fmtAnswer(r.answer)+'</div>'+
          ((SHOW_DISCLOSURE && r.disclosure)?'<div class="qdisc">⚠ '+esc(r.disclosure)+'</div>':'')+
          also+
          trace+
          sources+
          '<div class="qsp" id="qsp"><div class="qmeta">↳ searching documents…</div></div>';
        // #256: record whether the ROUTER produced evidence, so the document half (which
        // resolves later) can reconcile instead of contradicting it. Keyed on footnote COUNT,
        // never on the answer's wording — #233 showed how fragile matching phrasing is.
        out.dataset.routerEvidence = fns.length ? "1" : "0";
        // #724: the document half numbers its own sources from [1] too, so with both halves on
        // screen [2] meant a MySQL row above and an HR policy below - one marker, two meanings,
        // and a reader tracing a citation lands on the wrong evidence. The doc block continues
        // this count instead of restarting it. Offsetting is safe where RENUMBERING a dangling
        // marker would not be (#257): every marker and its source shift by the same constant,
        // so the mapping the model asserted is preserved exactly.
        out.dataset.fnCount = String(fns.length);
        const fnById={}; fns.forEach(f=>fnById[f.n]=f);
        out.querySelectorAll(".fnref").forEach(el=>{ el.onclick=()=>{
          const t=document.getElementById("fn"+el.dataset.fn); if(!t) return;
          out.querySelectorAll(".src.hl").forEach(s=>s.classList.remove("hl"));
          t.classList.add("hl"); t.scrollIntoView({block:"nearest"}); };});
        // #177: explanatory Sources actions — "Show query" reveals SQL, "Verify data" re-runs
        // live. #689: ui/proofs.js, shared with /ask, driven through THIS surface's own `api`.
        wireProofActions(out, fns,
          (p) => api("/router/rerun", { method: "POST", body: JSON.stringify(p) }));
        revealAnswered(r.routing, r.citations);   // #279 (B): show the databases that answered
        bridgeFor(r, q);
      })
      .catch(e=>{ out.innerHTML='<div class="qerr">'+esc(e.message||e)+'</div>'+
                    '<div class="qsp" id="qsp"><div class="qmeta">↳ searching documents…</div></div>';
                  askSharePoint(q); });
  }
  // #747: WHAT THE BRIDGE IS ASKED, on a compound question.
  //
  // The bridge used to be handed the WHOLE original question on every ask. On a compound one the
  // router has already split it and answered the halves from different stores — so the bridge was
  // being asked about a half that was routed elsewhere and could only ever decline on it. Live,
  // that read as: "**Freight cost for each region** I do not have that information in the provided
  // context", printed one screen below the freight costs the product had just retrieved correctly.
  // The product contradicting itself on one screen is the exact failure #724 exists to prevent —
  // #724 only removed the case where the documents contribute NOTHING, and a partial contribution
  // sails straight through its gate, because `referenced` is non-empty for the half that DID land.
  //
  // Coverage is read STRUCTURALLY off the outcomes — never by matching the disclosure's wording,
  // which is #233's standing lesson (every matcher built on phrasing has broken). A sub-question
  // counts as covered when a store actually returned rows for it.
  //
  // All covered → the bridge is not asked at all, and that kills the DUPLICATE too: the same live
  // screen answered "parental leave" twice, once from folder-1 via the router and again from an
  // uploaded copy via the bridge. Asking only what is still open is the honest reading of what the
  // bridge is FOR. It does cost the uploaded copy of an already-answered sub-question — which is
  // the point, that copy was the duplicate.
  function uncoveredSubQuestions(r){
    const subq=((r.routing||{}).sub_queries)||[];
    if(subq.length<2) return null;          // not compound — behaviour unchanged, ask the whole q
    const norm=s=>String(s||"").trim().toLowerCase();
    const answered=new Set((r.outcomes||[])
      .filter(o=>o.status==="ok" && (o.count||0)>0 && o.sub_question)
      .map(o=>norm(o.sub_question)));
    return subq.map(s=>s.question).filter(qq=>!answered.has(norm(qq)));
  }
  function bridgeFor(r, q){
    const open=uncoveredSubQuestions(r);
    if(open===null){ askSharePoint(q); return; }          // simple ask — unchanged
    if(open.length){ askSharePoint(open.join(" ")); return; }
    const el=document.getElementById("qsp"); if(el) el.remove();
  }
  // #170 bridge: SharePoint content lives in the edition index (/search), a different surface
  // from the router catalog — query it too so the canvas is the single ask surface. LAW 2 still
  // applies: /search trims to what the selected identity is authorized to see.
  function askSharePoint(q){
    // #279: /search is the LIVE edition index (SharePoint/uploads), refused for a demo identity.
    // In demo mode the router's own /router/ask already answers over the demo doc stores
    // (hr-wiki, fin-ledger), so there is no second document surface to bridge - skip it cleanly.
    if(isDemoMode()){ const el=document.getElementById("qsp"); if(el) el.remove(); return; }
    api("/search",{method:"POST",body:JSON.stringify({question:q})})
      .then(s=>{
        const el=document.getElementById("qsp"); if(!el) return;
        // #393: this is what the question RETRIEVED, not what the identity may see. The two
        // were the same variable once, and this panel printed the first as the second.
        const nAuth=(s.retrieved_docs||s.authorized_docs||[]).length;
        const cl=(s.citations||[]);
        // #724: `citations` is what the question RETRIEVED - the set the model was SHOWN. What
        // the answer POINTS AT is `referenced`, and the two are not the same question. Reading
        // the first as the second is what put "I do not have that information" above a numbered
        // Sources list naming an unrelated directorship PDF and an HR leave policy, under a
        // REVENUE answer the router had already got right. Anything below that asks "did the
        // documents contribute?" must ask THIS list, never cl.length.
        const ref=(s.referenced||[]);
        // #255: nothing authorized to show → remove the panel entirely rather than leaving a
        // "0 authorized docs" stub on every SQL-only ask. The router's own answer already
        // carries the honest "no evidence" story (#218); a second empty panel just adds noise.
        if(!cl.length && !nAuth){ el.remove(); return; }
        // #256: the router already painted its verdict. When it found nothing, that verdict is
        // an abstention saying no visible source holds this kind of data — which becomes FALSE
        // the moment documents answer, and it sits directly above the answer contradicting it.
        // A user reading top-down stops at the abstention and never scrolls, so the product
        // looks broken while working. Retract just that sentence; the disclosure beneath it
        // stays, because naming WHICH stores declined is still true. The replacement wording
        // also covers the error case — a store that failed likewise "did not answer".
        const outEl=document.getElementById("qresult");
        // #724: gated on `ref`, not `cl`. On cl.length this fired whenever anything was merely
        // RETRIEVED - so a question the documents also could not answer replaced the router's
        // honest abstention with "the answer below comes from your documents", pointing at a
        // paragraph that then said it did not have the information either. Retract the
        // abstention only when the documents genuinely answered.
        if(ref.length && outEl && outEl.dataset.routerEvidence==="0"){
          const ans=outEl.querySelector(".qanswer");
          if(ans) ans.textContent="No structured source answered this — the answer below comes "
                                 +"from your documents.";
        }
        // label the SURFACE, not one connector: these docs may have arrived by upload or any
        // future source, so calling them "SharePoint" would misattribute their origin.
        const entitled=(s.corpus&&s.corpus.authorized_docs)||0;
        const drawn=nAuth+' doc'+(nAuth===1?'':'s')
          +(entitled?' of '+entitled+' you can access':'');
        // #724: THE DOCUMENTS CONTRIBUTED NOTHING. Retrieval found something to read, the model
        // read it, and its answer points at none of it. What used to render here was a decline
        // paragraph ("I do not have that information") plus a full numbered Sources list of
        // everything retrieved — appended beneath a correct SQL answer, so a revenue question
        // ended with an HR leave policy and a directorship PDF displayed as sources. Both halves
        // were noise, and the second was worse than noise: a Sources list is a provenance claim,
        // and there was no answer for it to be the provenance OF.
        //
        // What survives is the one fact worth a line — that the documents were searched too, and
        // came back empty. Deliberately not removed outright (the #255 path above already does
        // that when retrieval found nothing at all): silence here would leave a user who KNOWS
        // the answer is in their documents unable to tell a search that missed from a search that
        // never ran.
        if(!ref.length){
          el.innerHTML='<div class="qmeta">↳ also searched your documents — read '+esc(drawn)
                      +', none answered this.</div>';
          // AND IT MOVES. Seen on prod: the line was honest but sat at the very bottom, under
          // three source cards, a scroll away from the "also matched … not consulted" line it
          // belongs beside. Both sentences answer the same question - what else was consulted
          // and came to nothing - so they read as one thought or neither is found. When the
          // documents DO answer, the block stays where it is: it is an answer then, and an
          // answer belongs after the evidence for the one above it, not wedged into the
          // routing notes.
          const anchor=outEl&&outEl.querySelector(".tracefoot");
          if(anchor&&anchor.parentNode===outEl) outEl.insertBefore(el,anchor);
          return;
        }
        // #257: render the sources as a NUMBERED list, in citation order, so a [n] in the prose
        // has something on screen to land on. The bare pill carried no number, so every marker
        // was unresolvable by inspection — the reader saw a footnote and had nowhere to follow
        // it. Same rule the router's Sources block already obeys (#177): a marker that cannot be
        // resolved reads as corroboration the answer has not actually got.
        // #724: numbering CONTINUES the router's footnotes rather than restarting, so [2] means
        // one thing on this screen. `off` is the count the router half recorded on #qresult.
        const off=Number((outEl&&outEl.dataset.fnCount)||0)||0;
        const srcs=cl.map((c,i)=>{
          const n=i+1+off;
          const label=esc(c.title||c.doc||"doc");
          const where=c.uri?esc(String(c.uri).replace(/^upload:\/\//,"uploaded · ")):"";
          // #724: the answer points at some of these and not others, and saying which is the
          // difference between evidence and attribution (#633's pointed/retrieved rule). An
          // unpointed row stays on the list — it is honestly part of what was read — but it must
          // not sit there unlabelled, looking like support the answer never claimed.
          const used=ref.indexOf(i+1)>=0;
          // #729, second review: this card said the same thing three times and named no store —
          // "[2] Document document hr-leave-policy.txt · uploaded · hr-leave-policy.txt". The
          // system slot was the hardcoded word "Document" sitting beside a "document" tag, and
          // `label` (the title) and `where` (the rewritten upload:// uri) were BOTH the filename.
          // Meanwhile the ROUTER's card beside it reads "Azure SQL · host / database", so the
          // one thing a reader actually needs here — which surface this came from — was the one
          // thing missing. The system slot now names the surface, and the location line drops a
          // `where` that merely repeats the label.
          const sameTwice=where&&where.replace(/^uploaded · /,"")===label;
          const body='<span class="snum">['+n+']</span> <span class="ssys">Your documents</span>'
                    +'<span class="stag">'+(used?'document':'read, not cited')+'</span>';
          return '<div class="src'+(used?'':' src--unused')+'" id="dfn'+n+'"><div>'+body+'</div>'
                +'<div class="sloc">'+label+((where&&!sameTwice)?' · '+where:
                                             (sameTwice?' · uploaded':''))+'</div>'
                +(c.uri&&!/^upload:\/\//.test(c.uri)
                    ?'<div class="sacts"><a class="sbtn" href="'+esc(c.uri)+'" target="_blank" rel="noopener">↗ Open source</a></div>'
                    :'')
                +'</div>';
        }).join("");
        el.innerHTML='<div class="qmeta"><b>Documents</b> · edition index · drew on '+esc(drawn)+'</div>'+
          '<div class="qanswer">'+fmtAnswer(s.answer,{attr:"data-dfn",offset:off})+'</div>'+
          (srcs?'<div class="sources"><div class="shdr">Sources — where this answer came from</div>'+srcs+'</div>':'');
        // clicking a marker highlights its source, exactly as the router's citations do
        el.querySelectorAll(".fnref").forEach(x=>{ x.onclick=()=>{
          const t=document.getElementById("dfn"+x.dataset.dfn); if(!t) return;
          el.querySelectorAll(".src.hl").forEach(s=>s.classList.remove("hl"));
          t.classList.add("hl"); t.scrollIntoView({block:"nearest"}); };});
      })
      .catch(e=>{ const el=document.getElementById("qsp"); if(el) el.innerHTML='<div class="qerr">Documents: '+esc(e.message||e)+'</div>'; });
  }
  // #171 real per-user Microsoft/Entra sign-in
  let authState={enabled:false, signed_in:false, name:"", email:"", google_enabled:false, linked:[]};
  // #320: connection env vars THIS server can resolve (names only). Empty until /config
  // answers, which means a node dropped before boot completes gets blank fields - the safe
  // direction, since a blank field is fixable by typing and a dead ${REF} is not.
  let ENV_PRESENT=new Set();
  // ADR 0011: operator affordances. Defaults TRUE so dev rigs and old servers (no
  // `operator` field) behave exactly as today; a real-login server that says false
  // withholds env prefill (and already sends env_present:[]).
  let CFG_OPERATOR=true;
  function refreshAuth(){
    if(!alive) return ABANDONED;
    return fetch("/auth/me").then(r=>r.json()).then(a=>{ if(!alive) return; authState=a; renderAuth(); })
      .catch(()=>{ authState={enabled:false,signed_in:false,google_enabled:false,linked:[]}; renderAuth(); });
  }
  /**
   * #643: what is LEFT of the canvas's auth chip after the merge, which is the grant
   * affordance and nothing else.
   *
   * It used to render an avatar, the user's name, a "session expired" warning, a Microsoft
   * sign-in button and Sign out - a second, differently-shaped answer to "who am I?" sitting
   * three inches from the shell's account control, which answers the same question with an
   * avatar and a dropdown. That divergence IS #414's open subtask: #630 gave the shell a
   * control with four honest per-provider states, the canvas kept flat text, and the
   * confusion #630 set out to remove simply moved to the seam between the two front-ends.
   * With one document there is no seam, and the shell's control is the one that stays.
   *
   * Google is the exception, and deliberately so. account.js renders "Connect" for a provider
   * you have not granted and links it HERE, on the stated ground that "the grant flow lives on
   * the canvas; this is a pointer to it, not a second copy of it". Dropping this pill would
   * make that pointer land on a page with nothing to click. Microsoft needs no equivalent: its
   * grant is the "Connect with Microsoft" button on a SharePoint node, already on this canvas.
   */
  function renderAuth(){
    const el=document.getElementById("authArea"); if(!el) return;
    let html="";
    // Google linking is independent of Entra sign-in (#193): a BigQuery store with
    // require_signin needs a linked Google account regardless of Microsoft state.
    if(authState.google_enabled){
      const linked=(authState.linked||[]).indexOf("google")>=0;
      html+='<a class="pill'+(linked?" ok":"")+'" href="/auth/google/login" '+
            'title="queries against GCP stores run as this Google account">'+
            (linked?"✓ Google linked":"Connect Google")+'</a>';
    }
    el.innerHTML=html;
    // signed in → the query identity is the verified user (server reads the session cookie),
    // so the dev switcher is irrelevant; hide it.
    const qu=document.getElementById("quser"); if(qu) qu.style.display=authState.signed_in?"none":"";
  }
  // #171 sign-in / #193 link outcome. #430: these used to be written to #statusbar, which
  // renderStatus() overwrites wholesale (bar.innerHTML=...) on the very next render - so in
  // practice the user NEVER saw a sign-in failure. Against a real foreign tenant that cost six
  // retries and a trip to the prod logs before AADSTS650052 was visible anywhere. The message
  // was not missing; it was written somewhere guaranteed to be erased. Route all three outcomes
  // through the toast, which owns its own element and its own lifetime.
  function handleLoginReturn(){
    const q=new URLSearchParams(location.search);
    if(!q.has("login") && !q.has("linked")) return;
    if(q.get("login")==="error"){
      const raw=q.get("msg")||"";
      // AADSTS codes are the IdP's own wording and are what a Microsoft admin needs in order to
      // act, so they are KEPT - but lead with the sentence the user can act on themselves.
      const consent=/AADSTS(65001|650052|650057)/.test(raw);
      toast(consent
        ? "Your organization hasn't approved database access yet — an admin may need to approve it. "+raw
        : ("Sign-in didn't complete. "+raw).trim());
    }
    else if(q.has("linked")) toast("✓ "+(q.get("name")||"account")+
      " linked — queries against "+q.get("linked")+" stores now run as you.");
    else toast("✓ Signed in as "+(q.get("name")||"user")+
      " — queries now run as you, trimmed to your real permissions.");
    history.replaceState({},"",location.pathname);
  }

  function loadUsers(){
    if(!alive) return;
    fetch("/config").then(r=>r.json()).then(c=>{
      if(!alive) return;
      ENV_PRESENT=new Set(c.env_present||[]);    // #320
      CFG_OPERATOR = c.operator!==false;
      buildRail();                 // #551: operator-only services appear/disappear here
      const sel=document.getElementById("quser");
      const users=(c.users&&c.users.length)?c.users:["alice","bob"];
      const isOid=u=>/^[0-9a-f]{8}-[0-9a-f]{4}/i.test(u);   // real Entra OID → friendly label
      sel.innerHTML=users.map(u=>'<option value="'+esc(u)+'">'+(isOid(u)?"owner (SharePoint)":esc(u))+"</option>").join("");
    }).catch(()=>{
      document.getElementById("quser").innerHTML='<option value="alice">alice</option><option value="bob">bob</option>';
    });
  }
  function yamlHTML(){
    const m=manifest();
    let out='<span class="k">tenant</span>: <span class="str">'+m.tenant+'</span>\n<span class="k">stores</span>:\n';
    if(!m.stores.length) out+='<span class="cmt">  # add a source on the canvas to populate this manifest</span>\n';
    m.stores.forEach(s=>{
      out+='<span class="dash">  -</span> <span class="k">id</span>: <span class="str">'+esc(s.id)+'</span>\n';
      out+='    <span class="k">kind</span>: <span class="kind">'+esc(s.kind)+'</span>\n';
      out+='    <span class="k">business_unit</span>: <span class="str">'+esc(s.bu)+'</span>\n';
      out+='    <span class="k">acl</span>: [<span class="str">'+s.acl.map(esc).join(", ")+'</span>]\n';
      out+='    <span class="k">config</span>: { '+colorCfg(s.cfg)+' }\n';
      if(s.delegation) out+='    '+delegHTML(s.delegation)+'\n';
    });
    return out;
  }
  // Render the delegation block from its ACTUAL keys (#193) - an entra_refresh block and a
  // google_refresh block carry different fields (tenant_id vs resource, ${AUTH_*} vs ${GOOGLE_*}),
  // so hardcoding the Entra refs here would preview GCP stores with the wrong credentials.
  function delegHTML(d){
    const parts=Object.entries(d).map(([k,v])=>{
      const val=/^\$\{/.test(String(v))?'<span class="env">'+esc(v)+'</span>':'<span class="str">'+esc(v)+'</span>';
      return '<span class="k">'+esc(k)+'</span>: '+val;
    });
    return '<span class="k">delegation</span>: { '+parts.join(", ")+' }';
  }
  function delegText(d){
    return "delegation: { "+Object.entries(d).map(([k,v])=>k+": "+v).join(", ")+" }";
  }
  function colorCfg(cfg){
    return esc(cfg).replace(/(\$\{[^}]+\})/g,'<span class="env">$1</span>')
                   .replace(/([\w-]+):/g,'<span class="k">$1</span>:');
  }
  function yamlText(){
    const m=manifest();
    let out="tenant: "+m.tenant+"\nstores:\n";
    m.stores.forEach(s=>{
      out+="  - id: "+s.id+"\n    kind: "+s.kind+"\n    business_unit: "+s.bu+
        "\n    acl: ["+s.acl.join(", ")+"]\n    config: { "+s.cfg+" }\n";
      if(s.delegation) out+="    "+delegText(s.delegation)+"\n";
    });
    return out;
  }
  function syncYaml(){ if(document.getElementById("drawer").classList.contains("open")) document.getElementById("yaml").innerHTML=yamlHTML(); }

  /* ---------------- drawer ---------------- */
  function openDrawer(){
    document.getElementById("yaml").innerHTML=yamlHTML();
    document.getElementById("drawer").classList.add("open");
    document.getElementById("scrim").classList.add("open");
  }
  function closeDrawer(){
    document.getElementById("drawer").classList.remove("open");
    document.getElementById("scrim").classList.remove("open");
  }

  /* ---------------- right-click node menu ---------------- */
  const ctxmenu=document.getElementById("ctxmenu");
  function closeCtxMenu(){ ctxmenu.classList.remove("show"); }
  function openCtxMenu(node,x,y){
    // #923: the uploads node offers only Open and Delete. Rename/Duplicate/Test belong to
    // composed stores - a duplicated upload node would compose a providerless store, and its
    // "connection" is /admin/documents, which has nothing to test.
    if(node.derived){
      const dItems=[
        {ic:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.65 1.65 0 004.6 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 4.6a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06A1.65 1.65 0 0019.4 9c.14.63.63 1.1 1.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/>',label:"Open",fn:()=>{selected=node.uid;renderAll();}},
        {sep:true},
        {ic:'<path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m-1 0v14a2 2 0 01-2 2H8a2 2 0 01-2-2V6"/>',label:"Delete",danger:true,fn:()=>deleteNode(node)},
      ];
      return renderCtxMenu(dItems,x,y);
    }
    const items=[
      {ic:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.65 1.65 0 004.6 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 4.6a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06A1.65 1.65 0 0019.4 9c.14.63.63 1.1 1.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/>',label:"Configure",fn:()=>{selected=node.uid;renderAll();}},
      {ic:'<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4z"/>',label:"Rename",fn:()=>renameNode(node)},
      {ic:'<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 012-2h10"/>',label:"Duplicate",fn:()=>duplicateNode(node)},
      {ic:'<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',label:"Test connection",fn:()=>{selected=node.uid;renderAll();testConn(node);}},
      {sep:true},
      {ic:'<path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m-1 0v14a2 2 0 01-2 2H8a2 2 0 01-2-2V6"/>',label:"Delete",danger:true,fn:()=>deleteNode(node)},
    ];
    renderCtxMenu(items,x,y);
  }
  function renderCtxMenu(items,x,y){
    ctxmenu.innerHTML=items.map(it=> it.sep?'<div class="csep"></div>':
      '<div class="ci'+(it.danger?' danger':'')+'"><svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'+it.ic+'</svg>'+it.label+'</div>').join("");
    ctxmenu.classList.add("show");
    const mw=ctxmenu.offsetWidth, mh=ctxmenu.offsetHeight;
    ctxmenu.style.left=Math.max(8,Math.min(x,window.innerWidth-mw-8))+"px";
    ctxmenu.style.top=Math.max(8,Math.min(y,window.innerHeight-mh-8))+"px";
    const cis=ctxmenu.querySelectorAll(".ci"); let i=0;
    items.forEach(it=>{ if(it.sep) return; const el=cis[i++];
      el.onclick=ev=>{ ev.stopPropagation(); closeCtxMenu(); it.fn(); }; });
  }
  // #731: BOTH delete affordances (the panel's Remove and the context menu) go through
  // here. Delete used to be client-only - the stored manifest resurrected every node on the
  // next page load, and boot's composeUp() then re-committed the resurrected set. Now the
  // server owns deletion via DELETE /router/stores/{id} (row first, warm catalog second,
  // no rebuilds), and this function never shows a deletion the server refused.
  function removeNode(node){
    state=state.filter(s=>s.uid!==node.uid);
    if(selected===node.uid) selected=null;
    renderAll();
    if(isDemoMode()) return;               // the demo canvas is client-local by design
    // RAW fetch, not api(): api() returns a never-settling promise once the surface is
    // unmounted (#643) and cannot pass keepalive - but delete-then-navigate is exactly the
    // race #731 is about, and the DELETE must still reach the server.
    fetch("/router/stores/"+encodeURIComponent(node.id),
          {method:"DELETE", headers:idHeaders(), keepalive:true})
      .then(r=>{
        if(!r.ok) throw new Error("HTTP "+r.status);
        if(alive) undoToast(node);
      })
      .catch(e=>{
        if(!alive) return;                 // navigated away; the row is the truth either way
        state.push(node); renderAll();     // never show a deletion the server refused
        toast("Could not remove "+node.id+" - "+(e.message||e));
      });
  }
  // #731's revertibility half: deletion is non-destructive server-side (documents, ingest
  // jobs and secret handles survive; only catalog membership changes), so Undo is simply
  // the held node re-inserted and re-composed - the ordinary re-add path.
  function undoToast(node){
    if(!alive) return;
    let t=document.getElementById("undoToast");
    if(!t){ t=document.createElement("div"); t.id="undoToast"; t.className="toast";
            document.getElementById("canvas").appendChild(t); }
    t.innerHTML='Removed <b>'+esc(node.id)+'</b> <button class="btn" id="undoDel">Undo</button>';
    t.classList.add("show");
    t.querySelector("#undoDel").onclick=()=>{
      t.classList.remove("show");
      state.push(node); renderAll(); composeUp();
    };
    if(_undoT) clearTimeout(_undoT);
    _undoT=setTimeout(()=>t.classList.remove("show"), 8000);
  }
  let _undoT=null;
  // #923 (owner's rule): deleting the uploads NODE deletes the DATA - all the caller's own
  // uploaded documents, from the backend, for everyone they were shared with. That is a
  // destructive act, so it opens a confirm MODAL (the spPicker shell - the product's one
  // modal; never a NATIVE confirm(), #297: it freezes the canvas and the automation that
  // verifies it) that states the count before anything is sent. Documents OTHER people
  // shared with the caller are not theirs to delete and are left untouched. An EMPTY node
  // deletes like any node - nothing destructive to confirm.
  function deleteNode(node){
    if(node.kind==="upload"){
      const own=upDocsCache.filter(d=>d.shared_with_you!==true);
      if(!own.length){
        upNodeGone=true;
        state=state.filter(s=>s.uid!==node.uid);
        if(selected===node.uid) selected=null;
        renderAll(); saveCanvas();
        return;
      }
      openUploadNodeDeleteModal(node, own.length);
      return;
    }
    removeNode(node);
  }
  function openUploadNodeDeleteModal(node, ownCount){
    const modal=document.getElementById("spPicker");
    const body=document.getElementById("spPickerBody");
    const s=ownCount===1?"":"s";
    document.getElementById("spPickerTitle").textContent="Delete your documents?";
    body.innerHTML=
      '<div class="qmeta" style="color:var(--err,#c33);font-weight:600">Delete this node and '+
        'permanently delete '+ownCount+' uploaded document'+s+'?</div>'+
      '<div class="qmeta" style="margin:8px 0">They are removed from the database for everyone '+
        'they were shared with. This cannot be undone. Documents other people shared with you '+
        'are not touched.</div>'+
      '<div class="up-row" style="margin-top:12px"><button class="btn danger updoc-nodedel2">'+
        'Delete '+ownCount+' document'+s+'</button>'+
      '<button class="btn updoc-nodedel0">Keep</button></div>';
    modal.classList.add("show");
    body.querySelector(".updoc-nodedel0").onclick=()=>modal.classList.remove("show");
    const go=body.querySelector(".updoc-nodedel2");
    go.onclick=async ()=>{
      go.disabled=true; go.textContent="Deleting…";
      await reallyDeleteUploadNode(node);
      modal.classList.remove("show");
    };
  }
  async function reallyDeleteUploadNode(node){
    const own=upDocsCache.filter(d=>d.shared_with_you!==true);
    let failed=0;
    for(const d of own){
      try{ await api("/documents/"+encodeURIComponent(d.doc_external_id),{method:"DELETE"}); }
      catch(_){ failed++; }
    }
    upNodeGone=true;
    state=state.filter(s=>s.uid!==node.uid);
    if(selected===node.uid) selected=null;
    await syncDocumentsNode();   // truth check; the tombstone stops an auto-resurrect
    renderAll(); saveCanvas();   // the row's layout drops "your-documents" - durably gone
    toast(failed
      ? "Deleted "+(own.length-failed)+" of "+own.length+" documents - "+failed+" could not be deleted."
      : "Deleted the node and "+own.length+" document"+(own.length===1?"":"s")+".");
  }
  function duplicateNode(node){ seq++;
    const c={uid:"n"+seq,id:node.id+"-copy",kind:node.kind,bu:node.bu,acl:node.acl.slice(),
      config:Object.assign({},node.config),status:"draft",x:node.x+44,y:node.y+44};
    state.push(c); selected=c.uid; renderAll(); flashCenter(c.uid); }
  function renameNode(node){
    const el=world.querySelector('.node[data-uid="'+node.uid+'"] .nid'); if(!el) return;
    const inp=document.createElement("input"); inp.value=node.id;
    inp.style.cssText="width:132px;font:inherit;background:var(--panel-2);color:var(--text);"+
      "border:1px solid var(--k);border-radius:5px;padding:1px 5px";
    el.replaceWith(inp); inp.focus(); inp.select();
    inp.addEventListener("pointerdown",e=>e.stopPropagation());
    const commit=()=>{ const v=inp.value.trim(); if(v) node.id=v; renderAll(); };
    inp.addEventListener("keydown",e=>{ if(e.key==="Enter"){e.preventDefault();commit();}
      else if(e.key==="Escape"){renderAll();} });
    inp.addEventListener("blur",commit);
  }

  /* ---------------- misc ---------------- */
  function flashCenter(uid){
    const n=state.find(s=>s.uid===uid); if(!n) return;
    centerOn(n.x+106,n.y+60,null,true);   // node card ≈212×120 → center it, keep zoom
  }
  // #689: `esc`, `humanizeSnippet` and the Sources renderer now live in ui/proofs.js, so
  // /ask explains an answer exactly the way this surface does. They were MOVED, not copied -
  // see that module's header for why a second copy would be #689 one layer out.

  // #555: the model emits markdown bold and its OWN citation markers (【1†L4-L6】, the
  // OpenAI convention), and both used to reach the reader verbatim - so an answer read
  // "**18 weeks** ... 【1†L4-L6】" while a perfectly good [1] sat in the Sources list below.
  // Normalise the marker into the [n] footnote this canvas already resolves, keeping the
  // line range as the tooltip. Runs AFTER esc(), so the tags inserted here are the only
  // markup in the string and model output can never inject any of its own.
  // #724: `opt.attr` picks the marker namespace the footnote resolves in (the router half uses
  // data-fn, the document half data-dfn), and `opt.offset` continues the router's numbering
  // rather than restarting at [1] - see the fnCount note in askLive for why one screen must
  // not carry two meanings of [2].
  //
  // ORDER IS LOAD-BEARING, and it changed here. esc() still runs first, which is the entire
  // injection argument. But the \u3010n\u2020Lx\u3011 pass emits a literal "[n]" INSIDE the <sup> it builds,
  // and the plain-[n] pass used to run after it across the whole string - so it matched that
  // freshly-inserted text and wrapped a SECOND <sup> around it, leaving nested markup and two
  // click handlers on one marker. Plain [n] now runs first; the \u3010\u3011 form carries no brackets,
  // so it cannot be damaged by going second.
  function fmtAnswer(s,opt){
    const off=(opt&&opt.offset)||0, attr=(opt&&opt.attr)||"data-fn";
    const num=n=>String(Number(n)+off);
    const built=esc(s)
      // #751 round 2: `[^*]+` crossed newlines, so `**a\n\nb**` bolded across a PARAGRAPH break -
      // and _blockify then split the paragraphs, emitting <p><strong>a</p><p>b</strong></p>.
      // Unbalanced markup built out of model text, which no DOM read can show because the parser
      // repairs it before any test sees it. Emphasis stops at a blank line, as it does in
      // CommonMark: the asterisks then stay literal, which is honest about what the model wrote.
      .replace(/\*\*((?:(?!\n\s*\n)[^*])+)\*\*/g,'<strong>$1</strong>')
      .replace(/\[(\d+)\]/g,(m,n)=>'<sup class="fnref" '+attr+'="'+num(n)+'">['+num(n)+']</sup>')
      .replace(/\u3010\s*(\d+)\s*(?:\u2020([^\u3011]*))?\u3011/g,
               (m,n,loc)=>'<sup class="fnref" '+attr+'="'+num(n)+'" title="source '+num(n)+
                          ((loc&&loc.trim())?', '+esc(loc.trim()):'')+'">['+num(n)+']</sup>')
      // #729: the stranded terminal period. The model writes "... sixteen weeks \u30101\u2020L4-L6\u3011 ."
      // and the server's marker sweep can leave the same gap behind when it drops a dangling
      // one, so a full stop ends up floating a space clear of the sentence - and wraps onto a
      // line of its own often enough that the owner noticed it on prod. Closing the gap is
      // typographic only: no word, number or marker is touched.
      .replace(/(<\/sup>)\s+(?=[.,;:!?)])/g,"$1");
    return _blockify(built);
  }

  // #751: the model writes STRUCTURE and this surface threw all of it away. Measured on prod: a
  // 200-character compound answer carried five newlines, one blank line and three bullet lines,
  // and rendered as one run-on — "Freight costs by region are: - EMEA: … - APAC: … - AMER: …" —
  // with a bolded heading running inline straight after a citation marker. That inline collision
  // is a large part of why the #747 decline read as badly as it did.
  //
  // THE INJECTION ARGUMENT IS UNCHANGED, and it is the reason this runs LAST: every character
  // reaching here has already been through esc(), and the only markup in the string is the <sup>
  // and <strong> the passes above built. This adds a fixed whitelist — p, br, ul, li — and never
  // reflects model text into a tag or an attribute.
  //
  // A single-line answer returns byte-identical, so nothing that has no structure gains any.
  const _BULLET=/^\s*[-–—*•]\s+/;
  // #764: models emit numbered lists at least as often as dashed ones, and "1." reaching the
  // reader inside a run-on paragraph is the same defect #751 fixed for "-". Both `1.` and `1)`.
  //
  // #793: this was `\d+`, and the marker is DELETED at the push below - so any line opening
  // with a number lost it. "History:\n2008. Revenue was 4.2M" rendered as an <ol> whose item
  // read "Revenue was 4.2M": the year was gone from the screen, replaced by a counter reading
  // "1.". Years, page numbers, citation numbers and figure numbers all open lines in real
  // answers. I guarded the heading analogue here (a bare "#hashtag" is correctly not a heading)
  // and never guarded the numeric one.
  //
  // Two independent narrowings, because either alone still loses content:
  //   * AT MOST TWO DIGITS. A four-digit number is a year far more often than a list index, and
  //     an answer panel 240px wide does not carry hundred-item lists. `\d{1,2}` still reaches
  //     item 99, so a list breaking at 10 stays a real regression a fixture can catch.
  //   * THE RUN MUST HAVE TWO ITEMS. A single numbered line with no sibling is prose. This is
  //     what saves a three-digit page number, and it fails SAFE: an unrecognised run keeps its
  //     marker as visible text rather than having it deleted.
  // The capture group carries the start number, which used to be discarded - see flushList.
  const _NUMBERED=/^\s*(\d{1,2})[.)]\s+/;
  // #764: an ATX heading. Without this the hashes themselves reach the reader ("## Freight
  // costs"), which is worse than no heading at all. Rendered as a bold line rather than an <h*>:
  // this sits inside a ~240px panel with its own type scale, and a real heading element would
  // need a size of its own to not compete with the surface around it. The SEMANTICS are
  // therefore not carried - a screen reader gets emphasis, not a heading level - and that is a
  // deliberate limit, not an oversight.
  const _HEADING=/^\s*#{1,6}\s+/;
  // A line that is indented and carries content — the shape of a WRAPPED bullet.
  const _INDENTED=/^\s+\S/;
  function _blockify(html){
    // #751 round 2: this early return is what makes "an answer with no structure returns
    // byte-identical" true, and it was testing the raw string - so a single TRAILING newline,
    // which models emit constantly, sent a plain sentence down the block path and wrapped it in a
    // <p> it never had. Surrounding whitespace is not structure.
    if(!/\n/.test(html.trim())) return html;
    const out=[];
    for(const block of html.split(/\n\s*\n/)){
      // NOT trimmed here: the indentation is the only thing that distinguishes a wrapped bullet
      // from a new paragraph, and trimming first destroyed it before anything could look.
      const lines=block.split("\n").filter(l=>l.trim());
      if(!lines.length) continue;
      let buf=[], items=[], tag="ul", start=null;
      // #793: a numbered line is only a LIST ITEM if it has a sibling. One "2008." on its own is
      // a sentence that happens to begin with a year, and the marker below would delete it.
      const ordered=lines.filter(l=>{ const t=l.trim();
        return !_BULLET.test(t)&&_NUMBERED.test(t); }).length>=2;
      const flushText=()=>{ if(buf.length){ out.push("<p>"+buf.join("<br>")+"</p>"); buf=[]; } };
      const flushList=()=>{ if(items.length){
        // #793: the model's own start number used to be dropped, so "3. third / 4. fourth"
        // rendered as "1. / 2." - the counter silently contradicting the answer's own text.
        const at=(tag==="ol"&&start&&start!==1)?' start="'+start+'"':"";
        out.push("<"+tag+at+">"+items.map(i=>"<li>"+i+"</li>").join("")+"</"+tag+">");
        items=[]; start=null; } };
      for(const line of lines){
        const t=line.trim();
        // A leading dash or number is a LIST MARKER, not content, so it goes — leaving it would
        // print the bullet twice once the <li> supplies its own. #764: an ORDERED run and an
        // unordered one cannot share a list, so a change of kind closes the open one first.
        const bullet=_BULLET.test(t), numbered=!bullet&&ordered&&_NUMBERED.test(t);
        if(bullet||numbered){
          const want=bullet?"ul":"ol";
          if(items.length&&tag!==want) flushList();
          tag=want; flushText();
          if(numbered&&!items.length) start=parseInt(t.match(_NUMBERED)[1],10);
          items.push(t.replace(bullet?_BULLET:_NUMBERED,""));
        }
        // #751 round 2: an indented non-bullet line under a bullet is that bullet's CONTINUATION,
        // which the model writes whenever an item runs long. Treating it as body text flushed the
        // list and started a second one, so a single three-item list rendered as two lists with a
        // stray paragraph wedged between them.
        else if(items.length && _INDENTED.test(line)){ items[items.length-1]+=" "+t; }
        // #764: the hashes are the marker, and they are not content either.
        else if(_HEADING.test(t)){
          flushList(); flushText();
          out.push("<p><strong>"+t.replace(_HEADING,"")+"</strong></p>");
        }
        else { flushList(); buf.push(t); }
      }
      flushList(); flushText();
    }
    return out.join("")||html;
  }

  // SharePoint OAuth returns to /canvas?connector=sharepoint&tenant=… (or &error=…). Show the
  // outcome on the canvas and mark the SharePoint node connected, then clean the URL.
  function handleSpReturn(){
    const q=new URLSearchParams(location.search);
    if(q.get("connector")!=="sharepoint") return;
    const toast=document.getElementById("spToast"); if(!toast) return;
    if(q.get("error")){
      toast.classList.add("err");
      toast.innerHTML="⚠ SharePoint connect failed: "+esc(q.get("error"));
    } else {
      const t=(q.get("tenant")||"").slice(0,8);
      toast.innerHTML="✓ <b>SharePoint connected</b>"+(t?" · tenant "+esc(t)+"…":"")+
        " — pick a library to ingest, then ask below.";
    }
    toast.classList.add("show");
    setTimeout(()=>toast.classList.remove("show"),9000);
    history.replaceState({},"",location.pathname);   // clean the URL, keep them on the canvas
  }

  function currentTheme(){
    const t=document.documentElement.getAttribute("data-theme");
    if(t) return t;
    return window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";
  }

  /* ---------------- wire up ---------------- */
  buildRail();
  loadUsers();
  state=[]; state.tenant="acme";
  // #279: decide the mode from the auth state FIRST, then load the right catalog. In demo mode
  // (real login configured, not signed in) loadLiveDemo would 403 on compose/principals - so we
  // branch to the read-only demo fleet instead.
  refreshAuth().then(bootCanvas);   // bootCanvas's loader sets the view (fitView / centerOn)
  renderAll();
  handleSpReturn();   // if we just returned from Microsoft consent, reflect it on the canvas
  handleLoginReturn();   // #171: if we just returned from Microsoft sign-in, reflect it

  document.getElementById("export").onclick=openDrawer;
  document.getElementById("closeDrawer").onclick=closeDrawer;
  document.getElementById("scrim").onclick=closeDrawer;
  document.getElementById("reset").onclick=()=>loadLiveDemo({fresh:true});   // #199 escape hatch
  document.getElementById("setupChat").onclick=toggleSetup;
  document.getElementById("compose").onclick=composeUp;
  document.getElementById("qask").onclick=()=>askLive();
  document.getElementById("qroute").onclick=routeLive;
  document.getElementById("qtext").addEventListener("keydown",e=>{ if(e.key==="Enter") askLive(); });
  // #818: Cmd/Ctrl+S = flush the workspace save NOW, with feedback. The browser's
  // save-page dialog helps nobody on a canvas, so it is suppressed in every mode.
  on(document,"keydown",e=>{
    if(!(e.metaKey||e.ctrlKey) || String(e.key).toLowerCase()!=="s") return;
    e.preventDefault();
    if(isDemoMode()){ toast("Demo canvas - sign in to save a workspace of your own"); return; }
    if(!isLiveUser()){ saveCanvas(); toast("Saved locally"); return; }   // dev rig: localStorage IS the store
    flushRowSave().then(ok=>{ if(ok&&alive) toast("Workspace saved"); });
  });
  // #818: a pending debounced save must survive the user leaving - pagehide covers the
  // hard-refresh/close race (keepalive carries the request out), unmount covers routing.
  on(window,"pagehide",()=>{ flushRowSave(); });
  // #643: the theme BUTTON is gone - the account control owns the toggle for every surface -
  // but the canvas still has to repaint, because node colours are resolved at render time and
  // would otherwise keep the old palette until something else forced a render. Observing the
  // attribute rather than being called by the toggle keeps the two sides unaware of each other:
  // anything that flips data-theme, including the pre-paint script in <head>, is honoured.
  const themeWatch = new MutationObserver(() => renderAll());
  themeWatch.observe(document.documentElement,
                     { attributes: true, attributeFilter: ["data-theme"] });
  document.getElementById("copyYaml").onclick=()=>{
    const txt=yamlText(), btn=document.getElementById("copyYaml");
    const done=()=>{ btn.textContent="Copied ✓"; setTimeout(()=>btn.textContent="Copy manifest",1400); };
    if(navigator.clipboard&&navigator.clipboard.writeText){ navigator.clipboard.writeText(txt).then(done).catch(done); }
    else done();
  };
  // #880: the picker joins ask.js and admin.js on the ONE dialog contract (ui/modal.js) - it
  // hand-rolled two of the three dismissal routes and had no focus trap, while declaring
  // nothing in markup. Dismissal is deliberately permissive even mid-crawl: the ingest is a
  // durable server-side job (LAW 4), so closing the window is not cancelling anything, and the
  // watcher keeps running to update the node. What is NOT permissive is the reverse - the CODE
  // never closes this dialog on a completed run. That is the whole of #880.
  (function(){ const p=document.getElementById("spPicker");
    document.getElementById("spPickerClose").onclick=closeSpPicker;
    wireModalHost(p, {isOpen:()=>p.classList.contains("show"), onDismiss:closeSpPicker}); })();
  // The three that reach outside the surface, and so the three that have to be given back.
  // (The picker's own Escape is wireModalHost's, above.)
  on(window,"keydown",e=>{ if(e.key==="Escape"){ closeDrawer(); closeCtxMenu(); closeProvMenu(); } });
  on(window,"resize",drawEdges);
  // click anywhere outside the context menu closes it
  on(document,"pointerdown",e=>{ if(!e.target.closest(".ctxmenu")) closeCtxMenu(); },true);

  /**
   * Take the surface back down. The router calls this before mounting the next one.
   *
   * Order matters: stop the observer and the listeners BEFORE removing the DOM, so nothing
   * fires against a tree that is halfway gone.
   */
  return function unmountCanvas() {
    flushRowSave();                      // #818: BEFORE alive drops - the keepalive PUT
                                         // rides out the teardown; its callbacks are inert.
    alive = false;                       // #643: FIRST - see the note on `alive` above
    themeWatch.disconnect();
    for (const off of offs) off();
    offs.length = 0;
    for (const t of timers) clearInterval(t);
    timers.clear();
    root.classList.remove("surface--bleed");
    host.remove();
  };
}
