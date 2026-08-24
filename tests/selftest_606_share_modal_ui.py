"""#606 / #610 - the share MODAL: audience picker, narrowing checklist, copy-link view.

WHAT THIS FILE ASSERTS ON, AND WHY IT IS NOT A FILE GREP. tests/selftest_600_share_ui.py
reads `static/js/surfaces/ask.js` off DISK. That proves a string exists in a file in the
repository - nothing more. Task 6 shipped four such tests for the visitor disclosure and the
review then found the page a visitor loads never rendered that module at all: four green
tests, zero visitors told anything.

So every content assertion here is made against the bytes an actual HTTP response hands the
browser, fetched through the real app, and the module is followed along the chain that puts
it in front of a user:

    GET /ask                        -> the shell, with a versioned main.js
    GET /static/js/main.js          -> imports the router
    GET /static/js/router.js        -> maps the "ask" route to mountAsk in surfaces/ask.js
    GET /static/js/surfaces/ask.js  -> the modal itself
    GET /static/css/app.css         -> the modal is actually styled, not unstyled markup

That chain is still not a browser: it proves the code reaches the client and parses, not that
a pixel appears. A real-browser pass over the modal is owed on top of this, and this file does
not claim to be one.

    PYTHONPATH=src python3 tests/selftest_606_share_modal_ui.py
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import _domgate  # noqa: E402  the shared jsdom gate (#792)
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)


def _served(path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} is not served: {r.status_code}"
    return r.text


def _joined(src):
    """Source with adjacent string literals spliced together.

    A sentence wrapped over two source lines as `"half " + "the rest"` is ONE sentence to the
    reader, and pinning the copy must not turn into pinning where the author broke the line.
    What is asserted is the text a user reads; the wrapping is style."""
    return re.sub(r'"\s*\+\s*"', "", src)


def _code(src):
    """The source with comments removed, string literals left intact.

    The absence-of-a-control assertions below have to read CODE. Run naively over the whole
    file they match the comment that EXPLAINS why no add-document control exists, which would
    make the test pass only as long as nobody documented the rule."""
    out, i, n = [], 0, len(src)
    quote = None
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1]); i += 2; continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'`":
            quote = c; out.append(c); i += 1; continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c); i += 1
    return "".join(out)


ASK = _served("/static/js/surfaces/ask.js")
API = _served("/static/js/api.js")
CSS = _served("/static/css/app.css")
ASK_CODE = _code(ASK)

# The DOM probe drives the module on DISK, because the module imports its siblings by relative
# path and only a real tree can satisfy that. The two are asserted equal below, so driving one
# is driving the other.
ASK_PATH = ROOT / "src/dbsearch/server/static/js/surfaces/ask.js"
JSDOM = _domgate.JSDOM
PROBE = ROOT / "tests/share_modal_dom_probe.mjs"
_dom = {}


def _report():
    """Mount the ask surface in a real DOM, click Share, and read what is there.

    Returns None when node or jsdom is unavailable, and the DOM assertions then skip - the
    same stance selftest_557 takes on `node --check`. A skipped DOM check is reported, never
    silently counted as a pass."""
    if "r" not in _dom:
        if not _domgate.gate("the share-modal DOM check"):
            _dom["r"] = None                           # permitted skip, already counted
        else:
            _dom["r"] = _domgate.run_node(
                ["node", str(PROBE), str(JSDOM), str(ASK_PATH)], "the share modal")
    return _domgate.resolve(_dom["r"])


def _skip_dom():
    """The DOM half did not run. `_report` has already printed and counted why."""
    return True

# The copy is product, not decoration (spec s3). Verbatim, both of them.
SCOPE_NOTE = ("Only these. Nothing else in your workspace is reachable, and nobody can "
              "download the files.")
LINK_NOTE = ("Anyone with this link can read this conversation and ask questions from its "
             "documents until it expires.")
PEOPLE_LABEL = "Specific people - they sign in, named by email"
LINK_LABEL = "Anyone with the link - no sign-in, link is the key"


# ---- the module actually reaches a user -------------------------------------------------

def test_the_ask_shell_loads_the_module_that_carries_the_modal():
    """The chain, through responses. A modal built in a module nothing imports is the exact
    failure Task 6's review found, and no amount of grepping the file would show it."""
    shell = _served("/ask")
    assert re.search(r'src="/static/js/main\.js\?v=', shell), \
        "the ask shell no longer loads a versioned main.js"
    main = _served("/static/js/main.js")
    assert "./router.js" in main, "main.js no longer reaches the router"
    router = _served("/static/js/router.js")
    assert "./surfaces/ask.js" in router and "mountAsk" in router, \
        "the router no longer mounts the ask surface, so the share modal is unreachable"


