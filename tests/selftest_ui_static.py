"""Static serving self-test: the app shell is served at / and assets under /static,
without shadowing the API routes.

    python3 tests/selftest_ui_static.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SRC = Path(__file__).resolve().parents[1] / "src" / "dbsearch"

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)


def test_an_anchor_button_is_not_left_looking_like_a_link():
    """#939: the canvas renders some actions as <a class="btn"> (the store file list's "Open"
    opens the source in a new tab, which is a link's job). `.btn` declares no colour and no
    text-decoration - right for <button>, wrong for <a>, which keeps the UA's blue underline -
    so without this rule an Open sits underlined and blue beside the plain Share/Delete of the
    uploads list it is meant to match. Caught by looking at prod, not by a test."""
    css = (SRC / "server/static/css/canvas.css").read_text()
    assert "a.btn {color:inherit;text-decoration:none}" in css, (
        "the a.btn normalisation is gone - anchor-buttons will render as links")


def main():
    print("UI static-serving self-test:")
    test_an_anchor_button_is_not_left_looking_like_a_link()
    # #401: "/" is the MARKETING site when one has been exported into site/out, and
    # falls back to the app's own landing when it has not (the self-host case). The
    # app shell itself is addressable at /app and the other SHELL_PATHS either way,
    # which is what the assertions below check.
    r = client.get("/")
    assert r.status_code == 200, r.status_code
    assert "text/html" in r.headers["content-type"], r.headers["content-type"]
    assert "DBSearch" in r.text, "/ should mention DBSearch"
    # Read the app's OWN resolved site dir, not a guess at the repo layout, so this
    # test follows DBSEARCH_SITE_DIR exactly as the server does.
    from dbsearch.server.app import _SITE_DIR
    site_built = (_SITE_DIR / "index.html").is_file()
    if site_built:
        # Assert STRUCTURE, not marketing copy. This used to require the headline "Search
        # everything", which the site has since stopped saying ("Talk to your databases...") -
        # so the test failed while the behaviour it guards was perfectly fine. A headline is the
        # single most likely thing in a repo to change; pinning it tests the copywriter, not the
        # server. What actually matters is WHICH document is served: the exported site, not the
        # app shell.
        assert 'data-testid="hero"' in r.text, "/ did not serve the exported marketing site"
        assert "_next/static" in r.text, "/ served HTML without the exported site's assets"
        assert "view-app" not in r.text, "/ served the app shell where the site was expected"
        print("  PASS  GET / -> marketing site (exported)")
    else:
        assert "view-app" in r.text, "/ did not fall back to the app shell"
        print("  PASS  GET / -> app shell (no exported site; self-host fallback)")

    # API routes still resolve (static mount didn't shadow them)
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/config").json()["edition"] == "self-host"
    print("  PASS  /health and /config still resolve")

    # --- doc-upload UI present (card #32) ---
    admin_js = (SRC / "server/static/js/surfaces/admin.js").read_text()
    api_js = (SRC / "server/static/js/api.js").read_text()
    assert "renderUpload" in admin_js, "admin upload panel missing"
    assert 'type: "file"' in admin_js or "type:'file'" in admin_js or 'type=\\"file\\"' in admin_js, \
        "file input missing from admin upload panel"
    assert "uploadDocument" in api_js, "api.js uploadDocument helper missing"
    assert "/admin/upload" in api_js, "api.js does not call /admin/upload"
    print("  PASS  doc-upload UI present")

    # --- Draft surface present (card #33, merged from main) ---
    assert client.get("/static/js/surfaces/draft.js").status_code == 200, "draft.js not served"
    assert "draftProposal" in client.get("/static/js/api.js").text, "api.draftProposal missing"
    # #413: the navigation is rendered by js/ui/rail.js (shared with the canvas),
    # so it is no longer literal markup in the shell. Assert the ONE definition
    # instead - that is now the thing that must not regress.
    rail_js = (SRC / "server/static/js/ui/rail.js").read_text()
    assert 'href: "/draft"' in rail_js, "Draft nav link missing from the shared rail"
    assert "mountDraft" in client.get("/static/js/router.js").text, "draft route missing"
    print("  PASS  Draft surface assets served + wired")
    # --- Developer surface present (card #29) ---
    assert client.get("/static/js/surfaces/developer.js").status_code == 200, "developer.js not served"
    dev_js = (SRC / "server/static/js/surfaces/developer.js").read_text()
    api_js2 = (SRC / "server/static/js/api.js").read_text()
    router_js = (SRC / "server/static/js/router.js").read_text()
    assert "mountDeveloper" in dev_js, "mountDeveloper missing"
    assert "createKey" in api_js2 and "/developer/keys" in api_js2, "api.js key helpers missing"
    assert "mountDeveloper" in router_js and "developer" in router_js, "developer route not wired"
    assert 'href: "/developer"' in rail_js, "Developer nav link missing from the shared rail"
    print("  PASS  Developer surface assets served + wired")

    # --- Chat is MERGED INTO ASK, not a second surface (#632) ---
    # This assertion used to be the exact inverse: chat.js served, mountChat wired, a Chat
    # item in the rail. Ask and Chat rendered the SAME backend (both POSTed /chat/stream with
    # a conv_id through one ConversationService), so a thread begun on Chat was durable and
    # findable only from Ask - and nothing in the product could say what the difference was.
    # The doorway stays open (308) because bookmarks and muscle memory exist; the second
    # surface does not.
    assert client.get("/static/js/surfaces/chat.js").status_code == 404, \
        "chat.js is still served - the merged surface left a second copy behind"
    assert "mountChat" not in router_js, "router still wires a Chat surface"
    assert 'href: "/chat"' not in rail_js, "the rail still offers Chat as a peer of Ask"
    r = client.get("/chat", follow_redirects=False)
    assert r.status_code == 308, f"GET /chat -> {r.status_code}, wanted a 308 to /ask"
    assert r.headers["location"] == "/ask", r.headers.get("location")
    print("  PASS  Chat is merged into Ask; GET /chat 308s and no second surface is served")
    print("\nUI STATIC SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
