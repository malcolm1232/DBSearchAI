// src/dbsearch/server/static/js/ui/proofs.js
//
// The SOURCES rail - where a routed answer says what it was built from (#689, ADR 0025).
//
// MOVED HERE FROM canvas.js, not copied, and the ADR is explicit about which of those two it
// wanted: "the canvas components are the donor, moved to a shared module rather than
// duplicated". The reason is the defect this whole card is about. /ask and /canvas answering
// the same question differently is #689; /ask and /canvas EXPLAINING the same answer
// differently would be the same bug one layer out, and two copies of a renderer is how that
// starts. Every comment below travelled with its code, because the reasoning is the part worth
// keeping - #729's three honesty rules, #748's word-boundary cut, #177's actions.
//
// This module owns MARKUP STRINGS rather than DOM nodes, which is the canvas's idiom and not
// ask.js's (ui/components.js builds nodes through `el`). That is deliberate: a faithful move
// keeps the canvas byte-identical, and rewriting the renderer into `el` calls on the way past
// would be a redesign wearing a refactor's clothes - with no way to tell, if the canvas then
// looked different, whether the move or the rewrite did it. ask.js hosts the string in one
// container it owns; if the two idioms ever need reconciling, that is its own card.
//
// EVERYTHING HERE IS ESCAPED AT THE BOUNDARY. `esc` runs on every value that reaches the
// markup, because these strings carry MODEL OUTPUT and DATABASE VALUES - #786's lesson, and
// the reason the canvas's own guards assert on what the DOM built rather than on how the text
// looks.

export function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  }[c]));
}

// #729: make an evidence snippet readable WITHOUT changing what it says.
//
// ONE RULE SURVIVES, AND THE ONE THAT DIED IS THE INSTRUCTIVE PART.
//
// The removed rule added thousands separators to any value carrying a decimal point, on the
// theory that identifiers are integers and measures carry decimals. An independent review
// falsified it in the shape it was written to guarantee: `app_version=2024.1.0` rendered as
// `2,024.1.0`, `period=2024.06` as `2,024.06`, `po=PO-12345.67` as `PO-12,345.67`. Every
// narrower version leaks too — requiring a value position keeps `period=2024.06`; forbidding
// a trailing dot-group keeps `PO-12345.67` — because `2024.06` and `1200.50` are the SAME
// SHAPE. Nothing in the snippet distinguishes them; only the column's TYPE does, and the
// evidence payload does not carry it (carded).
//
// So it is gone rather than narrowed. This text sits in the Sources rail, whose entire job is
// to be checkable against the source, and a value the reader cannot paste back into a query
// is a worse defect than an ugly one. Prettier evidence is not worth evidence that lies.
//
// WHAT REMAINS: drop a midnight time from a date, and ONLY when no zone or offset follows it.
// `2008-06-01 00:00:00` is a driver artefact on a DATE column. `2008-06-01 00:00:00+05:30` is
// not — it is a real instant, and the first cut printed it as a bare date with the offset
// stranded (`2008-06-01+05:30`), which states something false about the row. Anything
// carrying `+hh`, `-hh` or a zone name is left exactly as it arrived.
//
// No lookbehind: it was the only one in the shipped JS, and an engine without support fails
// to PARSE canvas.js, so `mountCanvas` never exists and the canvas is blank rather than
// degraded. A whole-surface failure mode is too much to risk for cosmetics.
// #729(a): THE RULE THAT DIED IS BACK, AND IT IS THE SERVER THAT REVIVED IT.
//
// The paragraph above ends "only the column's TYPE does, and the evidence payload does not
// carry it". It carries it now: `footnote.column_types` maps each column this query returned
// to how its values should be READ - "num", "date" or "ts" - derived server-side from the
// DECLARED information_schema type, which is knowable there and nowhere here. So the two
// shapes that could not be told apart are now told apart by something other than their shape:
//
//     period=2024.06        NUMERIC in name only - a `date`/text column -> untouched
//     price=1200.50         a decimal column                            -> 1,200.50
//     app_version=2024.1.0  not a number at all, and no strict match    -> untouched
//
// Three properties keep this honest, and each is a guard below:
//   - INTEGERS ARE NEVER GROUPED. No type separates a count from an identifier, so only
//     FRACTIONAL types are classed "num" and `customer_id=29485` stays as it is.
//   - NO CURRENCY IS INVENTED. `43962.7901` becomes `43,962.7901`, never `$43,962.79`: the
//     symbol is not in the payload and the digits are not ours to round.
//   - AN UNKNOWN COLUMN RENDERS RAW. An alias the schema cannot resolve ("SUM(x) AS total"),
//     a name that is numeric in one table and text in another, a document snippet with no
//     fields at all - none of them appear in the map, and no map means exactly the behaviour
//     this function had before. Being wrong about a type costs a formatting opportunity here,
//     never a value.
//
// A `ts` column is now left ALONE rather than midnight-stripped, which is a correctness fix
// the blind rule could not make: on a real timestamp, midnight is data.
const _FIELD=/(^|,\s)([A-Za-z_]\w*)=([^,]*)/g;
const _MIDNIGHT=/(\d{4}-\d{2}-\d{2})[ T]00:00:00(?:\.0+)?(?![\d:+\-]|\s*[A-Za-z]{2,5}\b)/g;
function _group(v){
  const m=/^(-?)(\d+)(\.\d+)?$/.exec(v);
  if(!m) return v;                       // not a plain number - leave it entirely alone
  return m[1]+m[2].replace(/\B(?=(\d{3})+(?!\d))/g,",")+(m[3]||"");
}
export function humanizeSnippet(s,types){
  const t=types||{};
  const out=esc(String(s||""));
  // No types known: byte-for-byte the behaviour described above, which is what every
  // document citation and every unresolvable store still gets.
  if(!Object.keys(t).length) return out.replace(_MIDNIGHT,"$1");
  return out.replace(_FIELD,(m,sep,col,val)=>{
    const cls=t[col]||t[col.toLowerCase()]||"";
    if(cls==="ts") return sep+col+"="+val;                 // midnight is data here
    if(cls==="date") return sep+col+"="+val.replace(/^(\d{4}-\d{2}-\d{2})[ T]00:00:00(?:\.0+)?$/,"$1");
    if(cls==="num") return sep+col+"="+_group(val);
    return sep+col+"="+val.replace(_MIDNIGHT,"$1");        // untyped: the old rule, unchanged
  });
}