def test_the_served_module_parses():
    node = subprocess.run(["which", "node"], capture_output=True, text=True)
    if node.returncode != 0:
        print("      (node not installed - skipping the parse check)")
        return
    for name, src in (("ask.js", ASK), ("api.js", API)):
        r = subprocess.run(["node", "--check", "--input-type=module", "-"],
                           input=src, capture_output=True, text=True)
        assert r.returncode == 0, f"the served {name} does not parse: {r.stderr[:300]}"


# ---- the modal, and both audiences in it -------------------------------------------------

def test_the_share_control_opens_a_modal_and_the_old_drawer_is_gone():
    assert 'id: "share-modal"' in ASK or '"share-modal"' in ASK, \
        "no share modal container in the served ask surface"
    assert "share-modal-backdrop" in ASK, \
        "the modal has no backdrop, so it is a drawer with a new name"
    assert "buildShareModal" in ASK, "the modal builder is missing"
    assert "buildShareBox" not in ASK and "share-box" not in ASK, \
        "the old drawer-only share box is still built"
    assert '"share-drawer"' not in ASK, "the old drawer container id is still rendered"
    assert ".share-modal" in CSS, \
        "the modal has no styles - it would render as unstyled markup over the page"


def test_both_audience_labels_are_offered_verbatim():
    assert PEOPLE_LABEL in ASK, f"the people-audience label is missing or has drifted"
    assert LINK_LABEL in ASK, f"the link-audience label is missing or has drifted"
    assert 'type: "radio"' in ASK, "the audience is not a choice - there is no radio group"
    assert '"link"' in ASK and '"people"' in ASK, \
        "the modal never names the two audiences the share POST accepts"


def test_the_link_audience_is_actually_sent_and_the_email_field_is_people_only():
    assert "audience" in API, "api.js never sends an audience, so every share is a people share"
    assert re.search(r"grantee_email", API), "the people path no longer sends the email"
    assert re.search(r"expires_in_days", API), "expiry is no longer sent"


# ---- the narrowing checklist, and the structural rule ------------------------------------

def test_the_checklist_is_built_from_the_shareable_route():
    assert "shareableDocs" in API and "/shareable" in API, \
        "api.js has no client for GET /conversations/{id}/shareable"
    assert "shareableDocs(" in ASK, "the modal never asks the server what it would expose"
    assert "share-doc-list" in ASK, "no document checklist container in the modal"
    assert "exclude_docs" in API, "the unchecked documents are never sent as exclude_docs"
    assert "excludeDocs" in ASK, "the modal computes no exclusions from its own checklist"


def test_there_is_no_add_document_affordance_anywhere_in_the_dom():
    """THE structural assertion of this task. The checklist can only UNCHECK. The guarantee
    is that the control does not exist - not that it is disabled, not that it is hidden - so
    what is asserted is the absence of any code that could build one."""
    # The comment stripper must not be silently eating the file - if it were, every assertion
    # below would hold vacuously.
    assert "buildShareModal" in ASK_CODE and len(ASK_CODE) > 4000, \
        "the comment stripper removed the code it was meant to leave behind"
    forbidden = [r"add[-_ ]?doc", r"doc[-_ ]?add", r"Add a document", r"Add document",
                 r'type:\s*"file"', r'type:\s*"search"', r"datalist", r"multiple:"]
    for pat in forbidden:
        assert not re.search(pat, ASK_CODE, re.I), \
            f"the served share surface contains an add-document affordance ({pat})"
    # Exactly one checkbox is ever constructed, in the row builder fed by /shareable. A
    # second construction site is how a "just one more document" control gets in.
    n = ASK_CODE.count('type: "checkbox"')
    assert n == 1, (
        f"a checkbox is built in {n} places - the checklist must have exactly one row "
        "builder, fed only by the server's shareable list")


def test_the_honesty_copy_is_present_verbatim():
    joined = _joined(ASK)
    assert SCOPE_NOTE in joined, "the narrowing/no-download sentence is missing or has drifted"
    assert LINK_NOTE in joined, "the copy-link view's what-this-link-does sentence is missing"


