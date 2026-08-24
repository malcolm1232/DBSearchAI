// src/dbsearch/server/static/js/surfaces/ask.js
import {
  askSuggestions, chatStream, getUser, shareConversation, conversationShares,
  revokeConversationShare, sharedWithMe, myConversations, conversationTranscript,
  shareableDocs, narrowShareScope, rerunProof,
} from "../api.js";
import {
  el, answerNodes, previewText, provenanceNote, sourcesPill, mountSourcesPanel,
} from "../ui/components.js";
import { popIn } from "../ui/motion.js";
import { errorBlock } from "../ui/errors.js";
import { wireModalHost, focusFirstIn } from "../ui/modal.js";
// #689 (ADR 0025): the SAME Sources rail the canvas renders. Moved there, not copied - two
// surfaces explaining one answer differently is this card's defect one layer out.
import { collapsibleSourcesRail } from "../ui/proofs.js";

// #392: the example prompts USED to be a hardcoded array here, naming the two demo-seed
// documents. On prod the seed is off and the index holds zero rows, so the likeliest first
// click a new user made was guaranteed to return nothing, and the generic "I couldn't find
// anything you have access to" made an empty index look like a permissions refusal. The
// prompts now come from /ask/suggestions, which only offers them when the corpus that can
// answer them is actually indexed. Nothing here may invent a question again.

