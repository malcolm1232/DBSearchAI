// src/dbsearch/server/static/js/ui/components.js
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children) node.append(c?.nodeType ? c : document.createTextNode(String(c)));
  return node;
}

function safeHref(uri) {
  return /^https?:\/\//i.test(uri || "") ? uri : null;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** The line under an answer saying what it was drawn from (#393).
 *
 * This lives here because it used to live in three places. Ask, Chat and Draft each owned
 * their own copy of the sentence, all three read the RETRIEVAL list, and all three printed
 * it as "documents you can access" - so one wrong idea got told three times, and fixing it
 * in one surface would have left the other two lying.
 *
 * Two numbers, never conflated: `retrieved` is how many documents this answer drew on;
 * `corpus.authorized_docs` is how many the caller may see at all. The four states below are
 * genuinely different situations and a user acts differently in each, so they get different
 * sentences rather than one that fits none of them:
 *
 *   nothing indexed      -> a "connect a source" problem, NOT a permissions one. Saying
 *                           "0 documents you can access" here is the #392/#393 bug: it sent
 *                           an operator hunting for an access fault that did not exist.
 *   indexed, none yours  -> a real permissions statement, and the one case where saying so
 *                           is correct.
 *   yours, none matched  -> retrieval found nothing; the user's access is not the issue and
 *                           the copy must not imply it is.
 *   answered             -> both numbers, stated as what they are.
 *
 * `corpus` is null when the backend cannot count. Then we report retrieval only and make no
 * claim about entitlement at all - an unmeasured "0 you can access" is the original lie.
 *
 * `as` names the identity whose permissions applied. It is woven into the sentence rather
 * than appended after it, so the two never collide into "...you can access. · as malcolm",
 * and it is dropped where it would be noise (nothing indexed is true for everyone).
 */
/* #555: render an answer as PROSE, not as the model's raw output.
 *
 * Measured on the live site, in the first answer the new upload flow produced:
 *
 *   Primary carers receive **18 weeks** of fully paid parental leave, and the daily meal
 *   allowance while travelling is **65 euros** 【1†L4-L6】 【1†L9-L10】
 *
 * Two defects in one line. The asterisks are markdown the surface printed literally, and
 * 【1†L4-L6】 is the model's own citation-marker convention passed straight through - so the
 * reader saw two citation systems at once and one of them resolved to nothing on screen,
 * while a perfectly good "[1] hr-leave-policy.txt" sat in the Sources block below.
 *
 * The marker carries real information (L4-L6 is a line range), so it is PARSED rather than
 * stripped: it becomes the same [n] footnote the Sources block uses, and the line range
 * survives as the element's title. Built as DOM nodes, never innerHTML - this is model
 * output rendered inside an authenticated page, and it must not be able to inject markup.
 */
const _CITE = /【\s*(\d+)\s*(?:†([^】]*))?】/g;   // 【1†L4-L6】 or bare 【1】 → [1]

/* #893: the same text, for the one place it is shown BEFORE the server has swept it.
 *
 * The streamed answer is written to the screen token by token as it arrives, as plain text,
 * and only the FINAL string goes through answerNodes above. So for the whole length of a
 * generation the reader is looking at the model's raw output - and gpt-oss cites natively as
 * 【9†L1-L4】. That is exactly the string the owner read back off prod on 260820: a chunk
 * index and a line range, in a format nothing on the page can resolve.
 *
 * Deliberately TEXT, not nodes. The preview is redrawn on every token, and rebuilding a
 * fragment of <button> controls that many times is work the reader never benefits from - the
 * real, clickable control arrives moments later with the final render. What this owes the
 * reader is only that the preview speaks the product's own language: 【9†L1-L4】 reads as
 * [9], a shape nothing can render reads as nothing.
 *
 * `_TRAILING_PARTIAL` is the streaming-specific half: a marker arrives in pieces, so at some
 * token the text really does end "…confirmed【9†L1-" with the closing bracket still in
 * flight. Rendering that would flash the very vocabulary this strips. It is held back until
 * it completes, which costs the reader nothing - a marker is meaningless until it closes.
 */
const _FOREIGN_CITE = /\s?【(?!\s*\d+\s*(?:†[^】]*)?】)[^】]*】|\s?\ue200[^\ue201]*\ue201|[\ue200-\ue206]/g;
const _TRAILING_PARTIAL = /(\s?【[^】]*|\s?\ue200[^\ue201]*)$/;

export function previewText(raw) {
  return String(raw || "")
    .replace(_TRAILING_PARTIAL, "")
    .replace(_FOREIGN_CITE, "")
    .replace(_CITE, (_m, n) => `[${n}]`);
}
/* #629: THE PLAIN `[n]` SPELLING TOO, and this is not a cosmetic addition.
 *
 * Two spellings reach a reader as a footnote. 【n†Lx-Ly】 is the model's own convention, which
 * this function already turned into a superscript. `[n]` is what the SERVER leaves in the
 * prose - it is the spelling the synthesis prompt asks for ("Cite passages by their [n]
 * markers"), and it was rendered as literal text. So on a real answer - "...accrue 30 days
 * [1]." is the owner's own screenshot - the marker beside the claim was inert while a
 * different spelling elsewhere was interactive.
 *
 * `citedMarkers` has read both spellings since #622, which is exactly the asymmetry: the
 * product already COUNTED a `[n]` as a citation when deciding whether a source was pointed
 * at, while refusing to render it as one. Matching them here makes the two agree.
 *
 * Safe against numbers that are not citations, and the guarantee is the server's, not a
 * heuristic here: `_drop_dangling_markers` deletes every `[n]` outside 1..N from the answer
 * before it is sent, precisely because the model picks numbers out of document content
 * ("4. PUBLIC HOLIDAYS" became "[4]"). Whatever survives IS a citation by construction.
 */
const _CITE_PLAIN = /\[(\d+)\]/g;
const _BOLD = /\*\*([^*]+)\*\*|__([^_]+)__/g;

export function answerNodes(raw) {
  const frag = document.createDocumentFragment();
  String(raw || "").split("\n").forEach((line, i, all) => {
    if (i) frag.append(el("br", {}));
    let cursor = 0;
    // One pass over the line, taking whichever marker comes next.
    const marks = [];
    for (const m of line.matchAll(_CITE)) marks.push({ i: m.index, len: m[0].length, kind: "cite", n: m[1], loc: m[2] });
    for (const m of line.matchAll(_CITE_PLAIN)) marks.push({ i: m.index, len: m[0].length, kind: "cite", n: m[1] });
    for (const m of line.matchAll(_BOLD)) marks.push({ i: m.index, len: m[0].length, kind: "bold", text: m[1] || m[2] });
    marks.sort((a, b) => a.i - b.i);
    for (const mk of marks) {
      if (mk.i < cursor) continue;                       // overlapping match, already consumed
      if (mk.i > cursor) frag.append(document.createTextNode(line.slice(cursor, mk.i)));
      if (mk.kind === "bold") {
        frag.append(el("strong", {}, mk.text));
      } else {
        // #629: a CONTROL, not a decoration. The marker is the reader's natural place to ask
        // "says who?" - it is right beside the claim - so it opens the sources panel focused
        // on the source it names. A <sup> could not be reached by keyboard and announced
        // nothing; a <button> is both. The surface delegates the click, because this function
        // is pure and knows nothing about which citations belong to which answer.
        const loc = (mk.loc || "").trim();
        frag.append(el("button", {
          type: "button", class: "cite-ref", "data-cite": mk.n,
          ...(loc ? { "data-loc": loc, title: `source ${mk.n}, ${loc}` }
                  : { title: `source ${mk.n}` }),
          "aria-label": `Show source ${mk.n}`,
        }, `[${mk.n}]`));
      }
      cursor = mk.i + mk.len;
    }
    if (cursor < line.length) frag.append(document.createTextNode(line.slice(cursor)));
  });
  return frag;
}

export function provenanceNote({ retrieved = 0, corpus = null,
                                 verb = "Answered from", as = "" } = {}) {
  const note = (...kids) => el("div", { class: "authorized-note" }, ...kids);
  const sentence = (body) => note(`${body}${as ? `, as ${as}` : ""}.`);

  if (!corpus) {
    return retrieved ? sentence(`${verb} ${plural(retrieved, "document")}`)
                     : sentence("No matching documents");
  }
  // #937: RETRIEVAL DISPROVES THE DENOMINATOR. `corpus` counts the uploaded-document index,
  // and a connector store builds its own (router/providers/connector.py:
  // `index = InMemoryIndex(obj)`), so a caller whose only source is a Drive folder gets
  // citations alongside `indexed: false`. That pairing is not a state to describe - it is two
  // planes disagreeing, and only one of them is on screen. Report retrieval; claim nothing
  // about entitlement.
  //
  // ONE clause, because the other one is dead. The first shape was
  // `!corpus.indexed || !corpus.authorized_docs`, and corpus_status derives `indexed` as
  // `bool(total)` over the same scan that counts `authorized_docs` - so nothing indexed always
  // means nothing authorized, and the left half can never decide anything alone. Mutation
  // testing is what said so: deleting it changed no test.
  //
  // Placed ABOVE both no-source sentences, not beside one. `indexed:false` reaches "there was
  // nothing to search" and `authorized_docs:0` reaches "none of them are shared with you yet";
  // both are false above a list of sources, and fixing only the first is the near-miss.
  // `corpus_status` itself is deliberately untouched - it feeds the #392/#393 permission
  // surfaces, and this only decides which sentence prints.
  if (retrieved && !corpus.authorized_docs) {
    return sentence(`${verb} ${plural(retrieved, "document")}`);
  }
  // #937 round 2: nothing retrieved, and the document index is empty - but this caller has a
  // source composed. "Connect a source to get started" is then advice to do a thing they have
  // already done, and prod printed it directly under an answer reading "That query ran against
  // your data and matched no records. The source is there and readable." The answer and the
  // note contradicted each other in adjacent lines.
  //
  // `connected_sources` is undefined for the anonymous link visitor, who has no workspace to
  // count - and undefined is falsy here, so that path keeps exactly the sentence it had.
  if (!corpus.indexed && corpus.connected_sources) {
    return sentence("Nothing you can access matched this question");
  }
  if (!corpus.indexed) {
    return note("No documents are indexed yet, so there was nothing to search. ",
      el("a", { href: "/canvas" }, "Connect a source"), " to get started.");
  }
  const entitled = corpus.authorized_docs || 0;
  if (!entitled) {
    return note("None of the indexed documents are shared with you yet. "
      + "Ask whoever administers your sources for access.");
  }
  if (!retrieved) {
    return sentence(`Nothing in the ${plural(entitled, "document")} you can access `
      + "matched this question");
  }
  return sentence(`${verb} ${retrieved} of the ${plural(entitled, "document")} you can access`);
}

// One ranked source card. `c` = {doc, title, uri}. Deep-links to the source doc when uri is set.
//
// #622: `cited` says whether the ANSWER points at this card's number. It defaults to true so
// that a caller which knows nothing about markers gets the plain rendering, and the only
// thing a `false` adds is a muted line saying so - never a hidden row. See
// `buildSourcesPanel` for why an uncited source is still listed rather than filtered out.
// (#629 moved that reasoning there from the old `sourcesRail`, which is gone; the panel now
// groups uncited rows under a heading instead of captioning each one, so this branch is
// currently unused by the panel and kept for any caller that wants a standalone card.)
export function sourceCard(num, c, { cited = true } = {}) {
  const href = safeHref(c.uri);
  const inner = el("span", {},
    el("span", { class: "num" }, `[${num}]`),
    el("span", { class: "title" }, c.title || c.doc),
  );
  const card = el(href ? "a" : "div",
    href ? { class: "source-card", href: href, target: "_blank", rel: "noopener" }
         : { class: "source-card" },
    inner);
  if (href) card.append(el("div", { class: "uri" }, href));
  if (!cited) {
    card.append(el("div", {
      class: "source-uncited",
      title: "This document was retrieved and given to the model as context for this "
           + "question. The answer carries no [" + num + "] marker, so nothing in it is "
           + "attributed to this document.",
    }, "not cited in this answer"));
  }
  return card;
}

/* #622: which source numbers does this answer actually POINT AT?
 *
 * Both marker spellings count, because both reach a reader as a footnote: `[n]` is what the
 * server leaves in place (`QueryService._drop_dangling_markers` has already deleted anything
 * outside 1..N), and 【n†Lx-Ly】 is the model's own convention, which `answerNodes` above
 * renders as the same [n]. Reading only one of them would mark a genuinely cited source as
 * uncited on whichever surface the other spelling survived - a false accusation is no better
 * than the false credit this function exists to remove.
 *
 * Pure, and exported, so it can be tested in node without a DOM
 * (tests/selftest_622_sources_say_what_they_are.py runs it for real rather than grepping
 * this file - the failure mode this repo has been bitten by is a test that asserts on an
 * asset the user never receives).
 */
export function citedMarkers(answer) {
  const found = new Set();
  const text = String(answer || "");
  for (const m of text.matchAll(/\[(\d+)\]/g)) found.add(Number(m[1]));
  for (const m of text.matchAll(/【\s*(\d+)\s*(?:†[^】]*)?】/g)) found.add(Number(m[1]));
  return found;
}

/* #629: THE TAIL OF AN ANSWER, IN ONE LINE.
 *
 * What was under every answer: a Sources block listing every retrieved document, then a
 * separate sentence naming the denominator. Two paragraphs of apparatus per turn, on every
 * turn, which by the third exchange is most of what is on screen. The owner's word was
 * "messy" and he was right.
 *
 * WHAT STAYS VISIBLE IS THE COUNT, and that is the deliberate part. "1 of 2 you can access"
 * is LAW 2 demonstrated on this specific answer - the product's whole claim is that it
 * trimmed to your permissions, and a claim you have to click to see is a claim most people
 * never see. So the apparatus collapses; the promise does not.
 *
 * Returns null when there are no citations. The four no-source states (nothing indexed, none
 * yours, none matched, no corpus count) are genuinely different situations that a user acts
 * on differently, and `provenanceNote` says four different sentences for them. A pill cannot
 * compress those without being wrong about at least one, so callers keep rendering the note
 * in that case - see how ask.js branches on a null return.
 */
export function sourcesPill(cites, answer, { retrieved = 0, corpus = null } = {}) {
  const list = cites || [];
  if (!list.length) return null;
  const entitled = corpus && corpus.indexed ? (corpus.authorized_docs || 0) : 0;
  const n = retrieved || list.length;
  const label = entitled
    ? `Sources · ${n} of ${entitled} you can access`
    : `Sources (${list.length})`;
  return el("button", { type: "button", class: "sources-pill",
                        "aria-label": `${label}. Open the sources panel` },
    el("span", { class: "sources-pill-ico", "aria-hidden": "true" },
       docGlyph()),
    el("span", {}, label));
}

function docGlyph() {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 16 16");
  s.setAttribute("class", "sources-ico");
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = '<path d="M4 2.5h5l3 3v8H4z"/><path d="M9 2.5v3h3"/>';
  return s;
}

/* #629 + #622 + #633: the panel's contents.
 *
 * EVERY RULE THE OLD RAIL KEPT IS KEPT HERE, and this is where they now live:
 *
 * WHAT THIS LIST IS: every document that survived the permission trim and the top-k cut and
 * was therefore placed in the prompt as context - not the documents the answer demonstrably
 * used. Those are different sets, and the product used to show the first under a heading that
 * read as the second.
 *
 * UNCITED SOURCES ARE GROUPED, NEVER FILTERED. Filtering is the satisfying change and it is
 * wrong twice: the model was handed that document's text, so the answer can be shaped by it
 * with no marker to show for it, and dropping the row would claim an independence nobody can
 * verify; and the numbering is LOCKSTEP with the context blocks (#257), so removing a row
 * means renumbering, which is how you invent provenance instead of removing it. Grouping says
 * the one knowable thing: this was evidence, and the answer does not point at it.
 *
 * AN ANSWER THAT MARKS NOTHING ACCUSES NOBODY. When no marker appears at all, no row is
 * labelled. Two kinds of answer reach that state and the label is wrong for one: a refusal
 * genuinely attributes nothing, but the EXTRACTIVE model never emits a marker in ANY answer -
 * so labelling its rows would tell the reader, on every answer of every extractive
 * deployment, that an answer built out of those documents does not come from them.
 *
 * QUOTES (#633) APPEAR UNDER CITED ROWS ONLY. A passage quoted beneath a document the answer
 * never pointed at would dress evidence up as attribution - the exact confusion the grouping
 * above exists to prevent, reintroduced in a prettier form.
 */
export function buildSourcesPanel({ question = "", cites = null, answer = "",
                                    retrieved = 0, corpus = null, as = "" } = {}) {
  const list = cites || [];
  const pointed = citedMarkers(answer);
  const marking = pointed.size > 0;
  const wrap = el("div", { class: "sources-panel-body" });
  if (question) wrap.append(el("div", { class: "sources-panel-q" }, `"${question}"`));
  wrap.append(provenanceNote({ retrieved, corpus, verb: "Grounded in", as }));

  const cited = el("div", { class: "sources-group" });
  const shown = el("div", { class: "sources-group" });
  // Counted rather than read back off `.children`: this builder is pure and is exercised in
  // node against a minimal DOM stand-in, so it must not depend on DOM properties it does not
  // need. It appended the rows; it already knows how many.
  let nCited = 0;
  let nShown = 0;
  list.forEach((c, i) => {
    const n = i + 1;
    const isCited = !marking || pointed.has(n);
    const group = isCited ? cited : shown;
    if (isCited) nCited += 1; else nShown += 1;
    group.append(sourceCard(n, c, { cited: true }));
    if (isCited && c.quote) {
      group.append(el("blockquote", { class: "source-quote" },
        el("div", { class: "source-quote-cap" },
          c.quote_kind === "pointed" ? "The lines the answer points at"
                                     : "Top passage given to the model"),
        c.quote));
    }
  });
  if (nCited) wrap.append(cited);
  if (nShown) {
    wrap.append(
      el("div", { class: "sources-group-head" }, "Also given to the model"),
      el("div", { class: "admin-note" },
        "These documents were retrieved and placed in the model's context for this "
        + "question. The answer carries no marker pointing at them."),
      shown);
  }
  return wrap;
}

/* ONE panel per surface (#629), not one per answer.
 *
 * Opening it from a different answer repaints it, and its header quotes THAT question - a
 * panel that silently changed contents while looking identical would let a reader check a
 * claim against the wrong answer's evidence.
 */
export function mountSourcesPanel(root) {
  const panel = el("aside", { class: "sources-panel", hidden: "hidden",
                              role: "complementary", "aria-label": "Sources" });
  // MOUNTED ON <body>, NOT ON THE SURFACE, and that is a fix rather than a preference.
  // Appending it to the surface made it a direct child of the reading column, which carries
  // `#view-app .surface:has(.chat-composer) > * { width:100%; max-width:820px }` to centre
  // the thread - so the panel inherited an 820px width and covered the answer it was meant
  // to sit beside. Found in a browser; jsdom has no layout and could not have seen it.
  // Out of that subtree, no column rule can reach it. `root` is still what gets the
  // push class, so the reading column knows to make room.
  document.body.append(panel);

  function close() {
    panel.hidden = true;
    panel.innerHTML = "";
    root.classList.remove("has-sources-panel");
  }

  /** In SHEET mode the panel takes the bottom of the screen, so the answer it is about can
   *  be behind it. Scroll that answer up into the strip that is still visible - otherwise
   *  "open the sources to check the claim" hides the claim, which is the thing this whole
   *  card exists to stop (#636 horizontally, #639 vertically). `anchor` is the message the
   *  panel was opened from; without one there is nothing to keep in view and this is a no-op.
   */
  function keepAnchorVisible(anchor) {
    if (!anchor || panel.hidden) return;
    const sheet = Math.round(panel.getBoundingClientRect().width) >= window.innerWidth - 1;
    if (!sheet) return;                       // side panel: the column already made room
    anchor.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  function open(props, focusCite, anchor) {
    panel.innerHTML = "";
    const closeBtn = el("button", { type: "button", class: "sources-panel-close",
                                    "aria-label": "Close sources" }, "✕");
    closeBtn.addEventListener("click", close);
    panel.append(el("div", { class: "sources-panel-head" },
      el("span", { class: "sources-title" }, "Sources"), closeBtn));
    panel.append(buildSourcesPanel(props));
    panel.hidden = false;
    // The reading column gets out of the way at desktop widths; the class is on the host so
    // one CSS rule decides push-vs-overlay rather than every surface deciding for itself.
    root.classList.add("has-sources-panel");
    if (focusCite) {
      const num = [...panel.querySelectorAll(".source-card .num")]
        .find((x) => x.textContent === `[${focusCite}]`);
      if (num) num.closest(".source-card").scrollIntoView({ block: "nearest" });
    }
    closeBtn.focus();
    keepAnchorVisible(anchor);
  }

  // Escape closes it, like every other dismissible layer in this shell.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.hidden) close();
  });

  return { open, close };
}
