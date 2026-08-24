# tests/e2e_ask.py
"""End-to-end: the Ask UI returns cited answers AND is permission-faithful through the
browser — ask the SAME question as alice vs bob, assert the restricted source appears only
for alice (LAW 2, visible in the sources rail).

Starts the app on 127.0.0.1:8077 in-process, seeds two docs (one restricted), then drives
Chromium.

    python3 tests/e2e_ask.py
"""
import os
import sys
import threading
import time
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

HOST, PORT = "127.0.0.1", 8077
BASE = f"http://{HOST}:{PORT}"
DEAL = "deal-falcon"


def _seed():
    c = TestClient(app)
    c.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={"external_id": "public-handbook", "title": "Staff Handbook",
        "text": "General staff handbook: holidays, expenses, onboarding for all staff.",
        "acl": ["all-staff"], "uri": "https://example/handbook"})
    c.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={"external_id": DEAL, "title": "Project Falcon — Confidential",
        "text": "Confidential Project Falcon merger acquisition target valuation, deal team only.",
        "acl": ["deal-team"], "uri": "https://example/falcon"})


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _e2eserver import serving  # noqa: E402  #846: start it, and be able to STOP it


# #309: "/" is the landing now and hands off to /canvas. The app shell keeps its own
# URLs, so this drives /app (the Ask surface is its default route) instead of "/".
def _ask_as(page, user, question):
    page.goto(BASE + "/app", wait_until="domcontentloaded")
    page.wait_for_selector("#user-select")
    page.select_option("#user-select", user)
    _fresh_thread(page)
    _turn(page, question)
    # #678: sources moved BEHIND a pill in the #643 shell. The pill is what a user sees
    # first and it states the trim in words - "Sources · 1 of 2 you can access" - so it is
    # asserted alongside the titles underneath.
    #
    # NO PILL AT ALL IS A VALID, AND THE STRONGEST, OUTCOME. An identity who is entitled to
    # nothing the question matches retrieves nothing, so there is no pill to render. Treating
    # that as an error would turn the cleanest LAW 2 pass into a red test - which is exactly
    # what it did on the first run of this rewrite. `_turn` has already proved the page
    # answered, so an absent pill here means "no sources", not "nothing happened".
    pill = None
    for _ in range(10):
        pill = page.query_selector(".sources-pill")
        if pill:
            break
        time.sleep(0.3)
    if not pill:
        return "", ""
    pill_text = (pill.text_content() or "").strip()
    pill.click()
    page.wait_for_selector(".sources-panel .source-card")
    titles = page.eval_on_selector_all(".sources-panel .source-card .title",
                                       "els => els.map(e => e.textContent)")
    return " ".join(titles), pill_text


def _fresh_thread(page):
    """Start from an empty thread (#678).

    The old test relied on a reload being a reset. Since #643 the shell restores your last
    conversation from the rail, so a reload can hand the next assertion the PREVIOUS
    identity's answer - which would read as a pass while proving nothing. Ask for a new
    conversation explicitly instead of assuming.
    """
    page.click("#new-conversation")
    page.wait_for_function("() => document.querySelectorAll('.msg').length === 0")


def _turn(page, question):
    """Ask one question and wait for a settled bot answer."""
    n_before = page.eval_on_selector_all(".msg-bot", "els => els.length")
    page.fill("#ask-input", question)
    page.click(".ask-btn")
    page.wait_for_function(
        "n => document.querySelectorAll('.msg-bot').length > n", arg=n_before)
    # The bubble is created immediately holding a placeholder, so "it exists" is not "it
    # answered". Wait for the LAST bot body to stop saying Searching and carry real text.
    page.wait_for_function(
        "() => { const b = [...document.querySelectorAll('.msg-bot .msg-body')].pop();"
        "        return b && b.textContent.trim().length > 0"
        "               && !b.textContent.trim().startsWith('Searching'); }")


def _multi_turn(page):
    page.goto(BASE + "/app", wait_until="domcontentloaded")
    page.wait_for_selector("#user-select")
    page.select_option("#user-select", "alice")
    _fresh_thread(page)
    _turn(page, "What is in the handbook?")
    # Turn 2 (follow-up) — the thread keeps the first exchange AND adds a second.
    _turn(page, "what about Falcon?")
    bots = page.eval_on_selector_all(".msg-bot", "els => els.length")
    users = page.eval_on_selector_all(".msg-user", "els => els.length")
    assert bots == 2 and users == 2, f"expected 2 turns each way, got {users} user / {bots} bot"
    # New conversation resets the thread.
    page.click("#new-conversation")
    page.wait_for_function("() => document.querySelectorAll('.msg').length === 0")
    print("  PASS  multi-turn thread stacks turns; New conversation resets it")


def main():
    print("Ask UI E2E (permission-faithful through the browser):")
    _seed()
    # #846: `serving` returns once the port is ACTUALLY listening (not after a hopeful
    # sleep) and is stopped in the finally, so the interpreter never finalizes under a
    # live server thread - the race that made this file fail intermittently in the suite
    # while passing standalone.
    srv = serving(app, HOST, PORT)
    try:
        from playwright.sync_api import sync_playwright
        q = "confidential falcon merger acquisition valuation"
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            alice_sources, alice_pill = _ask_as(page, "alice", q)
            bob_sources, bob_pill = _ask_as(page, "bob", q)

            assert "Falcon" in alice_sources, f"alice should see the Falcon source, got: {alice_sources!r}"
            assert "Falcon" not in bob_sources, f"LAW 2 BREACH in UI: bob saw Falcon: {bob_sources!r}"
            # The pill is prose, and prose is its own channel: a fix can close the source list
            # and leave the count claiming the restricted document is reachable.
            assert "Falcon" not in bob_pill, f"LAW 2 BREACH in the pill text: {bob_pill!r}"
            print(f"  PASS  alice sources={alice_sources!r} · pill={alice_pill!r}")
            print(f"  PASS  bob sources={bob_sources!r} · pill={bob_pill!r} (restricted doc hidden)")

            _multi_turn(page)

            browser.close()
    finally:
        srv.stop()

    print("\nASK UI E2E PASSED — cited answers + permission-trim visible in the browser.")


if __name__ == "__main__":
    main()