// #177/#729: ONE numbered Sources block, from the server's `footnotes`.
//
// Returns markup, or "" when there is nothing to show - and "" rather than an empty shell is
// load-bearing on both surfaces: a "Sources" heading with no sources under it is a provenance
// claim with nothing behind it, which is exactly what #724 removed from the canvas.
export function sourcesBlockHTML(footnotes) {
  const fns = footnotes || [];
  if (!fns.length) return "";
  return '<div class="sources"><div class="shdr">Sources — where this answer came from</div>'
    + fns.map((f) => {
      const sys = f.system || (f.origin || "").split(" · ")[0] || "Source";
      const tag = f.kind === "sql" ? "query" : (f.kind === "document" ? "document" : "source");
      let acts = "";
      if (f.kind === "sql" && f.rerun_token) {
        acts = '<button class="sbtn sverify" data-fn="' + f.n + '">✓ Verify data</button>'
             + '<button class="sbtn squery" data-fn="' + f.n + '">&lt;/&gt; Show query</button>';
      } else if (f.kind === "document" && f.uri) {
        acts = '<a class="sbtn" href="' + esc(f.uri) + '" target="_blank" rel="noopener">↗ Open source</a>';
      }
      return '<div class="src" id="fn' + f.n + '">'
        + '<div><span class="snum">[' + f.n + ']</span> <span class="ssys">' + esc(sys) + '</span>'
          + '<span class="stag">' + esc(tag) + '</span></div>'
        // #729: location is the STORE (host / database); object is what inside it answered (a
        // table, a document). Without the second, three citations from one query render as
        // three identical cards.
        + ((f.location || f.object)
            ? '<div class="sloc">' + esc(f.location || "")
              + ((f.location && f.object) ? " · " : "") + esc(f.object || "") + "</div>"
            : "")
        + '<div class="osnip">' + humanizeSnippet(f.snippet, f.column_types) + "</div>"
        + (acts ? '<div class="sacts">' + acts + "</div>" : "")
        + '<div class="sout" id="so' + f.n + '"></div>'
      + "</div>";
    }).join("") + "</div>";
}