def test_unshareable_documents_are_shown_greyed_and_never_checkable():
    assert "not yours to share" in ASK, \
        "a document the owner cannot pass on is not explained - it would look lost"
    assert re.search(r"shareable", ASK), "the modal ignores the shareable flag"
    assert "share-doc-blocked" in ASK and "share-doc-blocked" in CSS, \
        "an unshareable row is not visually distinguished from a shareable one"


# ---- the token is shown once -------------------------------------------------------------

def test_the_copy_link_view_shows_the_full_url_once_with_a_copy_button():
    assert "location.origin" in ASK, \
        "the copy-link view shows a path, not a URL anybody could open"
    assert re.search(r"clipboard", ASK), "there is no Copy control on the link"
    assert re.search(r"shown once", ASK, re.I), \
        "the owner is not told the link cannot be shown again"
    assert "share-link-url" in ASK and "share-link-url" in CSS, \
        "the link itself has no field to be read or selected from"


def test_closing_the_modal_over_an_uncopied_link_is_guarded():
    """If the modal can be dismissed before the owner has copied a link that can never be
    fetched again, the share is dead and only the recipient finds out."""
    assert re.search(r"without copying", ASK, re.I), \
        "closing over an uncopied one-time link is not guarded at all"


# ---- the existing shares list stays, and both audiences render ---------------------------

def test_the_existing_shares_list_lives_in_the_modal_and_names_both_audiences():
    assert "conversationShares(" in ASK, "the modal no longer lists existing shares"
    assert "revokeConversationShare(" in ASK, "revoke is gone from the share surface"
    assert "Anyone with the link" in ASK, "a link share has no row label of its own"
    assert re.search(r"\bopens\b", ASK), \
        "a link row does not report how many times it has been opened"


# ---- what a user actually finds in the modal, read out of a real DOM ---------------------
#
# Everything above is a string search over bytes a response handed the client. Everything
# below MOUNTS the surface, clicks the Share button, and inspects the resulting DOM - the
# difference between "the file says it" and "the control is there".

def test_dom_the_served_module_is_the_one_the_probe_drives():
    assert ASK == ASK_PATH.read_text(), \
        "the served ask.js differs from the file on disk - the DOM probe proves nothing"


def test_dom_the_modal_opens_with_both_audiences_the_checklist_and_the_copy():
    r = _report()
    if r is None: return _skip_dom()
    assert r["modal_present"], "clicking Share built no modal at all"
    for label in (PEOPLE_LABEL, LINK_LABEL, SCOPE_NOTE):
        assert label in r["form_text"], f"the open modal does not show: {label}"
    assert [d["text"] for d in r["doc_rows"]], "the checklist rendered no documents"
    # #851: two documents AND the two sources the fixture's thread drew on. The count is the
    # owner's one summary of what she is handing over, so it names both kinds.
    assert r["count_line"] == "2 documents and 2 sources will be shared.", r["count_line"]


def test_dom_the_only_controls_are_narrowing_ones():
    """Counted, not grepped: the modal's inputs are two audience radios, one checkbox per
    SHAREABLE document, #851's one checkbox per SOURCE, the email field and the expiry field.
    Nothing else exists to click, which is what makes 'you cannot add anything here' a fact
    about the DOM rather than a promise about a handler.

    The count is EXACT and stays exact. A source row is the same narrowing control as a
    document row - it is built by the same one builder - so it widens what is counted here and
    changes nothing about what may be clicked."""
    r = _report()
    if r is None: return _skip_dom()
    kinds = sorted(i["type"] for i in r["form_inputs"])
    assert kinds == ["checkbox", "checkbox", "checkbox", "checkbox",
                     "number", "radio", "radio", "text"], \
        f"unexpected controls in the share modal: {r['form_inputs']}"
    assert all(i["checked"] for i in r["form_inputs"] if i["type"] == "checkbox"), \
        "a row arrived unticked - the checklist narrows from everything, it does not build up"
    for b in r["form_buttons"]:
        assert not re.search(r"\badd\b|\+", b, re.I), \
            f"a button in the share modal offers to add something: {b!r}"
    assert r["form_buttons"].count("Share") == 1, r["form_buttons"]


def test_dom_an_unshareable_document_is_shown_but_has_no_checkbox():
    r = _report()
    if r is None: return _skip_dom()
    blocked = [d for d in r["doc_rows"] if d["blocked"]]
    assert len(blocked) == 1, f"the unshareable document was hidden: {r['doc_rows']}"
    assert "not yours to share" in blocked[0]["text"], blocked[0]
    assert not blocked[0]["has_checkbox"], \
        "the unshareable document has a checkbox - it is not the owner's to pass on"
    # ...and it is not in the count, which is the number she acts on.
    assert r["count_line"].startswith("2 "), r["count_line"]


