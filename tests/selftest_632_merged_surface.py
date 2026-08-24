"""#632: one conversational surface, and the merge kept the better half of each.

Ask and Chat rendered the SAME backend - both POSTed /chat/stream with a conv_id through one
ConversationService - so a thread begun on Chat was durable and reachable ONLY from Ask's
conversation list. The owner could not say what the difference was, which is the defect.

WHAT THE MERGE HAD TO PRESERVE, and why each of these is asserted rather than trusted:

  Chat's PRESENTATION was better - bubbles, a pinned composer, a multi-line textarea - and
  is what a reader now gets on /ask.

  Ask's CAPABILITIES were the ones with teeth, and every one of them is a rule somebody
  argued for: the guarded share teardown that protects an uncopied one-time link, the
  revoke-mid-thread check that stops a dead share answering from an empty corpus, transcript
  reopen with grantor labelling, and server-checked suggestions.

  Chat's THREE HARDCODED STARTERS did NOT come across, deliberately. That is #392: an
  invented example on a deployment with an empty index is a question guaranteed to return
  nothing, and the generic "I couldn't find anything you have access to" then makes an empty
  corpus look like a permissions refusal.

    python3 tests/selftest_632_merged_surface.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)


def _ask_js() -> str:
    r = client.get("/static/js/surfaces/ask.js")
    assert r.status_code == 200, f"ask.js -> {r.status_code}"
    return r.text


def test_ask_carries_the_chat_presentation():
    js = _ask_js()
    for needle in ("chat-thread", "chat-scroll", "chat-composer", "chat-input",
                   "msg-user", "msg-bot", "textarea", "autoGrow", "stickToBottom"):
        assert needle in js, f"the merged surface lost the chat presentation: {needle}"
    print("  PASS  /ask renders the bubble thread and the pinned composer")


def test_ask_kept_every_capability_chat_never_had():
    js = _ask_js()
    for needle in ("dismissShareModal", "openConversation", "buildShareModal",
                   "askSuggestions", "sharedWithMe", "myConversations",
                   "conversationTranscript", "shareEndedNotice", "conversationGoneNotice"):
        assert needle in js, f"the merge dropped a capability: {needle}"
    print("  PASS  history, sharing, transcripts and honest suggestions all survived")


def test_the_share_teardown_is_still_the_only_route_down():
    """The guard that protects an uncopied one-time share link. It was a Critical finding
    once already: two navigations tore the modal down without asking, destroying a token the
    server can never return again. The merge moved this code; it must not have grown a
    second way down."""
    js = _ask_js()
    assert "if (!dismissShareModal()) return" in js, (
        "a navigation no longer consults the guarded teardown")

    # The real invariant, as the module's own comment states it: every clear of the modal
    # lives either in `dismissShareModal` (the one teardown, which consults the guard) or
    # inside `renderShareModal` (repainting itself while it OPENS, when no guard can exist).
    # A clear anywhere else is a route down that skips the guard.
    functions = {}
    current = None
    for line in js.splitlines():
        stripped = line.strip()
        if stripped.startswith("function ") or stripped.startswith("async function "):
            current = stripped.split("function ", 1)[1].split("(", 1)[0].strip()
        if 'shareModal.innerHTML = ""' in line:
            functions.setdefault(current, 0)
            functions[current] += 1
    assert set(functions) <= {"dismissShareModal", "renderShareModal"}, (
        f"the modal is cleared outside the teardown and its own repaint: {sorted(functions)}"
        " - that is a route down which never consults the guard, and an uncopied one-time "
        "share link dies with it")
    print("  PASS  one guarded teardown, still the only route down")


def test_no_invented_starter_questions_came_across():
    js = _ask_js()
    for invented in ("What is our holiday and expenses policy?",
                     "Summarise what I can see about Project Falcon.",
                     "Which security policies changed this quarter?"):
        assert invented not in js, (
            f"chat.js's hardcoded starter came across: {invented!r}. #392: an invented "
            "example on an empty index returns nothing and reads as a permissions refusal")
    assert "askSuggestions" in js, "the honest, server-checked suggestions are gone too"
    print("  PASS  suggestions come from the server, never from a hardcoded list")


def test_no_second_surface_remains():
    assert client.get("/static/js/surfaces/chat.js").status_code == 404, \
        "chat.js is still served"
    r = client.get("/chat", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/ask", (
        f"GET /chat -> {r.status_code} {r.headers.get('location')}")
    print("  PASS  the doorway redirects; the second surface is gone")


def test_the_answer_tail_has_exactly_one_builder():
    """A live answer and a reopened transcript turn are painted by the same function. They
    were not, and the drift was #620: a reopened thread lost its sources and its footer while
    the live one kept them."""
    js = _ask_js()
    assert js.count("function renderResult") == 1, "more than one answer-tail builder"
    assert "renderResult(bot," in js or "renderResult(block," in js
    body = js.split("function transcriptTurn", 1)[1]
    assert "renderResult(" in body, (
        "transcriptTurn paints its own answer again instead of using the one builder - that "
        "is exactly how a reopened thread drifted away from a live one")
    print("  PASS  live and reopened answers share one tail builder")


if __name__ == "__main__":
    test_ask_carries_the_chat_presentation()
    test_ask_kept_every_capability_chat_never_had()
    test_the_share_teardown_is_still_the_only_route_down()
    test_no_invented_starter_questions_came_across()
    test_no_second_surface_remains()
    test_the_answer_tail_has_exactly_one_builder()
    print("\nMERGED SURFACE SELF-TEST PASSED.")