// The CHAT framing of the same rail: collapsed behind a one-line summary, actions wired.
//
// It lives here rather than in ask.js because the summary label and the collapse ARE part of
// this rail's design, not the calling surface's - and because selftest_622 pins, correctly,
// that no surface builds its own "Sources" heading. A surface that assembled the wrapper
// itself would be re-creating the drift that test exists to catch, one element out from the
// block it was factored to prevent.
//
// #629 is why the chat variant is collapsed and the canvas's is not: the canvas shows one
// answer, a thread shows ten, and ten open SQL rails is a wall.
//
// Returns null when there is nothing to show, so a caller can branch on the rail's existence
// exactly as it branches on `sourcesBlockHTML`'s empty string.
export function collapsibleSourcesRail(footnotes, rerun) {
  const fns = footnotes || [];
  if (!fns.length) return null;
  const host = document.createElement("details");
  host.className = "ask-proofs proof-host";
  const summary = document.createElement("summary");
  const tri = document.createElement("span");
  tri.className = "tri";
  tri.setAttribute("aria-hidden", "true");
  tri.textContent = "▸";
  summary.append(tri, document.createTextNode(`Sources (${fns.length})`));
  const body = document.createElement("div");
  body.innerHTML = sourcesBlockHTML(fns);
  host.append(summary, body);
  wireProofActions(host, fns, rerun);
  return host;
}


// #177: the two explanatory actions - "Show query" reveals the SQL, "Verify data" re-runs it
// live against the source under the CALLER's own guards (/router/rerun re-checks gate #1 and
// the token's binding, so this button can never reach a store the clicker cannot see).
//
// `rerun` is ONE INJECTED OPERATION, not a fetch client: `({store_id, sql, token}) ->
// Promise<{cols, rows, count, capped}>`. The canvas and the shell reach the network through
// different wrappers (the canvas has a generic `api(path, opts)`, api.js exposes named calls),
// and a module that imported either would work on one surface and fail on the other. Narrowing
// the injection to the single call it makes means neither surface has to expose a client shaped
// for this module's convenience.
//
// Idempotent per element: handlers are ASSIGNED (`onclick=`), not added, so re-rendering a
// block into the same container cannot accumulate listeners.
export function wireProofActions(root, footnotes, rerun) {
  const byId = {};
  (footnotes || []).forEach((f) => { byId[f.n] = f; });
  root.querySelectorAll(".squery").forEach((b) => {
    b.onclick = () => {
      const f = byId[b.dataset.fn];
      const o = root.querySelector("#so" + b.dataset.fn);
      if (!f || !o) return;
      o.innerHTML = o.innerHTML ? "" : "<pre>" + esc(f.sql) + "</pre>";
    };
  });
  root.querySelectorAll(".sverify").forEach((b) => {
    b.onclick = () => {
      const f = byId[b.dataset.fn];
      const o = root.querySelector("#so" + b.dataset.fn);
      if (!f || !o) return;
      o.innerHTML = '<div class="qmeta">verifying against the live source…</div>';
      rerun({ store_id: f.store_id, sql: f.sql, token: f.rerun_token })
        .then((x) => {
          const head = "<tr>" + x.cols.map((c) => "<th>" + esc(c) + "</th>").join("") + "</tr>";
          const rows = x.rows.slice(0, 10).map(
            (rw) => "<tr>" + rw.map((v) => "<td>" + esc(String(v)) + "</td>").join("") + "</tr>"
          ).join("");
          o.innerHTML = '<div class="qmeta">✓ verified live · ' + x.count + " row"
            + (x.count === 1 ? "" : "s")
            + (x.capped ? " (first " + x.rows.length + " shown)" : "")
            + " returned now</div><table>" + head + rows + "</table>";
        })
        .catch((e) => { o.innerHTML = '<div class="qerr">' + esc(e.message || e) + "</div>"; });
    };
  });
}