def test_dom_unchecking_a_document_narrows_the_request_that_is_sent():
    r = _report()
    if r is None: return _skip_dom()
    # #851: the count speaks about BOTH kinds now, because it is the owner's one summary of
    # what she is handing over - "1 document will be shared" under a checklist whose third
    # ticked row is a warehouse would be quietly false.
    assert r["count_after_uncheck"] == "1 document and 2 sources will be shared.", \
        r["count_after_uncheck"]
    assert r["email_row_hidden_in_link_mode"], \
        "link mode still shows an email field, so the owner can name somebody who is not asked for"
    posted = r["posted"][0]
    assert posted["audience"] == "link", posted
    assert posted["exclude_docs"] == ["d-sales"], \
        f"the unchecked document did not reach the server as an exclusion: {posted}"


def test_dom_a_source_is_offered_in_the_same_checklist_and_says_what_it_hands_over():
    """#851, the owner's ruling on #850. A turn the router answered came from a connected
    database: there is no grant to mint and nothing for the recipient's permissions to be
    checked against, so what makes it shareable is the GRANTOR SAYING SO - in the same list,
    because "what does this hand over" is one question and splitting it across two dialogs is
    how an owner answers only half of it.

    The label is asserted verbatim because it is the honest half people miss: what travels is
    a RECORD of what she saw, not live access, and the figures do not refresh."""
    r = _report()
    if r is None: return _skip_dom()
    rows = [x for x in r["doc_rows"] if "Azure SQL" in x["text"] or "BigQuery" in x["text"]]
    assert len(rows) == 2, f"the sources were not offered in the checklist: {r['doc_rows']}"
    for row in rows:
        assert row["has_checkbox"], f"a source row has no checkbox to untick: {row}"
        assert "shared as a record, as it was then" in row["text"], row["text"]
    assert any("Sources this conversation used" in x["text"] for x in r["doc_rows"]), \
        "the sources are mixed in with the documents with nothing to tell them apart"


def test_dom_unticking_a_source_narrows_the_request_the_same_way_a_document_does():
    """The narrowing rule, one list out. TWO sources are offered on purpose: with one, "sent
    the unticked id" and "sent every id" look identical, and a rig that cannot tell a working
    narrow from a broken one proves nothing."""
    r = _report()
    if r is None: return _skip_dom()
    assert r["count_after_source_uncheck"] == "1 document and 1 source will be shared.", \
        r["count_after_source_uncheck"]
    posted = r["posted"][0]
    assert posted.get("exclude_stores") == ["bigquery-1"], \
        f"the unticked source did not reach the server as an exclusion: {posted}"
    assert posted.get("exclude_docs") == ["d-sales"], \
        f"unticking a source disturbed the document exclusions: {posted}"


def test_dom_the_copy_link_view_shows_the_whole_url_and_says_what_it_does():
    r = _report()
    if r is None: return _skip_dom()
    assert r["link_field_value"] == "http://localhost/c/TOKEN-abc123", \
        f"the link shown is not one anybody could open: {r['link_field_value']!r}"
    assert LINK_NOTE in r["link_view_text"], "the copy-link view does not say what the link does"
    assert "shown once" in r["link_view_text"], "the owner is not told this is the only showing"
    assert r["copied_text"] == ["http://localhost/c/TOKEN-abc123"], \
        f"Copy put something else on the clipboard: {r['copied_text']}"


def test_dom_an_uncopied_link_survives_the_first_dismissal():
    """The show-once problem, answered. The first dismissal - here by the BACKDROP, the route
    the surface owns rather than the one the modal owns - refuses and explains; the second
    closes. After a copy, one click is enough and there is nothing to warn about."""
    r = _report()
    if r is None: return _skip_dom()
    assert r["closed_on_first_backdrop_click"] is False, \
        "an uncopied one-time link was dismissed by a stray click on the backdrop"
    assert "without copying" in r["close_label_after_first_attempt"].lower(), \
        r["close_label_after_first_attempt"]
    assert "cannot be shown again" in r["guard_note"], r["guard_note"]
    assert r["closed_on_second_click"] is True, "the modal cannot be closed at all"
    assert r["closed_after_copy"] is True, \
        "the modal still nags after the link has been copied"


