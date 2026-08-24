// src/dbsearch/server/static/js/surfaces/draft.js
// Two-phase conversational proposal draft (#57/#59): a CHAT to gather requirements (cheap model,
// Haiku) -> "Ready" shows a requirements summary for sign-off -> "Confirm" drafts the proposal
// (strong model, Sonnet) over the SAME permission-trimmed retrieval. The model split lives in
// the backend; this surface just sends intents (chat | ready | confirm | cancel) to /draft/turn.
//
// #890: THIS SURFACE NOW USES ASK'S CHAT SYSTEM RATHER THAN ITS OWN.
// The rebuild that gave Ask a pinned composer and a scrolling thread never reached Draft, so
// Draft kept rendering the pre-rebuild markup while inheriting the rebuild's CSS. Two defects
// fell out of that, and the second one was severe:
//
//   1. `.chat-composer` is `display: block`, so the input's `flex: 1` had no flex container to
//      act in and collapsed to its intrinsic ~170px. The placeholder rendered UNDERNEATH the
//      Send button.
//   2. `#view-app .surface:has(.chat-composer)` sets `overflow: hidden` and delegates the
//      scrolling to a `.chat-scroll` child. Ask has one; Draft did not. Measured on prod after
//      generating one proposal: scrollTop 922, wheel events dead, scrollbar width 0, surface
//      tabIndex -1. 922px of the user's own proposal was unreachable by mouse OR keyboard.
//      The scrollIntoView() calls this file used to make MASKED it - the thread auto-followed
//      while streaming, so it looked fine until you tried to scroll back.
//
// The fix is to reuse what Ask already ships (.chat-scroll, .composer-shell, .chat-empty,
// .composer-hint, .surface-head) instead of inventing a second vocabulary for the same job.
// No new layout CSS is introduced here beyond one secondary button.
import { draftTurn, draftStream, getUser } from "../api.js";
import {
  el, provenanceNote, sourcesPill, mountSourcesPanel,
} from "../ui/components.js";
import { popIn } from "../ui/motion.js";

// #328: temporary demo toggle - hide the per-section "Sources (N)" rail. Flip back to true
// to restore it; nothing else changes, and the citations still arrive from the backend, so
// this only affects what the section RENDERS. The provenance line under each section is
// deliberately kept: it is the permission-faithfulness claim, not a source dump. It now
// comes from the shared provenanceNote(), which reports retrieval and entitlement as two
// separate numbers instead of printing the first as the second (#393).
const SHOW_SECTION_SOURCES = false;

// #890: starting points for the BRIEF, offered as chips instead of the old behaviour, which
// pre-filled one hardcoded sentence into the input AND text-selected it - so the box opened
// scrolled to the middle of a sentence the user never wrote, and their first keystroke
// silently destroyed it.
//
// WHY THIS IS ALLOWED HERE AND BANNED ON ASK. selftest_ask_suggestions locks down #392: Ask
// may not offer a hardcoded example, because clicking one QUERIES THE CORPUS, and on a
// deployment whose index is empty the resulting "I couldn't find anything you have access to"
// makes an empty index look like a permissions refusal. These chips cannot reproduce that.
// They describe a CLIENT SCENARIO, they name no document and no seeded corpus, and clicking
// one only fills the composer - the first retrieval does not happen until "Confirm & draft",
// by which point the provenance line reports the real corpus size honestly. Do not copy this
// pattern back to Ask, and do not make these chips send on click.
const STARTERS = [
  "A retail bank, acquisition advisory",
  "An HR policy review",
  "A staff onboarding programme",
];

