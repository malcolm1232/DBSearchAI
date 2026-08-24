// src/dbsearch/server/static/js/visitor.js
// The link visitor's surface (#605 task 12, ADR 0021) - the entry module for visitor.html.
//
// THE ONE RULE THIS MODULE IS WRITTEN AROUND: the visitor has NO SESSION. Every route that
// answers from customer documents depends on `current_user` and 401s when there is nothing to
// resolve, and a 401 arriving on page boot does not look like a permissions refusal to a
// visitor - it looks like a broken page. So this file talks to the four `/c/{token}` routes
// and to nothing else. It does not fetch /config, /auth/me, /ask/suggestions,
// /conversations/*, /admin/*, /search or /chat, it imports no module that does, and there is
// a test asserting exactly that on the served asset.
//
// It is a SEPARATE ENTRY POINT from main.js for the same reason visitor.html is a separate
// document from index.html: main.js loads /config, builds the model picker, mounts the
// workspace rail and starts the shell router. Every one of those is a workspace the visitor
// does not have. Reusing it and then hiding the parts that do not apply makes "a visitor is
// offered nothing of the owner's account" a property of a conditional somebody has to keep
// getting right; here it is a property of what is imported.
//
// What IS shared with the shell, and should be: the answer/source/provenance components
// (ui/components.js) and the palette. A shared link must not land somewhere that looks like a
// different product, and the citation rendering is the same rendering.
import {
  el, answerNodes, provenanceNote, sourcesPill, mountSourcesPanel,
} from "./ui/components.js";
import { popIn } from "./ui/motion.js";

// Read from the location, never injected into the page by the server. The token is a
// credential (ADR 0021: possession IS the authorization), and a credential written into HTML
// tends to end up in a copied page, a screenshot or a bug report. The browser already has it.
const TOKEN = (location.pathname.replace(/\/+$/, "").split("/")[2] || "");
const BASE = `/c/${encodeURIComponent(TOKEN)}`;

const thread = document.getElementById("visitor-thread");
const form = document.getElementById("visitor-form");
// #629: one sources panel for this page, mounted on <body> because the visitor page has no
// app shell to hang it inside. A visitor checking a claim needs the same affordance an
// account holder has - the whole point of a shared link is that it is readable.
const sourcesPanel = thread ? mountSourcesPanel(document.body) : null;
const input = document.getElementById("visitor-input");

// #600's string and treatment, said in the visitor's terms rather than the recipient's. One
// copy, used by every place a dead link can be discovered: on load, and on the next question
// asked inside a page that was already open when the owner revoked.
const GONE_TITLE = "This link is no longer available.";
const GONE_BODY = "Ask whoever shared it with you to send a new one.";

function notice(title, body) {
  return el("div", { class: "empty" }, el("h2", {}, title), el("div", {}, body));
}

/** The link died underneath an open page. Say so, and take the input away - leaving a live
 *  composer under a dead link invites a visitor to type a question that can only fail. */
function linkIsGone() {
  thread.innerHTML = "";
  thread.append(notice(GONE_TITLE, GONE_BODY));
  form.hidden = true;
}

/** A 429 from the per-share question cap, rendered honestly.
 *
 *  ADR 0021 bounds a forwardable link's cost with an hourly cap on QUESTIONS, and the refusal
 *  carries Retry-After. A visitor who has hit that ceiling must be told what happened and when
 *  to come back: a spinner, or a generic "something went wrong", turns a working product
 *  behaving exactly as designed into what looks like a fault, and the visitor retries into the
 *  same wall. The cap is per LINK, not per person, so the copy must not accuse this visitor of
 *  asking too much - somebody else may have spent it. */
function rateCapNotice(retryAfter) {
  const secs = Number(retryAfter);
  const mins = Number.isFinite(secs) && secs > 0 ? Math.ceil(secs / 60) : 0;
  const when = mins ? `Try again in about ${mins} minute${mins === 1 ? "" : "s"}.`
                    : "Try again shortly.";
  return notice("This link has answered as many questions as it can for now.",
                `There is an hourly limit on how many questions a shared link may ask. ${when}`);
}

function problemNotice(status) {
  if (status >= 500) {
    return notice("The server could not answer that.",
                  "This is not a permissions problem. Try again in a moment.");
  }
  return notice("That question could not be answered.",
                "Try rephrasing it, or ask whoever shared the link.");
}