# ---- fix round 1: no route may take the modal down behind the guard ----------------------

def test_the_navigation_routes_go_through_the_one_guarded_teardown():
    """Structural, on the served code. Every navigation that REPLACES the conversation the
    modal belongs to used to tear it down directly, destroying a one-time link with no warning.
    Each must now ask the single teardown and obey a refusal, and there must be exactly one
    teardown for them to ask.

    #602 changed the shape and not the rule. There were two such navigations ("New
    conversation", and opening a thread somebody shared with you) and the card added a third -
    a row in the owner's own "Your conversations" list. Rather than a third call site for a
    fourth to forget, every thread-REPLACING navigation now runs through `openConversation`,
    which asks once; `openSharedConversation` is a thin wrapper on it. So the list checked here
    is the list of functions that replace the thread, whatever they are called, and the DOM
    tests below plus selftest_602's own drive all three routes through a real DOM."""
    assert ASK_CODE.count("function dismissShareModal(") == 1, \
        "there is not exactly one teardown - a second one is a second bypass"
    assert "function closeShareModal(" not in ASK_CODE, \
        "the unguarded teardown is back; new callers will reach for it"
    for fn in ("function resetConversation()", "async function openConversation("):
        assert fn in ASK_CODE, \
            f"{fn} is gone - a navigation was renamed or split, so this check pins nothing"
        i = ASK_CODE.index(fn)
        head = ASK_CODE[i:i + 500]
        assert "if (!dismissShareModal()) return;" in head, \
            f"{fn} does not ask the guarded teardown, or does not obey its refusal"
    # ...and no navigation may reach the transcript without going through it.
    assert ASK_CODE.count("conversationTranscript(") == 1, (
        "a second call site reads a transcript directly - it has bypassed the one guarded "
        "navigation")


def test_dom_new_conversation_cannot_silently_destroy_an_uncopied_link():
    r = _report()
    if r is None: return _skip_dom()
    assert r["reset_closed_modal_on_first_click"] is False, \
        "'New conversation' destroyed an uncopied one-time link with no warning"
    assert "cannot be shown again" in r["reset_guard_note"], r["reset_guard_note"]
    assert r["reset_happened_on_first_click"] is False, \
        "the conversation was reset anyway - the loss happened, the dialog just stayed up"
    assert r["reset_closed_modal_on_second_click"] is True, \
        "the owner cannot start a new conversation at all once a link has been shown"
    assert r["reset_happened_on_second_click"] is True, \
        "the second click closed the modal but never performed the reset she asked for"


def test_dom_opening_a_shared_thread_cannot_silently_destroy_an_uncopied_link():
    r = _report()
    if r is None: return _skip_dom()
    assert r["shared_open_closed_modal_on_first_click"] is False, \
        "clicking a shared conversation destroyed an uncopied one-time link with no warning"
    assert "cannot be shown again" in r["shared_open_guard_note"], r["shared_open_guard_note"]
    assert r["shared_open_happened_on_first_click"] is False, \
        "the shared thread was opened over the top of the link anyway"
    assert r["shared_open_closed_modal_on_second_click"] is True, \
        "the owner can no longer open a thread shared with her"
    assert r["shared_open_happened_on_second_click"] is True, \
        "the second click dismissed the modal but never opened the thread"


def test_dom_the_modal_traps_focus():
    """WHAT THIS DOES AND DOES NOT PROVE. jsdom does not implement native tab traversal, so it
    cannot show where focus would have gone WITHOUT the trap - this cannot demonstrate the
    Shift+Tab escape onto 'New conversation' that motivated it. What it does prove is the
    trap's own contract, which is the code that prevents that escape in a browser: focus lands
    inside on open, Tab at the last control wraps to the first, Shift+Tab at the first wraps to
    the last, and focus parked outside is pulled back in. The browser half is still owed and is
    recorded as such in the report."""
    r = _report()
    if r is None: return _skip_dom()
    assert r["focus_on_open"]["inside"], "opening the modal left focus on the page behind it"
    assert r["shift_tab_landed_on_last"], \
        "Shift+Tab at the first control escaped the modal instead of wrapping to the last"
    assert r["tab_landed_on_first"], "Tab at the last control escaped the modal"
    for k in ("focus_after_shift_tab_from_first", "focus_after_tab_from_last",
              "focus_after_tab_from_outside"):
        assert r[k]["inside"], f"{k}: focus left the modal - {r[k]}"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
