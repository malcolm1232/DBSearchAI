# tests/e2e_draft.py
"""End-to-end: the Draft UI returns permission-faithful cited sections through the
browser — draft the SAME query as alice vs bob, assert the restricted source (Project
Falcon) appears only for alice (LAW 2, visible in the sources rail).

Starts the app on 127.0.0.1:8079 in-process, seeds two docs (one restricted), then drives
Chromium. The port is deliberately NOT 8078 - e2e_admin.py binds that one and runs first in
the suite, so sharing it left this test one TIME_WAIT away from a bind failure.

    python3 tests/e2e_draft.py
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

HOST, PORT = "127.0.0.1", 8079
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


def _draft_as(page, user):
    """Drive the TWO-PHASE conversational draft (#57/#59): type the brief, 'Ready to
    draft' folds it into a requirements summary, 'Confirm & draft' streams the
    sections. (The old single 'Draft proposal' button UI is gone - this test tracks
    the shipped surface.)

    #890: the labels lost their trailing '▸'. They are matched here EXACTLY rather than by
    substring, because "Ready to draft" is also a prefix of nothing else on the surface and
    an accidental substring match is how a click starts landing on the wrong control."""
    page.goto(BASE + "/draft", wait_until="domcontentloaded")   # #309: real URL, not a hash route
    page.wait_for_selector("#user-select")
    page.select_option("#user-select", user)
    page.fill("#draft-input", "Confidential acquisition advisory and staff onboarding for a bank.")
    page.click("button:text-is('Ready to draft')")
    page.click("button:text-is('Confirm & draft')")
    page.wait_for_selector(".draft-section")
    # streaming done = no section still streaming and every section has its footer note
    page.wait_for_function(
        "() => document.querySelectorAll('.draft-section').length > 0 && "
        "document.querySelectorAll('.draft-section.streaming').length === 0 && "
        "document.querySelectorAll('.authorized-note').length === "
        "document.querySelectorAll('.draft-section').length",
        timeout=30000,
    )
    return page.inner_text("#draft-thread")


def main():
    print("Draft UI E2E (permission-faithful through the browser):")
    _seed()
    # #846: returns once the port is ACTUALLY listening, and is stopped in the
    # finally so the interpreter never finalizes under a live server thread.
    srv = serving(app, HOST, PORT)
    try:

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            # FRESH CONTEXT PER USER: alice's finished thread would otherwise stay in the
            # DOM and bob's assertion would read HER sections (false breach). /draft is a real
            # URL since #309, but a fresh context is still the honest isolation.
            ctx_a = browser.new_context()
            alice_text = _draft_as(ctx_a.new_page(), "alice")
            ctx_a.close()
            ctx_b = browser.new_context()
            bob_text = _draft_as(ctx_b.new_page(), "bob")
            ctx_b.close()
            assert "Falcon" in alice_text, f"alice should see Falcon in her draft sources: {alice_text!r}"
            assert "Falcon" not in bob_text, f"LAW 2 BREACH: bob's draft surfaced Falcon: {bob_text!r}"
            print("  PASS  alice cited Falcon; bob did not")
            browser.close()

        print("\nDRAFT UI E2E PASSED — agent draft is permission-faithful in the browser.")
    finally:
        srv.stop()

if __name__ == "__main__":
    main()