function newConvId() {
  return (crypto.randomUUID && crypto.randomUUID()) ||
    `d-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// The model returns markdown-ish bullets with trailing whitespace and stray blank lines.
// `white-space: pre-wrap` renders both faithfully, which is how a tidy summary picked up a
// ragged right edge and gaps. Presentation only: no line is dropped, only trimmed.
function tidyRequirements(text) {
  return String(text || "")
    .split("\n")
    .map((line) => line.replace(/\s+$/, ""))
    .filter((line, i, all) => line !== "" || (all[i - 1] || "") !== "")
    .join("\n")
    .trim();
}

export function mountDraft(root) {
  let convId = newConvId();
  let busy = false;
  // #629: one sources panel for this surface, repainted by whichever section is asked about.
  const sourcesPanel = mountSourcesPanel(root);

  const thread = el("div", { id: "draft-thread", class: "chat-thread" });
  // #890: THE FIX FOR THE UNREACHABLE-PROPOSAL BUG. The surface is `overflow: hidden` and
  // expects a scroller among its children; this is it. min-height:0 lives on .chat-scroll in
  // app.css and is load-bearing - without it this flex child refuses to shrink and pushes the
  // composer off the bottom instead of scrolling.
  const scroller = el("div", { class: "chat-scroll" }, thread);

  // A textarea, not an <input>, for the same reason Ask uses one: a brief worth drafting from
  // is routinely longer than one line, and `.chat-input` is already styled for a textarea
  // (resize:none, max-height, overflow-y).
  const input = el("textarea", {
    class: "chat-input", id: "draft-input", rows: "1",
    placeholder: "Describe the client and what they need…", autocomplete: "off",
  });
  const readyBtn = el("button", { class: "draft-ready", type: "button",
    title: "Summarise requirements and review before drafting",
    onclick: () => ready() }, "Ready to draft");
  const sendBtn = el("button", { class: "ask-btn chat-send", type: "submit",
    "aria-label": "Send" }, "Send");

  const composer = el("form", { class: "chat-composer",
    onsubmit: (e) => { e.preventDefault(); send(input.value); } },
    el("div", { class: "composer-shell" }, input, readyBtn, sendBtn),
    el("div", { class: "composer-hint" },
      el("span", {}, "Enter to send · Shift + Enter for a new line"),
      el("span", { class: "composer-trust" }, "Trimmed to your permissions")));

  // #890: "New" moved OUT of the composer. It used to sit beside "Ready to draft" wearing the
  // identical `.new-conversation` style, so the step that advances the flow and the step that
  // destroys it were the same shape, the same weight and one pixel apart.
  const newBtn = el("button", { class: "new-conversation", type: "button",
    title: "Discard this brief and start over", onclick: () => reset() }, "New draft");

  root.append(scroller, composer);
  root.prepend(el("div", { class: "surface-head" },
    el("span", { class: "surface-title" }, "Draft"), newBtn));

  function showEmptyState() {
    thread.innerHTML = "";
    const chips = el("div", { class: "chat-starters" });
    STARTERS.forEach((s) => chips.append(el("button", {
      class: "starter", type: "button",
      onclick: () => { input.value = s; autoGrow(); input.focus();
                       input.setSelectionRange(s.length, s.length); },
    }, s)));
    thread.append(el("div", { class: "chat-empty" },
      el("h2", {}, "Draft a proposal"),
      el("p", {}, "Scope the brief in conversation, review the requirements it captures, "
                + "then draft. The proposal is grounded only in documents you can access."),
      chips));
  }
  showEmptyState();

  function clearEmptyState() {
    const empty = thread.querySelector(".chat-empty");
    if (empty) empty.remove();
  }

  // Auto-grow to a ceiling, then scroll inside the box. Without the ceiling a pasted brief
  // pushes the composer over the whole viewport.
  function autoGrow() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
  }
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); }
  });

  function atBottom() {
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  }
  // #890: follow the draft only if the reader is already at the bottom, which is the rule Ask
  // uses. The old code called scrollIntoView() unconditionally on every section, so a reader
  // who scrolled up to re-read section one was yanked back down on the next token.
  function stickToBottom(force) {
    if (force || atBottom()) scroller.scrollTop = scroller.scrollHeight;
  }

  function reset() {
    if (busy) return;
    draftTurn(convId, "", "cancel").catch(() => {});   // best-effort clear server state
    convId = newConvId();
    showEmptyState();
    input.value = "";
    autoGrow();
    input.focus();
  }

  function bubble(cls, ...kids) {
    clearEmptyState();
    const b = el("div", { class: `msg ${cls}` }, ...kids);
    thread.append(popIn(b));
    stickToBottom(true);
    return b;
  }

  async function send(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    input.value = "";
    autoGrow();
    bubble("msg-user", el("div", { class: "msg-body" }, text));
    const bot = bubble("msg-bot", el("div", { class: "msg-body" }, "…"));
    await guard(async () => {
      const r = await draftTurn(convId, text, "chat");
      bot.querySelector(".msg-body").textContent = r.reply || "(no reply)";
      stickToBottom();
    }, (msg) => { bot.querySelector(".msg-body").textContent = `Error: ${msg}`; });
  }

  async function ready() {
    if (busy) return;
    const pending = (input.value || "").trim();   // fold any unsent text into the summary
    input.value = "";
    autoGrow();
    if (pending) bubble("msg-user", el("div", { class: "msg-body" }, pending));
    const card = bubble("msg-bot", el("div", { class: "msg-body" }, "Summarising requirements…"));
    await guard(async () => {
      const r = await draftTurn(convId, pending, "ready");
      renderConfirm(card, r.requirements || "(nothing captured yet)");
    }, (msg) => { card.querySelector(".msg-body").textContent = `Error: ${msg}`; });
  }

  // #890: renders INTO the bubble's .msg-body rather than replacing the bubble. The old code
  // did `card.innerHTML = ""` on the .msg element itself, which destroyed the .msg-body that
  // `.msg-bot .msg-body` styles - so the sign-off card, the one thing on this surface the user
  // is asked to approve, was the only thing that rendered with no container at all, and its
  // two buttons floated loose underneath it looking like page actions.
  function renderConfirm(card, requirements) {
    const body = card.querySelector(".msg-body");
    body.innerHTML = "";
    body.append(
      el("div", { class: "sources-title" }, "These are your requirements"),
      el("div", { class: "draft-requirements" }, tidyRequirements(requirements)),
      el("div", { class: "draft-confirm-row" },
        el("button", { class: "ask-btn", type: "button", onclick: () => confirm() },
          "Confirm & draft"),
        el("button", { class: "new-conversation", type: "button", onclick: () => keepEditing() },
          "Keep editing")));
    stickToBottom(true);
  }

  async function keepEditing() {
    if (busy) return;
    await draftTurn(convId, "", "cancel").catch(() => {});
    bubble("msg-bot", el("div", { class: "msg-body" }, "Okay, what should change?"));
    input.focus();
  }

  async function confirm() {
    if (busy) return;
    const status = bubble("msg-bot",
      el("div", { class: "msg-body" }, `Drafting with Sonnet · as ${getUser() || "you"}…`));
    let wrap = null;
    const secs = {};   // title -> { sec, prose }
    await guard(async () => {
      await draftStream(convId, (ev) => {
        if (ev.type === "error") {
          status.querySelector(".msg-body").textContent = ev.message;
        } else if (ev.type === "plan") {
          status.remove();
          wrap = el("div", { class: "msg msg-bot draft-result" });
          const plan = el("section", { class: "draft-plan" },
            el("div", { class: "sources-title" }, `Plan · ${ev.plan.length} steps`));
          ev.plan.forEach((q, i) => plan.append(el("div", { class: "plan-step" }, `${i + 1}. ${q}`)));
          wrap.append(plan);
          thread.append(popIn(wrap));
          stickToBottom(true);
        } else if (ev.type === "section_start") {
          const prose = el("div", { class: "result-answer" }, "");
          const sec = el("section", { class: "draft-section streaming" }, el("h3", {}, ev.title), prose);
          secs[ev.title] = { sec, prose };
          wrap.append(sec);
          stickToBottom();
        } else if (ev.type === "token") {
          const s = secs[ev.title];
          if (s) { s.prose.textContent += ev.text; stickToBottom(); }
        } else if (ev.type === "section_done") {
          const s = secs[ev.title];
          if (!s) return;
          s.sec.classList.remove("streaming");
          // #629: one tail, every surface. The section's own prose carries the markers, so
          // its pill and panel are about THAT section rather than the whole draft.
          const props = { question: ev.title, cites: ev.citations || [],
                          answer: ev.text || ev.answer || "",
                          retrieved: (ev.retrieved_docs || []).length, corpus: ev.corpus };
          const pill = SHOW_SECTION_SOURCES
            ? sourcesPill(props.cites, props.answer, props) : null;
          if (pill && sourcesPanel) {
            pill.addEventListener("click", () => sourcesPanel.open(props));
            s.sec.append(pill);
            s.sec.addEventListener("click", (e) => {
              const b = e.target.closest && e.target.closest("button.cite-ref");
              if (b) sourcesPanel.open(props, b.getAttribute("data-cite"));
            });
          } else {
            s.sec.append(provenanceNote({
              retrieved: props.retrieved,
              corpus: ev.corpus,
              verb: "Drew on",
            }));
          }
          stickToBottom();
        }
      });
    }, (msg) => { bubble("msg-bot", el("div", { class: "msg-body" }, `Error: ${msg}`)); });
  }

  // shared busy-guard so double-clicks can't fire overlapping model calls
  async function guard(fn, onErr) {
    busy = true; setDisabled(true);
    try { await fn(); }
    catch (err) { onErr(err.message); }
    finally { busy = false; setDisabled(false); input.focus(); }
  }
  function setDisabled(v) {
    composer.querySelectorAll("button, input, textarea").forEach((e) => { e.disabled = v; });
    newBtn.disabled = v;
  }
}
