# tests/e2e_admin.py
"""End-to-end: the Admin Console renders real Index Health and the Permission Tester is
permission-faithful through the browser — preview as alice (Falcon visible) vs bob (denied).

    python3 tests/e2e_admin.py
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

HOST, PORT = "127.0.0.1", 8078
BASE = f"http://{HOST}:{PORT}"


def _seed():
    c = TestClient(app)
    c.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={"external_id": "public-handbook", "title": "Staff Handbook",
        "text": "holidays expenses onboarding all staff", "acl": ["all-staff"], "uri": "https://x/h"})
    c.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={"external_id": "deal-falcon", "title": "Project Falcon — Confidential",
        "text": "confidential falcon valuation deal team", "acl": ["deal-team"], "uri": "https://x/f"})


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _e2eserver import serving  # noqa: E402  #846: start it, and be able to STOP it


def _preview_as(page, user):
    # #386/#446 (260731): "/" is the sign-in LANDING now, so "/#/admin" renders no shell.
    # The admin surface is path-routed (#309): the shell at /admin selects it directly.
    page.goto(BASE + "/admin", wait_until="domcontentloaded")
    page.wait_for_selector("#ptest-user")
    page.select_option("#ptest-user", user)
    page.click("#ptest-run")
    page.wait_for_selector(".ptest-summary")
    return page.eval_on_selector_all(".ptest-row.ok", "els => els.map(e => e.textContent).join(' | ')")


def main():
    print("Admin Console E2E (permission-faithful through the browser):")
    _seed()
    # #846: returns once the port is ACTUALLY listening, and is stopped in the
    # finally so the interpreter never finalizes under a live server thread.
    srv = serving(app, HOST, PORT)
    try:

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            # Index Health renders real numbers
            page.goto(BASE + "/admin", wait_until="domcontentloaded")
            page.wait_for_selector("#admin-index .admin-kv")
            index_text = page.inner_text("#admin-index")
            assert "Documents" in index_text, index_text

            # Sources panel renders and a resync updates the row (doc_count 0 -> 3)
            page.wait_for_selector("#admin-sources .src-row")
            assert "never" in page.inner_text("#admin-sources"), page.inner_text("#admin-sources")
            page.click("#admin-sources .src-resync")
            page.wait_for_function(
                "() => !document.querySelector('#admin-sources').innerText.includes('never')")
            sources_text = page.inner_text("#admin-sources")
            assert "3 doc(s)" in sources_text, sources_text

            alice_visible = _preview_as(page, "alice")
            bob_visible = _preview_as(page, "bob")
            browser.close()

        assert "Falcon" in alice_visible, f"alice should see Falcon, got: {alice_visible!r}"
        assert "Falcon" not in bob_visible, f"LAW 2 BREACH in admin UI: bob saw Falcon: {bob_visible!r}"
        print(f"  PASS  index health rendered: {index_text.splitlines()[0] if index_text else ''}")
        print("  PASS  sources panel: resync updated last-sync + doc count")
        print(f"  PASS  permission tester: alice sees Falcon, bob does not")
        print("\nADMIN CONSOLE E2E PASSED.")
    finally:
        srv.stop()

if __name__ == "__main__":
    main()