// One opaque conversation id per ongoing conversation (client-minted; server keys
// history by (conv_id, user)). Reset by "New conversation".
function newConvId() {
  return (crypto.randomUUID && crypto.randomUUID()) ||
    `c-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// #631: hairline SVGs, not emoji. The rows these mark now live in the DARK rail, which is
// where the old emoji icons were at their worst: emoji render as flat glyphs on Windows and
// coloured blobs on macOS, so the same navigation looked like a different product per
// operating system. Design system rule 6, and the ban holds for comments too - a test greps
// this file, and quoting the banned character to explain the ban trips it either way.
// These paths are constant strings, never data, which is why innerHTML is safe here.
const ICON_NEW = '<path d="M8 3.5v9M3.5 8h9"/>';
const ICON_THREAD =
  '<path d="M13.5 9.5a1.5 1.5 0 0 1-1.5 1.5H6l-3 2.5V4a1.5 1.5 0 0 1 1.5-1.5h7.5A1.5 1.5 0 0 1 13.5 4z"/>';
const ICON_SHARED =
  '<circle cx="5.5" cy="6" r="2.5"/><path d="M1.5 13.5c0-2.2 1.8-4 4-4s4 1.8 4 4"/>'
  + '<circle cx="11" cy="6.5" r="2"/><path d="M10.5 9.5c2 .3 3.5 1.9 3.5 4"/>';

function railIcon(paths) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 16 16");
  s.setAttribute("class", "navrail-ico");
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = paths;
  return s;
}

// One row in the rail's thread list. A real <button>: these are the primary way back into a
// conversation, and a clickable div is unreachable by keyboard and silent to a screen reader.
function threadRow(icon, title, meta, onOpen, active) {
  const row = el("button", {
    class: "rail-thread" + (active ? " active" : ""), type: "button", title,
    ...(active ? { "aria-current": "true" } : {}),
  }, railIcon(icon), el("span", { class: "rail-thread-title" }, title));
  if (meta) row.append(el("span", { class: "rail-thread-meta" }, meta));
  row.addEventListener("click", onOpen);
  return row;
}

function fmtDate(iso) {
  const d = new Date(iso || "");
  return Number.isNaN(d.getTime()) ? String(iso || "")
    : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

// #600: revoked, expired, or never live for this caller. One string and one visual
// treatment, used by BOTH places a dead share can be discovered - on opening the
// transcript, and on the next question asked inside an already-open thread.
function shareEndedNotice() {
  return el("div", { class: "empty" },
    el("h2", {}, "This share is no longer active."),
    el("div", {}, "Ask whoever shared it with you to share it again if you still need it."));
}

// #602: the same slot, for a thread the OWNER clicked. It is a different sentence and not a
// reuse of the one above, because the one above is about somebody else's permission and this
// one is about her own data - telling an owner "ask whoever shared it with you" about her own
// conversation would send her looking for a person who does not exist.
function conversationGoneNotice() {
  return el("div", { class: "empty" },
    el("h2", {}, "This conversation is no longer here."),
    el("div", {}, "Start a new one to keep going."));
}

// #611 / ADR 0021's disclosure ("the person who shared this link can see the questions you
// ask here") USED TO BE DEFINED IN THIS FILE, as a constant plus a `linkDisclosure()` builder
// gated on `location.pathname.startsWith("/c/")`. It is gone from here, and that is the fix
// rather than a deletion of the requirement.
//
// It never rendered. `/c` was not in `SHELL_PATHS`, so `initShell` showed the LANDING view on
// `/c/{token}` and this module's node was built inside a container that was never displayed;
// and even once that was noticed, the visitor's page is not this surface at all - a visitor
// has no account, so `/c/{token}` now serves its own document (static/visitor.html, mounted
// by js/visitor.js) with none of the workspace chrome mountAsk builds. Four tests pinned the
// sentence in this file and every one of them was green while no visitor had been told
// anything, which is the failure shape this repo has been bitten by repeatedly: they asserted
// on a FILE, and nothing asserted on what `GET /c/{token}` returns.
//
// THE SENTENCE NOW LIVES IN EXACTLY ONE PLACE: static/visitor.html, as static markup directly
// above the question input, where it is in the bytes a cookie-less visitor receives with no
// fetch and no route decision in between. tests/selftest_605_visitor_surface.py and the
// disclosure tests in tests/selftest_611_visitor_question_log.py assert on that RESPONSE, and
// deleting the sentence from that page fails them. What is still owed, and is not claimed
// here, is a real browser: those tests prove the disclosure is in the document and in the DOM
// above the input, not that a pixel is painted where a visitor looks.
//
// Nothing about it belongs on THIS surface. The owner reading her own thread is not being
// told anything true by it - nobody shared a link with her - and a warning that fires where it
// does not apply is how a warning stops being read.

// #606 / #610: the four sentences the share modal is REQUIRED to say, held as constants so
// there is exactly one copy of each and a test can pin them verbatim. These are product, not
// decoration: they are the whole difference between an owner who knows what she just handed
// over and one who finds out from the recipient.
const AUDIENCE_PEOPLE_LABEL = "Specific people - they sign in, named by email";
const AUDIENCE_LINK_LABEL = "Anyone with the link - no sign-in, link is the key";
// Under the checklist. It answers the question the checklist raises ("is this really all of
// it?") and the one it does not ("can they take copies away?").
const SHARE_SCOPE_NOTE = "Only these. Nothing else in your workspace is reachable, "
  + "and nobody can download the files.";
// On the copy-link view. A link visitor does not just READ the thread, she can go on asking
// questions of the documents behind it, and an owner who thinks she sent a read-only page has
// not been told what she did.
const LINK_READS_NOTE = "Anyone with this link can read this conversation and ask questions "
  + "from its documents until it expires.";
// The token leaves the server in exactly one response (ADR 0021) and the row keeps only its
// digest, so this really is the only time it can ever be rendered.
const LINK_SHOWN_ONCE = "This link is shown once. It cannot be shown again - if you lose it, "
  + "share again to mint a new one.";
// ADR 0017 s2, on a document the owner is reading through somebody else's grant. It is shown
// rather than hidden: an owner who cannot see why a document is missing concludes the product
// lost it, and goes looking for a bug instead of for the document's owner.
const NOT_YOURS_TO_SHARE = "not yours to share";
// #851: what ticking a SOURCE actually hands over, said on the row where the owner decides.
// She is passing on a RECORD of what she already saw - the query and the rows it returned when
// this thread ran - and not access: DBSearch cannot grant permissions on her warehouse, and
// running her credential for the recipient would tell that database somebody else was asking
// (#850). "as it was then" is the honest half people miss: the figures do not refresh.
const SOURCE_TRAVELS_AS_A_RECORD = "shared as a record, as it was then";
// #607: what a link row is called once it exists, where the picker's longer label would be
// answering a question the owner has already answered. It is also the ONE label a link row can
// carry: there is nobody on the other end of a link to name until somebody opens it.
const AUDIENCE_LINK_LABEL_SHORT = "Anyone with the link";
// #608, in the edit dialog. Two facts, and the owner needs both before she unchecks anything:
// this is not queued behind anything, and the dialog cannot give a document back.
const EDIT_NARROWS_ONLY = "Unchecking takes a document out of this share straight away, "
  + "including for anyone already holding the link. You cannot put one back here - share the "
  + "conversation again to hand over more.";

export function mountAsk(root) {
  let convId = newConvId();
  // #600: the Share control (shareBtn.style.display) has nothing honest to offer until the
  // thread has cited something - a conversation share of zero turns is refused server-side
  // (400), so it stays hidden rather than being a tile that always fails (#551).
  let shareOpen = false;
  // #600: true while the open thread is one somebody shared WITH this caller, so her
  // authorization over it can be taken away underneath her while the page is up.
  let sharedConv = false;
  // #632: one question in flight at a time (see setBusy).
  let busy = false;

  // #632: THE MERGED PRESENTATION. What used to be surfaces/chat.js is now the way this one
  // surface looks, and chat.js is deleted rather than kept beside it. The two files rendered
  // the same backend - both POSTed /chat/stream with a conv_id through one
  // ConversationService - so a thread begun on Chat was durable and reachable only from Ask's
  // conversation list, and no honest sentence distinguished the two in the nav. What came
  // across is chat.js's PRESENTATION (bubbles, a pinned composer, a textarea that takes a
  // multi-line question); what stayed is every capability Ask had and Chat did not - history,
  // sharing and its guards, server-checked suggestions, revoke detection, transcripts.
  const thread = el("div", { id: "thread", class: "chat-thread" });
  const scroller = el("div", { class: "chat-scroll" }, thread);

  // A textarea, not an <input>: a question worth asking a knowledge base is routinely longer
  // than one line, and a single-line box silently truncated the user's thinking.
  const input = el("textarea", {
    class: "chat-input", id: "ask-input", rows: "1",
    placeholder: "Ask your company knowledge…", autocomplete: "off",
  });
  const sendBtn = el("button", { class: "ask-btn chat-send", type: "submit",
    "aria-label": "Ask" }, "Ask");
  const form = el("form", { class: "chat-composer",
    onsubmit: (e) => { e.preventDefault(); submit(input.value); } },
    el("div", { class: "composer-shell" }, input, sendBtn),
    el("div", { class: "composer-hint" },
      el("span", {}, "Enter to send · Shift + Enter for a new line"),
      el("span", { class: "composer-trust" }, "Trimmed to your permissions")));

  // Auto-grow to a ceiling, then scroll inside the box. Without the ceiling a pasted
  // document pushes the composer over the whole viewport.
  function autoGrow() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
  }
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(input.value); }
  });

  function atBottom() {
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  }
  // Follow the answer only if the reader is already at the bottom. Yanking the viewport back
  // while somebody is re-reading an earlier turn is how a streaming thread becomes unusable.
  function stickToBottom(force) {
    if (force || atBottom()) scroller.scrollTop = scroller.scrollHeight;
  }
  function bubble(cls, ...kids) {
    const b = el("div", { class: `msg ${cls}` }, ...kids);
    thread.append(popIn(b));
    stickToBottom(true);
    return b;
  }

  // One question in flight at a time. Without this an impatient second Enter starts a
  // concurrent turn on the same conv_id, and the store's append serializes them into an
  // order neither the reader nor the condenser expects.
  function setBusy(v) {
    busy = v;
    sendBtn.disabled = v;
    sendBtn.textContent = v ? "…" : "Ask";
    input.disabled = v;
  }

  // #631: it lives at the top of the rail's thread list now, where it reads as "start
  // another one of these" - beside the threads it creates rather than floating under the
  // input, which is where #399 recorded it looking wrong.
  const newConvBtn = el("button", { class: "rail-new", id: "new-conversation",
    type: "button", title: "Start a new conversation",
    onclick: () => resetConversation() },
    railIcon(ICON_NEW), el("span", {}, "New conversation"));

  const shareBtn = el("button", { class: "new-conversation", id: "share-conversation",
    type: "button", title: "Share this conversation",
    style: "display:none", onclick: () => toggleShareModal() }, "Share");
  // #606: the share surface is a MODAL, not the old inline drawer. The owner is about to
  // decide what a share exposes, and that decision deserves the page's whole attention -
  // a drawer under a button competes with the thread it is about. Empty when closed, which
  // is what the `:empty` rule in app.css keys the backdrop off.
  const shareModal = el("div", { class: "share-modal-backdrop", id: "share-modal" });
  // Clicking the backdrop, Escape and Tab, all three going through the ONE teardown like
  // everything else does. `wireModalHost` is ui/modal.js's, shared with the Shared section's
  // edit dialog (#607): the keyboard contract of an `aria-modal` dialog is a safety property,
  // and this product now opens one on two screens.
  wireModalHost(shareModal, { isOpen: () => shareOpen, onDismiss: () => dismissShareModal() });
  // Set by the modal while it is showing something that cannot be recovered once dismissed.
  // Returns true when closing now is safe, false when it has asked the owner to confirm.
  let closeGuard = null;

  // FIX ROUND 1, CRITICAL. This function is the ONLY thing in this module that takes the
  // share modal down, and it consults the guard before it does. That is a structural claim
  // and it is the whole fix: the previous shape had a `closeShareModal()` that tore down
  // unconditionally plus an `attemptClose()` that asked first, so every NEW caller silently
  // got the unguarded one - and two already had. `resetConversation` ("New conversation",
  // a button sitting inches from the modal) and `openSharedConversation` (clicking a thread
  // somebody shared with you) both destroyed an uncopied one-time link with no warning at
  // all. The token is returned by the API exactly once and is never fetchable again, so the
  // owner was left holding a live share with no URL, told nothing.
  //
  // The teardown is now the gate, so a caller cannot skip it by not knowing about it: this is
  // the only way to take the modal DOWN. Stated precisely, because a claim about a safety
  // property that is bigger than the code is worse than no claim - it tells the next reader
  // not to look. The other clears of `shareModal.innerHTML` in this module all live inside
  // `renderShareModal`, which is repainting itself while it OPENS, and it can only run when
  // the modal is already down (`toggleShareModal` guards the open path on `shareOpen`) and
  // therefore when no guard exists: a guard is only ever armed by the copy-link view, and
  // reaching a fresh open means a teardown completed and cleared it. Callers that
  // need to NAVIGATE (reset, open a thread) must therefore handle a refusal, and both of them
  // do - they abandon the navigation and let the modal explain itself, exactly as the Close
  // button does. Anyone adding another caller gets a boolean they have to consider, rather
  // than a silent bypass.
  //
  // #602 added the third navigation - a row in "Your conversations" - and did NOT add a third
  // call site. Every thread-replacing navigation now goes through `openConversation`, which
  // consults this once; see its comment. The count of callers is the thing that used to go
  // wrong, so the fix was to stop growing it.
  //
  // Returns true when the modal is down (or was never up), false when the owner has been
  // asked to confirm and nothing has happened yet.
  function dismissShareModal() {
    if (closeGuard && !closeGuard()) return false;
    shareOpen = false;
    closeGuard = null;
    shareModal.innerHTML = "";
    return true;
  }

  // `aria-modal="true"` is a promise to a screen reader, not a behaviour: without a trap a
  // keyboard user could Shift+Tab straight out of the panel onto "New conversation", which
  // is one of the two routes that used to destroy an uncopied link. Tab cycles inside the
  // panel and nowhere else for as long as the modal is up. The trap itself is ui/modal.js's,
  // so the edit dialog on "Your data" cannot drift from it or, as it did on arrival, simply
  // not have one.

  // #600 step 2.2: only rendered when it has something to show - an empty inbox is not
  // worth a section (the same call renderSources() makes for an empty source list).
  const sharedWithYou = el("div", { id: "shared-with-you" });

  // #602: THE OWNER'S DOOR BACK TO HER OWN THREADS, and this container is the whole of that
  // card on this surface. `newConvId()` mints a fresh id on every page load, which is right for
  // a page you arrive at to ask something - and it meant that after a reload the owner had no
  // way to reach the conversation she had just had. She could not reopen it, could not see who
  // she had shared it with, and could not press Remove. The thread was durable (#596) and
  // unreachable, which reads to her as data loss.
  //
  // It sits BELOW the buttons rather than beside "Shared with you" above the form, because
  // these two lists answer different questions and the order matters: what somebody has sent
  // her is news, and her own history is a filing cabinet.
  //
  // IT STAYS ON SCREEN once a thread is open, deliberately. Hiding it the moment she asks
  // something would rebuild the original defect in miniature - one door, usable once - and
  // switching between two of her own threads is exactly the motion this exists for.
  const yourConversations = el("div", { id: "your-conversations" });

  // The slot below the standing copy: examples, or an honest account of why there are none.
  //
  // #632: chat.js had three HARDCODED starter questions here, naming demo-seed documents,
  // and they are NOT what came across in the merge. That is the exact defect #392 fixed on
  // this surface: on a deployment whose index is empty, an invented example is a question
  // guaranteed to return nothing, and the generic "I couldn't find anything you have access
  // to" then makes an empty corpus look like a permissions refusal. The suggestions below
  // come from /ask/suggestions, which only offers a question the indexed corpus can actually
  // answer. Nothing here may invent one again - not even quoted in a comment, which is how
  // this very comment first tripped selftest_ask_suggestions.
  const hint = el("div", { class: "chat-starters" });
  function showEmptyState() {
    thread.innerHTML = "";
    thread.append(el("div", { class: "chat-empty" },
      el("h2", {}, "Search your company knowledge"),
      el("p", {}, "Answers are cited and respect your permissions - you only see what "
                + "you're allowed to."),
      hint));
  }
  showEmptyState();
  // #629: ONE panel for the surface, not one per answer. It is mounted on the surface root so
  // the reading column can make room for it at desktop widths, and repainted by whichever
  // answer is asked about.
  const sourcesPanel = mountSourcesPanel(root);
  root.append(scroller, form, shareModal);
  root.prepend(el("div", { class: "surface-head" },
    el("span", { class: "surface-title" }, "Ask"), shareBtn));

  // #631: THE THREAD LISTS LIVE IN THE RAIL, not in the reading column.
  //
  // They used to be stacked above the thread - "Shared with you", the form, two buttons, then
  // "Your conversations" - so the owner's filing cabinet competed with the answer she was
  // reading, and on a first visit the empty state collided with the list (#625). Moving them
  // out is what makes the reading column one thing.
  //
  // The order is not arbitrary: New first because it is the action, then her own threads, then
  // what other people sent her. News below the filing cabinet rather than above the question
  // box, which is where it used to shout.
  //
  // `railSlot()` is queried off the DOM rather than imported, deliberately: main.js loads
  // rail.js through a BUILD-VERSIONED dynamic import (#415), and a static import here would
  // reintroduce the unversioned module URL that once deleted the navigation for every warm
  // cache. A null slot (the canvas, or a page whose rail has not mounted) simply means no
  // list, never a crash.
  const slot = document.querySelector(".navrail-slot");
  if (slot) {
    slot.append(
      el("div", { class: "rail-slot-head" }, newConvBtn),
      yourConversations, sharedWithYou);
  } else {
    // No rail on this page. Everything the slot would have carried goes into the column
    // instead - INCLUDING "New conversation", which is the control that starts a thread.
    // Dropping it here would have been the #602 defect again in miniature: a door that
    // exists on one layout and silently does not on another.
    root.append(el("div", { class: "rail-slot-head" }, newConvBtn),
                yourConversations, sharedWithYou);
  }
  input.focus();

  loadSharedWithMe();
  loadMyConversations();

  // Ask the server what is actually true for THIS caller, then say only that.
  askSuggestions().then((s) => {
    if (!s) return;                       // unreachable: show nothing extra, never a false "empty"
    hint.innerHTML = "";
    if (s.unauthenticated) {
      hint.append(el("div", { class: "ask-note" },
        "Sign in to search your own documents. ",
        el("a", { href: "/signin" }, "Sign in"),
        " or ", el("a", { href: "/canvas" }, "try the demo"), "."));
      return;
    }
    if (s.examples && s.examples.length) {
      // Real buttons, not clickable spans: they are the first thing a new user presses, and
      // a span is unreachable by keyboard and invisible to a screen reader.
      s.examples.forEach((q) =>
        hint.append(el("button", { class: "starter", type: "button",
          onclick: () => submit(q) }, q)));
      return;
    }
    if (!s.known) return;                 // backend cannot count - stay silent (LAW: no guessing)
    // #937: "nothing is indexed" is a claim about the UPLOADED-document index only. A caller
    // whose source is a Drive folder, an S3 prefix or a local folder has their content in that
    // store's own index (router/providers/connector.py), which this count has never been able
    // to see - so on prod this sentence told a real user to connect a source they had already connected,
    // and kept telling them after it had answered their questions from it.
    //
    // `connected_sources` is the missing plane. Only 0 - a MEASURED zero - earns the sentence.
    // Anything else, including null (workspace store unreachable) and undefined (a server that
    // predates this field), takes the same path `!s.known` does one line above: say nothing.
    // Guessing "empty" from a number we do not have is the #392 defect, and #392 is the card
    // that put this sentence on the page in the first place.
    if (s.connected_sources !== 0) return;
    if (!s.indexed) {
      // An empty index is a "connect a source" problem, and saying so is the whole point of
      // #392: the old copy sent the user hunting for a permissions fault that did not exist.
      hint.append(el("div", { class: "ask-note" },
        "No documents have been indexed yet, so document questions will come back empty. ",
        el("a", { href: "/canvas" }, "Connect a source"),
        " to get started."));
    } else if (!s.authorized_docs) {
      // Documents exist here, but none of them admit this caller. That IS a permissions
      // statement, and it is the one case where saying so is correct rather than misleading.
      hint.append(el("div", { class: "ask-note" },
        "No documents you are permitted to see have been indexed yet. "
        + "Ask whoever administers your sources for access."));
    }
  });

  function resetConversation() {
    // The teardown comes FIRST and its answer is obeyed. "New conversation" is a navigation,
    // and a navigation that silently destroys a credential the owner cannot get back is not
    // a navigation, it is data loss with a friendly label. Refused means refused: nothing
    // below this line runs, the modal says why, and a second click goes through.
    if (!dismissShareModal()) return;
    convId = newConvId();
    sharedConv = false;
    shareBtn.style.display = "none";
    showEmptyState();
    input.value = "";
    autoGrow();
    input.focus();
  }

  function toggleShareModal() {
    if (shareOpen) { dismissShareModal(); return; }
    shareOpen = true;
    renderShareModal();
  }

  async function renderShareModal() {
    shareModal.innerHTML = "";
    shareModal.append(el("div", { class: "share-modal" },
      el("div", { class: "admin-muted" }, "Loading…")));
    let shares, scope;
    try {
      // Both in flight together: the checklist and the existing-share list are two halves of
      // one dialog, and showing one while the other is still loading invites the owner to
      // act on half a picture.
      const [listed, shareable] = await Promise.all([
        conversationShares(convId), shareableDocs(convId),
      ]);
      shares = listed.shares || [];
      scope = shareable;
    } catch (e) {
      shareModal.innerHTML = "";
      shareModal.append(el("div", { class: "share-modal" }, errorBlock(e)));
      return;
    }
    if (!shareOpen) return;               // closed while the request was in flight
    shareModal.innerHTML = "";
    shareModal.append(buildShareModal(convId, shares, scope, {
      // The modal's own Close button asks for the SAME teardown every other route asks for.
      // It holds no copy of the guard and no shortcut past it.
      close: dismissShareModal,
      guard: (fn) => { closeGuard = fn; },
    }));
    // Focus starts inside the modal, or the trap has nothing to hold: a Tab from the page
    // behind would otherwise walk the shell before it ever reached the dialog.
    focusFirstIn(shareModal);
  }

  async function loadSharedWithMe() {
    let data;
    try { data = await sharedWithMe(); } catch (_) { return; }   // unreachable: say nothing
    const shares = (data && data.shares) || [];
    if (!shares.length) return;
    // #631: a rail group label, matching the nav's own "Workspace"/"Operate" headings,
    // so the slot reads as part of the rail rather than as a page pasted into it.
    sharedWithYou.append(el("div", { class: "navrail-group" }, "Shared with you"));
    shares.forEach((s) => sharedWithYou.append(sharedConversationRow(s)));
  }

  function sharedConversationRow(s) {
    return threadRow(ICON_SHARED, `Conversation shared by ${s.grantor_oid}`,
                     fmtDate(s.created_at), () => openSharedConversation(s),
                     s.conv_id === convId);
  }

  // #602. Silent on failure and silent when empty, for two different reasons. Unreachable is
  // the `loadSharedWithMe` rule: a list that cannot be read says nothing rather than claiming
  // there is nothing. Empty is the renderSources rule: a heading over no rows is a section
  // announcing its own absence, and on somebody's first ever visit "Your conversations" with
  // nothing under it reads as a thread that went missing.
  async function loadMyConversations() {
    let data;
    try { data = await myConversations(); } catch (_) { return; }
    const convs = (data && data.conversations) || [];
    yourConversations.innerHTML = "";
    if (!convs.length) return;
    yourConversations.append(el("div", { class: "navrail-group" }, "Recents"));
    convs.forEach((c) => yourConversations.append(ownConversationRow(c)));
  }

  function ownConversationRow(c) {
    const n = c.turns || 0;
    // The thread's OPENING QUESTION is its name, already truncated by the server so one
    // definition of "what a conversation is called" serves this list and the Shared section on
    // "Your data". The count is what tells two of her threads apart when both start similarly.
    // FIX ROUND 1. `c.own === false` means this row is a thread somebody shared WITH her that
    // she has since replied in - her reply keys under her own oid, so the store legitimately
    // lists it, and only her own question and count are in the row. It must NOT be reopened as
    // if it were hers: `sharedConv` is what arms #600's revoke detection on the next question,
    // and reopening a received thread with it false told a grantee whose share had been revoked
    // "This conversation is no longer here" - owner-data words for somebody whose SHARE ended.
    // Defaulting to owned is the safe direction for an older server that omits the field: it is
    // the reading that shows her HER OWN data as hers, never somebody else's as hers.
    const received = c.own === false;
    // #631: the count is the meta, and the DATE is dropped from the row. A 248px rail cannot
    // carry "3 questions · last asked 11 Aug 2026 · shared with you" without ellipsing the
    // one thing that identifies the thread - its opening question. The count is what tells
    // two similar threads apart; the rest lives in the tooltip, which is where a detail you
    // only occasionally need belongs.
    const title = c.first_question || "Untitled conversation";
    const detail = `${n} question${n === 1 ? "" : "s"} · last asked ${fmtDate(c.last_asked_at)}`
      + (received ? " · shared with you" : "");
    const row = threadRow(received ? ICON_SHARED : ICON_THREAD, title, String(n),
      () => openConversation(c.conv_id, {
        shared: received,
        // On a received thread the transcript carries the GRANTOR's half too, and labelling
        // his turns with her name is the exact mistake `transcriptTurn`'s grantorLabel exists
        // to prevent. On her own threads every turn is `own`, so the label is never rendered.
        label: received ? (c.grantor_oid || "whoever shared this") : (getUser() || "you"),
        gone: received ? shareEndedNotice : conversationGoneNotice }),
      c.conv_id === convId);
    // The full sentence survives in BOTH channels a narrow row can still carry it in: the
    // tooltip a mouse user gets, and the accessible name a screen reader reads. The visible
    // meta is only the count because that is all 248px has room for; "2 questions · last
    // asked 11 Aug 2026 · shared with you" would ellipse away the question that names the
    // thread. Losing the sentence entirely was the tempting version and would have made the
    // singular/plural care that #602 put into it pointless.
    row.title = `${title}\n${detail}`;
    row.setAttribute("aria-label", `${title} - ${detail}`);
    return row;
  }

  // ONE function opens a thread, whoever it belongs to, and it is the ONE place the guarded
  // teardown is consulted before a thread is replaced.
  //
  // THIS IS THE STRUCTURAL HALF OF #602's CLIENT, and it is written this way because of what
  // happened the last time this surface grew a navigation. `dismissShareModal` returns false
  // while the modal is holding a one-time share link the owner has not copied - the API returns
  // that token exactly once and can never return it again - and the two navigations that
  // existed then, "New conversation" and opening a thread somebody shared with you, both tore
  // the modal down without asking. That was a Critical finding on this branch. A row in the
  // new "Your conversations" list is a THIRD navigation of exactly the same kind, and rather
  // than adding a third `if (!dismissShareModal()) return;` for a fourth caller to forget, the
  // whole navigation lives here once. `openSharedConversation` is now a thin wrapper on it,
  // so the two paths cannot drift.
  //
  // `opts.shared` decides whether a revoke underneath this caller replaces her next answer
  // (see `submit`), `opts.label` names whose turn it is when it is not hers, and `opts.gone`
  // is what an absent thread says - which is a different sentence for her own conversation
  // than for somebody else's share.
  async function openConversation(id, opts) {
    if (!dismissShareModal()) return;
    convId = id;
    sharedConv = !!opts.shared;
    shareBtn.style.display = "none";
    input.value = "";
    autoGrow();
    thread.innerHTML = "";
    thread.append(el("div", { class: "chat-empty" }, "Loading…"));
    let data;
    try {
      data = await conversationTranscript(convId);
    } catch (e) {
      thread.innerHTML = "";
      const box = errorBlock(e);
      box.classList.add("msg-body");
      thread.append(el("div", { class: "msg msg-bot msg-error" }, box));
      return;
    }
    thread.innerHTML = "";
    if (!data) {
      // #600: the transcript resolved to null (api.js's 404-as-null) - never a blank page.
      thread.append(opts.gone());
      input.focus();
      return;
    }
    const turns = data.turns || [];
    // ONE rule for the Share control, and the server is the honest arbiter of it either way.
    // On the owner's own thread every turn is `own`, so it simply appears - which is the
    // acceptance criterion #602 exists for. On a received thread her own continuation cites
    // the GRANTOR's documents through the conversation grant, not anything of hers, and the
    // control is still offered because ADR 0020 s5 refuses to pass on a turn drawing on a
    // document she holds only through somebody else's grant: a re-share is dropped turn by
    // turn there rather than pre-judged here, and usually hands on nothing at all.
    if (turns.some((t) => t.own)) shareBtn.style.display = "";
    // #620: `data.corpus` is the denominator the live answer shipped with, so a reopened
    // turn's footer says the same thing it said the day it was answered. It is null on a
    // thread read through somebody else's share, where this reader's entitlement cannot be
    // counted honestly - and provenanceNote then reports retrieval only, claiming nothing.
    turns.forEach((t) =>
      thread.append(transcriptTurn(t, opts.label, data.corpus, sourcesPanel)));
    stickToBottom(true);
    input.focus();
  }

  function openSharedConversation(share) {
    return openConversation(share.conv_id, { shared: true, label: share.grantor_oid,
                                             gone: shareEndedNotice });
  }

  async function submit(question) {
    question = (question || "").trim();
    if (!question || busy) return;
    const emptyState = thread.querySelector(".chat-empty");
    if (emptyState) emptyState.remove();
    input.value = "";
    autoGrow();
    setBusy(true);

    bubble("msg-user", el("div", { class: "msg-body" }, question),
                       el("div", { class: "msg-meta" }, `as ${getUser() || "you"}`));
    const block = bubble("msg-bot", el("div", { class: "msg-body" },
      el("span", { class: "typing" }, el("i", {}), el("i", {}), el("i", {}))));
    const answerEl = block.querySelector(".msg-body");
    answerEl.textContent = "";        // tokens stream in here as they arrive
    let acc = "";
    try {
      await chatStream(convId, question,
        // #893: `acc` is the model's RAW output, and this is the only place on the page it is
        // shown without passing through answerNodes - for the whole length of a generation,
        // which is where the owner read "…has been confirmed【9†L1-L4】" back off prod. The
        // preview now speaks the product's format; the clickable control still arrives with
        // the final render below.
        (tok) => { acc += tok; answerEl.textContent = previewText(acc); stickToBottom(false); },
        (done) => {
          // #600 acceptance step 8: Bob can revoke while Alice still has the thread open.
          // The transcript's honest-refusal state only fires on LOAD, so without this her
          // next question comes back answered from an empty corpus - which is the
          // fabrication route, not a refusal. The corpus block already rides on the
          // response (#393), so nothing extra is asked for: a shared thread whose
          // denominator has gone to zero has had its authorization taken away, and the
          // model's output is replaced rather than shown.
          //
          // A floor, not a ceiling, and deliberately so. A recipient who also owns
          // documents keeps a non-zero denominator after a revoke and gets a real answer
          // from HER OWN corpus - correct, and not something to refuse.
          if (sharedConv && done.corpus && !done.corpus.authorized_docs) {
            block.innerHTML = "";
            block.append(el("div", { class: "msg-body" }, shareEndedNotice()));
            return;
          }
          // #257, and #689 made it load-bearing rather than tidy. The streamed tokens are a
          // DRAFT: the server's marker sweep, the question-echo strip and - on the routed path
          // - the #493 condensed pass and the #474 cross-store rescue all rewrite the answer
          // AFTER the last token, so `done.answer` and `acc` genuinely differ. Rendering the
          // accumulator showed text the product had already decided was wrong, with markers
          // that resolve to nothing. `done.answer` is the record; `acc` was only ever the
          // preview the reader watched arrive.
          renderResult(block, question,
            { answer: done.answer || acc, citations: done.citations,
              retrieved_docs: done.retrieved_docs, corpus: done.corpus,
              // #689: present only on a routed turn. `renderResult` keys the whole Sources
              // rail off this, so a document-only answer is untouched.
              // #859: ...and which of those footnotes the FINAL answer points at. Forwarded
              // rather than re-derived here: the server reads it off the same answer it just
              // sent, after the marker strip, so the client cannot disagree with it about
              // what is on screen.
              footnotes: done.footnotes, referenced: done.referenced,
              disclosure: done.disclosure },
            { panel: sourcesPanel });
          // #600: at least one answered turn exists now, so there is something to share.
          shareBtn.style.display = "";
          // #602: ...and a thread that did not exist a moment ago now needs a door. Re-read
          // rather than pushed in from here: the row's name, its count and its position are
          // the SERVER's answer, and a client that guessed them would be showing her its own
          // opinion of what it just stored.
          loadMyConversations();
        });
    } catch (err) {
      // #409: was `Error: ${err.message}`, so a logged-out visitor's first
      // question came back as "Error: chat failed: 401".
      block.classList.add("msg-error");
      block.innerHTML = "";
      const box = errorBlock(err);
      box.classList.add("msg-body");
      block.append(box);
      stickToBottom(true);
    } finally {
      setBusy(false);
      input.focus();
    }
  }
}

// One turn of a transcript read through /conversations/{id}/transcript - either the
// grantor's half (read-only, `own: false`) or the reader's own continuation (`own: true`).
// `grantorLabel` names whose turn it is when it is not the reader's own, so a recipient can
// never mistake the grantor's half of the thread for something she wrote herself.
// #632: TWO bubbles per turn, returned as a fragment, so a reopened thread is laid out
// exactly like a live one. A transcript that reads differently from the conversation it is a
// record of makes the reader wonder which one is the real thing.
//
// #620/#633: the turn's citations now ride on the transcript, so a reopened answer is
// rendered through the SAME tail builder a live one uses - the markers resolve and the
// sources are there. Before that, reopening left dangling superscripts pointing at nothing.
function transcriptTurn(t, grantorLabel, corpus, panel) {
  const frag = document.createDocumentFragment();
  const q = el("div", { class: "msg-body" });
  if (!t.own) q.append(el("span", { class: "doc-audience doc-audience-shared" }, "Shared"), " ");
  q.append(t.question);
  frag.append(el("div", { class: "msg msg-user" }, q,
    el("div", { class: "msg-meta" },
      `as ${t.own ? (getUser() || "you") : grantorLabel}`)));

  const bot = el("div", { class: "msg msg-bot" });
  // #689: a stored PROOF row (`store_id`, no `doc`) becomes a footnote, so a reopened routed
  // turn renders through the SAME builder a live one does - #632's one-builder rule, which
  // exists because the two drifted once already and a reopened thread lost its sources (#620).
  // The server re-signs `rerun_token` for THIS reader on her own turns and never on a
  // grantor's half, so Verify data works where it should and is simply absent where it should
  // not - this maps what arrived and invents nothing.
  //
  // #861: THE NUMBER IS THE POSITION IN THE STORED LIST, NOT THE POSITION AMONG SURVIVORS.
  // This filtered to proof rows and then numbered them `i + 1`, so dropping the document
  // rows between them silently renumbered every proof after the first gap. Measured on prod:
  // an answer reading "Singapore 137[1] London 92[4] Berlin 78[7] Austin 65[10]" over twelve
  // stored citations reopened with its four rows labelled [1][2][3][4] - so [4] opened the
  // row that says AUSTIN while the answer meant London, and [7] and [10] resolved to nothing.
  // Wrong attribution is worse than none: a dangling marker looks broken, a moved one looks
  // sourced. It is #855's sentence at a fourth home - "removing row n silently renumbers
  // every later marker... A row that says less is honest; a row that has moved is a lie" -
  // and the LIVE path never had it, because router_api numbers footnotes over ALL evidence
  // and #859 filters that list while keeping each row's own `n`.
  const stored = t.citations || [];
  const footnotes = stored
    // Number FIRST, against the list the answer's markers were written against...
    .map((c, i) => ({ c, n: i + 1 }))
    // ...then drop the rows this builder cannot render, each survivor keeping its own number.
    .filter(({ c }) => c.store_id && !c.doc)
    .map(({ c, n }) => ({
      n, kind: c.kind || "", store_id: c.store_id,
      origin: c.origin || c.store_id,
      system: (c.origin || "").split(" · ")[0] || "",
      location: (c.origin || "").split(" · ")[1] || "",
      object: (c.origin || "").split(" · ")[2] || "",
      snippet: c.snippet || "", column_types: {}, uri: "", sql: c.sql || "",
      rerun_token: c.rerun_token || "",
    }));
  renderResult(bot, t.question, {
    answer: t.answer, citations: stored,
    // Only its LENGTH is read (provenanceNote's `retrieved`), and the citation list is
    // exactly the documents this turn drew on - which is what that number means.
    retrieved_docs: stored,
    corpus: corpus || null,
    footnotes,
  }, { feedback: false, panel });
  frag.append(bot);
  return frag;
}

// The share MODAL (#606/#610). Four things in one dialog, in the order the owner decides
// them: who it is for, exactly which documents it hands over, when it lapses, and who already
// has it. Plus the two honest facts the backend deliberately does not hide - the share is a
// snapshot, and the sharer may be told some of her own turns did not travel because they draw
// on documents that are not hers to pass on.
//
// THE NARROWING RULE IS STRUCTURAL, and this is the comment the brief asks for. There is NO
// affordance in this DOM that adds a document to a share: not a button, not a disabled
// button, not a hidden input, not an autocomplete. The checklist is built from the server's
// `/shareable` answer and its only interaction is UNCHECKING, so "a share can only ever be
// narrowed from here" is guaranteed by the control not existing rather than by a rule
// somebody has to keep enforcing. The server refuses to widen too (`exclude_docs` is
// subtracted from a set it computed), so the two agree - but the client half of that promise
// is kept by absence, and anyone adding an "add document" control here is breaking the
// feature, not extending it.
//
// #607 / #608: THE SAME MODAL IS ALSO THE EDIT SURFACE, reached from the Shared section on
// "Your data" (surfaces/admin.js) with `ctl.edit` set to the share row being edited. It is one
// builder and not two on purpose, and the reason is the paragraph above: the rule that a share
// can only ever be narrowed is kept by the DOM having no control that widens one, and a second
// dialog built somewhere else is a second DOM where somebody would have to remember that. In
// edit mode the audience picker, the email field, the expiry field and the Share button are all
// absent - the owner is not choosing a recipient, she has one - and the checklist is built by
// the SAME row builder from the share's own live scope, so unchecking is the only interaction
// that exists here either. Unchecking and saving calls PATCH /shares/{id}/scope, whose body has
// no add key.
export function buildShareModal(convId, shares, scope, ctl) {
  const edit = (ctl && ctl.edit) || null;
  const panel = el("div", { class: "share-modal", role: "dialog", "aria-modal": "true",
                            "aria-label": edit ? "Edit what this share opens"
                                               : "Share this conversation" });
  const list = el("div", { class: "share-list" });

  const paint = () => {
    list.innerHTML = "";
    if (!shares.length) {
      list.append(el("div", { class: "admin-muted" }, "Not shared with anyone yet."));
      return;
    }
    shares.forEach((s) => {
      const revoke = el("button", { type: "button", class: "share-revoke" }, "Remove");
      revoke.addEventListener("click", async () => {
        revoke.disabled = true;
        revoke.textContent = "Removing…";
        try {
          await revokeConversationShare(s.share_id);
          shares = shares.filter((x) => x.share_id !== s.share_id);
          paint();
        } catch (e) {
          revoke.disabled = false;
          revoke.textContent = "Remove";
          list.append(el("div", { class: "share-err" }, e.message));
        }
      });
      // #606: both audiences render in ONE list and revoke identically, because to the
      // owner they are the same act with two doorways. A link row cannot be named after a
      // person - there is nobody on the other end until somebody opens it - so it is named
      // after what it is, and its `opens` count is the only trace of use there will ever be.
      //
      // The people row prints `grantee_oid`, which today is a raw `acct_<hex>` rather than
      // the email that was typed. That is a real defect, it is carded as #603, and it is
      // deliberately NOT fixed here: the fix belongs where the account record is read, not
      // in a second surface papering over it.
      const isLink = s.audience === "link";
      const when = `shared ${fmtDate(s.created_at)}`
        + (s.expires_at ? ` · expires ${fmtDate(s.expires_at)}` : "")
        + (isLink ? ` · opened ${s.opens || 0} time${s.opens === 1 ? "" : "s"}` : "");
      list.append(el("div", { class: "share-row" },
        el("span", { class: "share-who" }, isLink ? "Anyone with the link" : s.grantee_oid),
        el("span", { class: "share-when" }, when),
        revoke));
    });
  };
  paint();

  // ---- who it is for --------------------------------------------------------------------
  // Two audiences, one modal. They are not two features: the owner is answering one question
  // ("who is this for?") and the answer changes what the form needs to ask her, so putting
  // them behind separate buttons would make her choose a MECHANISM before she has chosen a
  // recipient. `audience` is only ever a code-path selector, here and on the server.
  let audience = "people";
  const emailRow = el("div", { class: "share-controls" });
  const audienceBox = el("div", { class: "share-audience", role: "radiogroup",
                                  "aria-label": "Who this conversation is shared with" });
  [["people", AUDIENCE_PEOPLE_LABEL], ["link", AUDIENCE_LINK_LABEL]].forEach(([val, label]) => {
    const radio = el("input", { type: "radio", name: "share-audience", value: val,
                                class: "share-audience-radio" });
    radio.checked = val === "people";
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      audience = val;
      // Link-mode shows expiry only: there is no email to collect, and leaving a dead field
      // on screen invites the owner to type into it and believe she named somebody.
      emailRow.style.display = val === "link" ? "none" : "";
      err.textContent = "";
    });
    audienceBox.append(el("label", { class: "share-audience-opt" }, radio,
      el("span", { class: "share-audience-label" }, label)));
  });

  // ---- exactly which documents ----------------------------------------------------------
  const docList = el("div", { class: "share-docs", id: "share-doc-list" });
  const docCount = el("div", { class: "share-doc-count" });
  const boxes = new Map();               // doc id -> its checkbox. SHAREABLE documents only.
  // #851: declared HERE, beside `boxes`, rather than down with the rows it fills. `paintCount`
  // below closes over both, and a `const` declared after the function that reads it is one
  // reordering away from a temporal-dead-zone throw inside a change handler.
  const storeBoxes = new Map();          // store id -> its checkbox
  const paintCount = () => {
    const n = [...boxes.values()].filter((b) => b.checked).length;
    // Audience-neutral on purpose: "with them" is a sentence about a named person, and it
    // would be quietly wrong the moment the owner picks the link, where there is no "them"
    // yet and possibly several.
    //
    // Tense-correct in edit mode, which is not decoration: the share already exists, so "will
    // be shared" would read as a promise about a future act and leave the owner unsure whether
    // anything has happened yet.
    // #851: sources are counted here too, because this line is the owner's ONE summary of
    // what she is handing over. Counting documents alone would have gone quietly wrong the
    // moment a thread drew on a database - "2 documents will be shared" under a checklist
    // whose third ticked row is a warehouse.
    const m = [...storeBoxes.values()].filter((b) => b.checked).length;
    const docs = `${n} document${n === 1 ? "" : "s"}`;
    const srcs = m ? ` and ${m} source${m === 1 ? "" : "s"}` : "";
    docCount.textContent = edit
      ? `${docs}${srcs} stay${(n + m) === 1 ? "s" : ""} shared.`
      : `${docs}${srcs} will be shared.`;
  };
  // ONE ROW BUILDER, and it is a structural guarantee rather than a tidiness one. This modal
  // rests on "there is no control in this DOM that widens a share", and that claim is only
  // auditable if a checkbox is constructed in exactly one place - selftest_606 counts them.
  // #851 added a second kind of row (sources) and the honest way to do that was to feed this
  // builder, not to write a second one beside it.
  const checkRow = (into, id, title, note) => {
    const box = el("input", { type: "checkbox", class: "share-doc-box" });
    box.checked = true;                  // everything this thread used is in the share by default
    box.addEventListener("change", paintCount);
    into.set(id, box);
    const kids = [box, el("span", { class: "share-doc-title" }, title)];
    if (note) kids.push(el("span", { class: "doc-audience doc-audience-private" }, note));
    docList.append(el("label", { class: "share-doc" }, ...kids));
  };

  (scope && scope.documents || []).forEach((d) => {
    if (!d.shareable) {
      // ADR 0017 s2: hers to READ, not hers to pass on. It gets NO checkbox at all - not a
      // disabled one - so "cannot be checked" is a property of the markup rather than of an
      // attribute somebody could flip. It is greyed, labelled, and left out of the count.
      docList.append(el("div", { class: "share-doc share-doc-blocked" },
        el("span", { class: "share-doc-title" }, d.title || d.id),
        el("span", { class: "doc-audience doc-audience-private" }, NOT_YOURS_TO_SHARE)));
      return;
    }
    checkRow(boxes, d.id, d.title || d.id);
  });
  // ---- and exactly which SOURCES (#851, the owner's ruling on #850) ----------------------
  //
  // A turn the router answered came from a connected database, not from a document, so there
  // is no grant to mint and nothing for the recipient's own permissions to be checked against.
  // What makes it shareable is the GRANTOR SAYING SO - the same act that shares a document,
  // in the same list, because "what does this hand over" is one question and splitting it
  // across two dialogs is how an owner ends up answering only half of it.
  //
  // UNTICKING IS STILL THE ONLY INTERACTION. These rows add no affordance the document rows
  // do not have: default ticked, narrow-only, and the server subtracts `exclude_stores` from
  // a set it computed itself, so a store id sent from here can only ever remove one. The
  // structural rule this modal rests on - there is no control in this DOM that widens a share
  // - holds exactly as it did.
  const stores = (scope && scope.stores) || [];
  if (stores.length) {
    docList.append(el("div", { class: "share-doc-group" }, "Sources this conversation used"));
    // What the recipient gets from a source, said where the decision is made. It is a RECORD -
    // the query and the rows as they were when this thread ran - and never live access, which
    // the product cannot grant on somebody else's database (#850).
    stores.forEach((src) => {
      checkRow(storeBoxes, src.id, src.title || src.id, SOURCE_TRAVELS_AS_A_RECORD);
    });
  }
  if (!docList.children.length) {
    docList.append(el("div", { class: "admin-muted" },
      "This conversation has not cited any documents yet."));
  }
  paintCount();

  const who = el("input", { type: "text", class: "share-input",
    placeholder: "Their sign-in email address", autocomplete: "off" });
  // #617: the placeholder used to read "Expires in days (optional)" inside a 110px box, so a
  // real browser rendered it clipped to "Expires in da" - a field that looks broken before it
  // has been touched. jsdom has no layout, so no test could have seen it; the browser
  // acceptance run did. The fix is a placeholder that FITS its box rather than a wider box:
  // widening enough for the old sentence would have made an optional day count the widest
  // control in the modal, and the units belong in a label a screen reader can reach anyway.
  const days = el("input", { type: "number", class: "share-input", style: "flex:0 0 110px",
    placeholder: "Days", "aria-label": "Expires in days (optional)", title: "Expires in days (optional)",
    min: "1" });
  const err = el("div", { class: "share-err" });
  const status = el("div", { class: "admin-note" });
  const add = el("button", { type: "button", class: "share-add" }, "Share");
  add.addEventListener("click", async () => {
    const email = who.value.trim();
    err.textContent = "";
    status.textContent = "";
    if (audience === "people" && !email) {
      err.textContent = "Enter the email address to share with."; return;
    }
    // Share is a bare button, not a form submit, so the input's `min="1"` never gets
    // enforced by the browser - validated here instead. 0 is falsy and would otherwise
    // silently drop expires_in_days and mint a share that NEVER expires (the opposite of
    // what was typed); a negative number would reach the backend and mint an
    // already-expired one. Both are refused before the request is even sent.
    const rawDays = days.value.trim();
    let expiresInDays;
    if (rawDays) {
      expiresInDays = Number(rawDays);
      if (!Number.isFinite(expiresInDays) || expiresInDays < 1) {
        err.textContent = "Expires in days must be 1 or more.";
        return;
      }
    }
    add.disabled = true;
    add.textContent = "Sharing…";
    try {
      // The unchecked boxes, and nothing else, become `exclude_docs`. Documents that were
      // never shareable are not in `boxes` at all, so they cannot be "excluded" either -
      // the server already refuses them and naming them again would be noise.
      const excludeDocs = [...boxes.entries()]
        .filter(([, b]) => !b.checked).map(([id]) => id);
      // #851: the same rule, one list out. A source the owner unticked becomes
      // `exclude_stores`, which the server subtracts from the set IT computed - so this can
      // only ever narrow, and an id that names nothing removes nothing.
      const excludeStores = [...storeBoxes.entries()]
        .filter(([, b]) => !b.checked).map(([id]) => id);
      const r = await shareConversation(convId,
        { audience, email, expiresInDays, excludeDocs, excludeStores });
      shares = shares.filter((x) => x.share_id !== r.share_id).concat([r]);
      who.value = "";
      days.value = "";
      paint();
      if (r.url) { showLink(r); return; }
      // #600: what this exact click actually did - never presumed from the form inputs.
      status.textContent =
        `Backed by ${r.documents} document${r.documents === 1 ? "" : "s"}.`;
      status.append(withheldNote(r));
    } catch (e) {
      err.textContent = e.message;
    } finally { add.disabled = false; add.textContent = "Share"; }
  });

  // ---- the link, shown once ---------------------------------------------------------------
  //
  // WHAT WAS CHOSEN FOR THE SHOW-ONCE PROBLEM, deliberately. The plaintext token exists in
  // exactly one response (ADR 0021) and the row keeps only its digest, so a modal that can be
  // dismissed before the owner has copied it destroys the share - and she does not find out,
  // the recipient does. Three things together, none of which is a nagging dialog:
  //
  //   1. The link is in a readonly, pre-selected text field, so it can be copied by hand as
  //      well as by the button. A Copy button that fails on a locked-down clipboard must not
  //      be the only route to the value.
  //   2. Dismissing while it is UNCOPIED is guarded: the first Close (or Escape, or a click
  //      on the backdrop) arms and asks, the second closes. One extra click, never a trap.
  //   3. The share itself is not lost by closing - it is live and listed below with its own
  //      Remove. What is lost is the URL, and the guard says exactly that rather than
  //      implying the share failed.
  //
  // What is NOT offered is a way back to the token, because there is none: it is gone from
  // the server. Re-sharing mints a new link, which is the honest remedy and is what the copy
  // says.
  let copied = false;
  let closeArmed = false;
  function showLink(r) {
    const url = location.origin + r.url;
    const field = el("input", { type: "text", class: "share-link-url", readonly: "readonly",
                                "aria-label": "The share link" });
    field.value = url;
    const copy = el("button", { type: "button", class: "share-add" }, "Copy");
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(url);
      } catch (_) {
        field.focus(); field.select();   // clipboard blocked: hand her a selected value
      }
      copied = true;
      closeArmed = false;
      copy.textContent = "Copied";
      closeBtn.textContent = "Done";
      guardNote.textContent = "";
    });
    body.innerHTML = "";
    body.append(
      el("p", { class: "share-link-once" }, LINK_SHOWN_ONCE),
      el("div", { class: "share-controls" }, field, copy),
      el("p", { class: "admin-note" }, LINK_READS_NOTE),
      el("div", { class: "admin-note" },
        `Backed by ${r.documents} document${r.documents === 1 ? "" : "s"}.`,
        withheldNote(r)),
      guardNote,
      el("div", { class: "share-list-head" }, "Already shared with"),
      list,
    );
    field.focus();
    field.select();
    // From here on the modal is holding something unrecoverable. This function is handed to
    // the ONE teardown, so it is consulted by every route that can take the modal down -
    // Close, Escape, the backdrop, "New conversation", and opening a shared thread - and the
    // copy is written to be true of all of them rather than of the Close button alone.
    ctl.guard(() => {
      if (copied || closeArmed) return true;
      closeArmed = true;
      closeBtn.textContent = "Close without copying";
      guardNote.textContent = "You have not copied the link yet, and it cannot be shown "
        + "again. Copy it now, or do that again to dismiss it - the share stays live, but "
        + "you will have to share again to get a new link.";
      return false;
    });
  }

  // Its own class as well as the shared amber one: it is the only line on the copy-link view
  // that is about THIS click rather than about the share, and a DOM probe (or a person)
  // hunting for it must not land on the withheld-turns note beside it.
  const guardNote = el("div", { class: "share-note-warn share-guard-note" });
  const closeBtn = el("button", { type: "button", class: "share-modal-close",
                                  "aria-label": "Close" }, "Close");
  // No local copy of the guard, and no local teardown. This asks the surface to take the
  // modal down and the surface consults the guard - which is what makes "every route is
  // guarded" a property of there being one route rather than of five call sites agreeing.
  closeBtn.addEventListener("click", () => { ctl.close(); });

  // ---- edit mode: narrow a share that is already live ------------------------------------
  //
  // The whole of #608's client. It reuses the checklist above rather than drawing its own, so
  // "unchecking is the only interaction" is one fact about one builder. Its Save sends the
  // unchecked ids as `remove_docs` - the mirror of `exclude_docs` on the mint - and there is
  // nothing here that could send the opposite, because the route has no key for it.
  const save = el("button", { type: "button", class: "share-add share-save" }, "Save changes");
  save.addEventListener("click", async () => {
    err.textContent = "";
    status.textContent = "";
    const removed = [...boxes.entries()].filter(([, b]) => !b.checked).map(([id]) => id);
    if (!removed.length) { ctl.close(); return; }
    save.disabled = true;
    save.textContent = "Saving…";
    try {
      const r = await narrowShareScope(edit.share_id, removed);
      // What the SERVER says is left, never what the checklist assumed - a document deleted
      // between the two reads is the case where those two differ, and the owner is about to
      // close this dialog believing the number.
      status.textContent = `${r.documents} document${r.documents === 1 ? "" : "s"} still shared.`;
      if (ctl.onNarrowed) ctl.onNarrowed(r);
    } catch (e) {
      // The server's own sentence, verbatim - including the refusal to narrow a share down to
      // nothing, which tells her to revoke instead and is the one message she can act on.
      err.textContent = e.message;
    } finally { save.disabled = false; save.textContent = "Save changes"; }
  });

  if (edit) {
    // #603: a people row prints the raw `acct_<hex>` rather than the email that was typed.
    // That is a real defect and it is carded; it is deliberately not papered over here,
    // because the fix belongs where the account record is read and a second surface guessing
    // at an address would be a second thing to correct.
    const recipient = edit.audience === "link"
      ? AUDIENCE_LINK_LABEL_SHORT : edit.grantee_oid;
    panel.append(
      el("div", { class: "share-modal-head" },
        el("h3", { class: "share-modal-title" }, "Edit what this share opens"), closeBtn),
      el("div", { class: "share-modal-body" },
        el("div", { class: "admin-note" }, `Shared with ${recipient}.`),
        docList,
        docCount,
        el("p", { class: "share-scope-note" }, SHARE_SCOPE_NOTE),
        el("div", { class: "share-controls" }, save),
        err, status,
        el("p", { class: "admin-note" }, EDIT_NARROWS_ONLY)));
    return panel;
  }

  const body = el("div", { class: "share-modal-body" },
    audienceBox,
    docList,
    docCount,
    // The sentence that makes the checklist mean something. It answers both halves: nothing
    // outside this list is reachable, and what is on it cannot be taken away as files.
    el("p", { class: "share-scope-note" }, SHARE_SCOPE_NOTE),
    emailRow,
    el("div", { class: "share-controls" }, days, add),
    err, status,
    el("p", { class: "admin-note" },
      "This shares only what has been said so far - it will not grow as the conversation "
      + "continues, and sharing again updates (and can narrow) what they can see."),
    el("div", { class: "share-list-head" }, "Already shared with"),
    list,
  );
  emailRow.append(who);

  panel.append(
    el("div", { class: "share-modal-head" },
      el("h3", { class: "share-modal-title" }, "Share this conversation"), closeBtn),
    body);
  return panel;
}

// ADR 0020 s5: withholding PROPAGATES - once a turn is held back every later turn is too,
// because a follow-up is condensed against a window that includes the withheld answer. Only
// the first offender is withheld by its own citations, so "they use documents you cannot pass
// on" was true of exactly one of them. One definition, used by both audiences' success views.
function withheldNote(r) {
  if (!(r.turns_withheld > 0)) return el("span", {});
  return el("div", { class: "share-note-warn" },
    `${r.turns_withheld} turn${r.turns_withheld === 1 ? " was" : "s were"} not shared `
    + "because they use, or follow on from, documents you cannot pass on.");
}

/* ONE bot bubble, painted one way (#632).
 *
 * Both a live answer and a reopened transcript turn come through here, which is the point:
 * the two used to be built by different code and drifted, so a reopened thread lost its
 * sources and its footer while the live one kept them (#620). One builder, one drift-free
 * answer to "what does an answer look like".
 *
 * `opts.feedback` is off for a transcript turn: the thumbs are about the answer you just
 * received, and offering them on a thread from last week invites a vote on something the
 * reader no longer remembers judging.
 */
function renderResult(block, question, r, { feedback = true, panel = null } = {}) {
  block.innerHTML = "";

  // #555: prose, not the model's raw markdown + 【n†Lx】 markers.
  const answer = el("div", { class: "msg-body" });
  if (r.answer) answer.append(answerNodes(r.answer)); else answer.textContent = "No answer.";
  block.append(answer);

  // #629: the apparatus collapses to one line and opens on demand. Everything the old rail
  // said is still said, in the panel; what stays on screen is the permission-trim count,
  // because that is the product's promise rather than its plumbing.
  const props = {
    question,
    cites: r.citations || [],
    answer: r.answer || "",
    retrieved: (r.retrieved_docs || []).length,
    corpus: r.corpus,
    as: getUser() || "you",
  };
  // #689 (ADR 0025): a ROUTED answer explains itself through the same Sources rail the canvas
  // uses, and that rail is the ONLY provenance surface on this bubble when it is present.
  //
  // Not "as well as" the pill, deliberately. The router's footnotes already cover BOTH planes
  // - the caller's documents are a store in the ask scope (server/ask_router.py), so a
  // document this answer drew on appears as a `kind: "document"` footnote in the same list.
  // Rendering the pill beside it would put two provenance surfaces on one answer, with two
  // numbering schemes for the same evidence, which is #755's defect (two identical SOURCES
  // headings on one screen) re-created on a new surface.
  // #218/#799: what the router could NOT cover, in the user's own words, above the apparatus
  // and never buried inside it. An answer with a gap in it must say so where the answer is.
  if (r.disclosure) {
    block.append(el("div", { class: "authorized-note" }, `⚠ ${r.disclosure}`));
  }

  // #859: THE ROWS THE ANSWER POINTS AT, not everything the turn retrieved. Since #856 the
  // caller's documents are consulted on every routed turn, so a revenue question retrieves HR
  // policies that answer nothing - and rendering them claims a provenance the answer never
  // asserted. canvas.js said it first, under #724: "a Sources list is a provenance claim, and
  // there was no answer for it to be the provenance OF."
  //
  // `referenced` is the SERVER's reading of the final answer (QueryService._referenced), not a
  // second parse here - one rule, one home, and it is computed after the marker strip so it can
  // only name a number that is really on screen.
  //
  // EMPTY MEANS SHOW EVERYTHING, deliberately. An answer that cites nothing - a cautious model,
  // an extractive fallback - would otherwise be left with no rail at all, and a reader with
  // nothing to check is worse off than one shown more than the answer used. #724 kept the
  // honest line for the same reason rather than deleting the block.
  //
  // The survivors KEEP THEIR OWN NUMBERS. `f.n` is what the answer's marker names, so
  // renumbering them 1..n would point [3] at the row [1] describes - #855's lie, one surface out.
  const all = r.footnotes || [];
  const ref = Array.isArray(r.referenced) ? r.referenced : [];
  const fns = ref.length ? all.filter((f) => ref.includes(f.n)) : all;
  if (fns.length) {
    // The rail's CHAT framing - collapsed behind a one-line summary - is the shared module's,
    // not this surface's: selftest_622 pins that no surface builds its own Sources heading,
    // and a wrapper assembled here would be the same drift one element out.
    const host = collapsibleSourcesRail(fns, rerunProof);
    const body = host.lastElementChild;
    block.append(host);
    // A marker in the answer is a promise that the source is reachable. Clicking one OPENS the
    // rail before jumping, because a click that silently did nothing - because the container
    // happened to be closed - breaks that promise in the one place the reader is checking it.
    block.addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("button.cite-ref");
      if (!b) return;
      const target = body.querySelector("#fn" + b.getAttribute("data-cite"));
      if (!target) return;
      host.open = true;
      body.querySelectorAll(".src.hl").forEach((s) => s.classList.remove("hl"));
      target.classList.add("hl");
      target.scrollIntoView({ block: "nearest" });
    });
    if (!feedback) return;
    return appendFeedback(block);
  }

  const pill = sourcesPill(props.cites, props.answer, props);
  if (pill && panel) {
    pill.addEventListener("click", () => panel.open(props, null, block));
    block.append(pill);
    // Delegated, so every [n] in this answer - including ones streamed in later - opens the
    // panel focused on the source it names, without a listener per marker.
    block.addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("button.cite-ref");
      if (b) panel.open(props, b.getAttribute("data-cite"), block);
    });
  } else {
    // No citations (or no panel host): the four honest no-source sentences, which no pill
    // could compress without being wrong about at least one of them.
    block.append(provenanceNote({
      retrieved: props.retrieved, corpus: r.corpus,
      verb: "Grounded in", as: props.as,
    }));
  }

  if (!feedback) return;
  appendFeedback(block);
}

// #689: ONE definition, because `renderResult` now returns early on the routed path and a
// second copy of these two buttons is how a routed answer would quietly lose its thumbs -
// or keep them after somebody removed the others.
function appendFeedback(block) {
  const fb = el("div", { class: "feedback" });
  for (const [label, val] of [["Helpful", "up"], ["Not helpful", "down"]]) {
    const b = el("button", { type: "button", "aria-pressed": "false",
      onclick: () => {
        block.dataset.vote = val;   // per-turn signal; TODO(phase4): POST to feedback endpoint
        [...fb.children].forEach((c) => c.setAttribute("aria-pressed",
          c === b ? "true" : "false"));
      } }, label);
    fb.append(b);
  }
  block.append(fb);
}