/** One turn of the transcript.
 *
 *  `own` is the whole distinction and it comes from the SERVER (`/c/{token}/transcript`):
 *  false is the grantor's shared prefix, true is this visitor's own fork. The grantor's half
 *  is read only, and it is read only by CONSTRUCTION - nothing is built into it that could
 *  edit, retry, delete or re-ask, and there is no such control anywhere on this page for any
 *  turn. A label and a rule say so to the reader, so a visitor cannot mistake somebody else's
 *  question for one of their own. */
function turnNode(t) {
  const own = t.own === true;
  const q = el("div", { class: "result-q" });
  if (!own) {
    q.append(el("span", { class: "doc-audience doc-audience-shared" }, "Shared"), " ");
  }
  q.append(`"${t.question}"`);
  return el("section", { class: own ? "result" : "result visitor-turn-shared",
                         "data-own": own ? "true" : "false" },
    q, el("div", { class: "result-answer" }, answerNodes(t.answer)));
}

function renderAnswer(block, question, r) {
  block.innerHTML = "";
  block.append(el("div", { class: "result-q" }, `"${question}"`));
  const answer = el("div", { class: "result-answer" });
  if (r.answer) answer.append(answerNodes(r.answer)); else answer.textContent = "No answer.";
  block.append(answer);

  // #629: the same one-line tail every other surface has. A shared link must not land
  // somewhere that looks like a different product, and the citation rendering IS the
  // rendering - so the visitor gets the pill and the panel, not a rail nobody else has.
  // The denominator (#393) rides on the pill or, when there is nothing cited, on the note:
  // it is what tells a visitor how much of the workspace this link actually opens.
  const props = { question, cites: r.citations || [], answer: r.answer || "",
                  retrieved: (r.retrieved_docs || []).length, corpus: r.corpus };
  const pill = sourcesPill(props.cites, props.answer, props);
  if (pill && sourcesPanel) {
    pill.addEventListener("click", () => sourcesPanel.open(props));
    block.append(pill);
    block.addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("button.cite-ref");
      if (b) sourcesPanel.open(props, b.getAttribute("data-cite"));
    });
  } else {
    block.append(provenanceNote({ retrieved: props.retrieved, corpus: r.corpus }));
  }
}

async function loadTranscript() {
  let r;
  try {
    r = await fetch(`${BASE}/transcript`);
  } catch (_) {
    thread.innerHTML = "";
    thread.append(notice("This conversation could not be loaded.",
                         "Check your connection and reload the page."));
    return;
  }
  if (r.status === 404) { linkIsGone(); return; }
  if (!r.ok) {
    thread.innerHTML = "";
    thread.append(problemNotice(r.status));
    return;
  }
  const data = await r.json();
  thread.innerHTML = "";
  const turns = (data && data.turns) || [];
  // The server orders them: the grantor's readable prefix first, then this visitor's own
  // fork. Rendering in the order given is what puts the visitor's half BELOW the shared half.
  turns.forEach((t) => thread.append(turnNode(t)));
  input.focus();
}

async function ask(question) {
  question = (question || "").trim();
  if (!question) return;
  input.value = "";
  const block = el("section", { class: "result", "data-own": "true" },
    el("div", { class: "result-q" }, `"${question}"`),
    el("div", { class: "result-answer" }, "Searching…"));
  thread.append(popIn(block));

  let r;
  try {
    r = await fetch(`${BASE}/chat`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
  } catch (_) {
    block.innerHTML = "";
    block.append(notice("That question did not reach the server.",
                        "Check your connection and try again."));
    return;
  }
  if (r.status === 429) {
    block.innerHTML = "";
    block.append(rateCapNotice(r.headers.get("Retry-After")));
    return;
  }
  if (r.status === 404) {
    // Revoked, or expired, while this page was open. Same one answer as everywhere else.
    block.remove();
    linkIsGone();
    return;
  }
  if (!r.ok) {
    block.innerHTML = "";
    block.append(problemNotice(r.status));
    return;
  }
  renderAnswer(block, question, await r.json());
}

form.addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });

// The shell's theme preference, if this browser has one, so following a link from the product
// does not flash a light page at somebody who chose dark. There is no toggle here: it is one
// setting, owned by the app, and a second control for it on a page with no account attached
// would be a preference nobody could keep.
try {
  const t = localStorage.getItem("dbsearch_theme");
  if (t) document.documentElement.setAttribute("data-theme", t);
} catch (_) { /* storage can be blocked outright; the default theme is fine */ }

loadTranscript();
