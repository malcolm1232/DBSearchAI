"""#600 Task 6 - the Ask surface must actually be WIRED to the share API Task 5 shipped.

Every backend route (share / list / revoke / transcript / shared-with-me) has heavy
coverage in tests/selftest_600_share_api.py and its neighbours, all of which drive real
FastAPI routes and never touch a byte of static/js. A UI that imports the wrong function
name, builds a dialog that is never mounted, or drops the honesty copy the product spec
requires would ship with every one of those green.

This follows tests/selftest_561_upload_card.py's approach - grep the SERVED static asset
for the exact wiring a half-built surface would be missing - plus
tests/selftest_557_rendered_shell_parses.py's belt-and-braces stance of handing the file to
a real JS engine when one is available. It does not drive a browser; Task 7 does that.

    PYTHONPATH=src python3 tests/selftest_600_share_ui.py
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import _domgate  # noqa: E402  the shared node/jsdom gate (#792)
STATIC = ROOT / "src/dbsearch/server/static"
API_JS = (STATIC / "js/api.js").read_text()
ASK_JS = (STATIC / "js/surfaces/ask.js").read_text()

# #600 Task 5 shipped a deliberately different shape than the Task 6 brief documents (the
# deviation was reviewed and endorsed) - these are the five routes the SHIPPED backend
# actually exposes, and what this file pins the surface against.
EXPORTS = ["shareConversation", "conversationShares", "revokeConversationShare",
           "sharedWithMe", "conversationTranscript"]
ROUTE_LITERALS = [
    "/conversations/${encodeURIComponent(convId)}/shares",
    "/conversations/shares/${encodeURIComponent(shareId)}",
    "/conversations/shared-with-me",
    "/conversations/${encodeURIComponent(convId)}/transcript",
]


def test_api_js_exports_all_five_helpers():
    for name in EXPORTS:
        assert re.search(rf"export async function {name}\(", API_JS), \
            f"api.js does not export {name}() - the ask surface has nothing to import"


def test_api_js_calls_every_route_task_5_actually_shipped():
    for route in ROUTE_LITERALS:
        assert route in API_JS, f"no api.js helper calls {route}"


def test_ask_surface_imports_the_share_helpers():
    imp = re.search(r'import\s*\{([^}]*)\}\s*from\s*"\.\./api\.js"', ASK_JS, re.S)
    assert imp, "ask.js no longer imports from api.js"
    names = {n.strip() for n in imp.group(1).split(",") if n.strip()}
    missing = [n for n in EXPORTS if n not in names]
    assert not missing, f"ask.js does not import {missing} from api.js"


def test_ask_surface_actually_calls_every_helper_it_imports():
    """An import with no call site is exactly the half-wired state this file exists to
    catch - the same failure mode selftest_561 pins for the Upload card."""
    for name in EXPORTS:
        assert re.search(rf"\b{name}\(", ASK_JS), \
            f"ask.js imports {name} but never calls it"


def test_share_button_is_gated_on_an_answered_turn_not_always_shown():
    """Step 2.1: offered beside the conversation once it has at least one answered turn -
    an unconditional Share button would be the #551 always-fails tile on a fresh thread,
    since a zero-turn conversation is refused server-side (400)."""
    assert re.search(r'"share-conversation"', ASK_JS), "no Share control in the ask surface"
    assert "display:none" in ASK_JS or "display: none" in ASK_JS, (
        "the Share control has no hidden initial state - it would show before anything "
        "has been asked")


def test_shared_with_you_section_is_wired_to_open_a_conversation():
    assert "shared-with-you" in ASK_JS, "no 'Shared with you' section in the ask surface"
    assert "conversationTranscript(" in ASK_JS, (
        "clicking a shared conversation never reads its transcript")


def test_a_dead_share_says_so_and_is_never_a_blank_page():
    """A revoked or expired share resolves to null (api.js's 404-as-null idiom) and must be
    told, in words, not rendered as nothing."""
    assert re.search(r"no longer active", ASK_JS, re.I), (
        "a revoked/expired share must say 'This share is no longer active', "
        "not render a blank page")


def _ask_submit_body():
    """The body of ask.js's submit(), where a question is sent and its answer rendered."""
    start = ASK_JS.index("async function submit(")
    return ASK_JS[start:]


def test_a_dead_share_is_also_caught_on_the_NEXT_question_not_only_on_load():
    """Acceptance step 8. The refusal in openSharedConversation only fires when the
    transcript is loaded. If Bob revokes while Alice has the page open, her next question
    goes down the ordinary ask path, retrieves from an empty corpus, and the model answers
    from nothing - a fabrication where a refusal was required. The information needed is
    already on the /chat response (the #393 corpus block); a half-wired fix that renders the
    state on load only would pass every other test in this file."""
    body = _ask_submit_body()
    assert "authorized_docs" in body, (
        "the ask path never reads the answer's corpus denominator, so a revoked share is "
        "invisible until the page is reloaded")
    assert re.search(r"shareEndedNotice\(", body), (
        "the ask path never renders the 'no longer active' state - a revoked share's next "
        "question would show the model's answer")
    assert re.search(r"sharedConv\b", body), (
        "the ask path does not distinguish a thread shared WITH this caller from her own, "
        "so it would claim a share ended on a conversation nobody shared")


def test_the_dead_share_state_is_ONE_string_and_ONE_treatment():
    """Both discovery points (transcript load, next question) must render the same thing.
    Two copies drift, and a user who sees two different refusals learns to trust neither.

    #602 kept the rule and moved one reference: the transcript-load site is now inside
    `openConversation`, which is handed the notice builder (`gone: shareEndedNotice`) because
    the SAME function also opens the owner's own threads, where "ask whoever shared it with
    you" would name a person who does not exist. So the helper is referenced without calling
    parentheses at one of its two sites, and what is pinned is that the SENTENCE exists once
    and the helper is reached from both."""
    assert ASK_JS.count("no longer active") == 1, (
        "'This share is no longer active' is written more than once - it must live in the "
        "single shareEndedNotice() helper both call sites use")
    assert len(re.findall(r"shareEndedNotice\b", ASK_JS)) >= 3, (
        "shareEndedNotice is not shared by both call sites (declaration + two uses)")
    assert "gone: shareEndedNotice" in ASK_JS, (
        "the transcript-load site no longer renders the shared dead-share notice")


def test_the_shared_thread_flag_is_cleared_when_the_conversation_is_reset():
    """New conversation makes the thread the caller's OWN. If the flag survived, her first
    question on an empty index would be refused as 'this share is no longer active' - a
    false statement about a share that never existed, which is the #392 bug class."""
    reset = re.search(r"function resetConversation\(\)\s*\{(.*?)\n  \}", ASK_JS, re.S)
    assert reset, "resetConversation() is gone or has been reshaped"
    assert re.search(r"sharedConv\s*=\s*false", reset.group(1)), (
        "resetConversation() does not clear the shared-thread flag")


def test_the_turns_withheld_disclosure_is_not_buried():
    """Product fact #1: turns_withheld is non-zero when some of the sharer's own turns did
    not travel because they draw on a document she cannot pass on, and the UI must say so
    plainly rather than silently under-reporting `documents`."""
    assert "turns_withheld" in ASK_JS, (
        "the withheld-turns count from the share response is never read")
    assert re.search(r"not shared\b.*\bcannot pass on", ASK_JS, re.S), (
        "the withheld-turns sentence is missing or has drifted from the honest wording")
    # ADR 0020 s5: withholding PROPAGATES. Only the first held-back turn is held back by
    # its own citations; every later one goes because it follows on from that turn. Copy
    # naming only the citation reason is wrong for every turn after the first.
    assert re.search(r"or follow on from", ASK_JS), (
        "the withheld-turns sentence blames citations alone - it must also say the turns "
        "follow on from documents the sharer cannot pass on (ADR 0020 s5 propagation)")


def test_the_snapshot_and_reshare_narrowing_rule_is_stated():
    """Product fact #2: a share is a snapshot at share time, and re-sharing can narrow what
    a recipient already saw - the copy must not claim only growth is possible."""
    assert re.search(r"only what has been said so far", ASK_JS), (
        "the snapshot sentence is missing from the share dialog")
    assert re.search(r"\bnarrow\b", ASK_JS), (
        "the dialog does not warn that a re-share can narrow what a recipient already saw")


def test_the_grantors_half_is_marked_so_she_cannot_mistake_it_for_her_own():
    """A recipient reading a shared thread must never be left believing she wrote the
    grantor's half - each transcript turn's `own` flag has to reach the rendered markup."""
    assert re.search(r"\bt\.own\b", ASK_JS), (
        "ask.js never branches on a transcript turn's own flag")
    assert "grantorLabel" in ASK_JS or re.search(r"grantor", ASK_JS, re.I), (
        "the grantor's turns are not labelled as anyone else's")


def test_the_two_files_still_parse_as_javascript():
    if not _domgate.gate("the share-UI parse check"):
        return          # permitted skip (DBSEARCH_ALLOW_DOM_SKIP), already counted
    for path in (STATIC / "js/api.js", STATIC / "js/surfaces/ask.js"):
        r = subprocess.run(["node", "--check", "--input-type=module", "-"],
                           input=path.read_text(), capture_output=True, text=True)
        assert r.returncode == 0, f"{path} does not parse: {r.stderr[:300]}"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
