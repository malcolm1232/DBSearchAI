"""#560 - the rail must navigate with ONE scheme, and it has to be the path.

#309 gave every shell surface a real URL (/ask, /chat, /draft, /admin, /developer) and made
the client router fall back to the path only when there is no hash. The shared rail (#413)
was never converted: its Workspace items stayed fragments (#/ask ...) while Connectors alone
linked to a real path (/canvas). Two schemes on one navigation, which broke in two ways:

  1. On /canvas - a SEPARATE document that mounts the rail but runs no router - every
     Workspace link was dead. Clicking Draft moved the URL to /canvas#/draft and changed
     nothing on screen. The canvas was a one-way door again, the exact thing #309 fixed.
  2. On the shell the path never moved, so it accumulated a contradicting fragment:
     landing on /ask and clicking Chat gave /ask#/chat. The path said one surface, the
     hash said another, and the hash won.

The property pinned here is "one scheme": every rail destination is a real path, the shell
turns a same-document one into pushState, and a legacy #/x URL normalises to /x instead of
being a second source of truth. Fragments must not come back as navigation - on the canvas
they are not a worse URL, they are no navigation at all.

    PYTHONPATH=src python3 tests/selftest_560_one_navigation_scheme.py
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("SELFHOST_BACKEND", "memory")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
STATIC = ROOT / "src/dbsearch/server/static"

RAIL = (STATIC / "js/ui/rail.js").read_text()
ROUTER = (STATIC / "js/router.js").read_text()


def _nav_hrefs():
    """Every href: "..." in the NAV table."""
    nav = RAIL.split("export const NAV", 1)[1].split("\n];", 1)[0]
    return re.findall(r'href:\s*"([^"]+)"', nav)


def test_no_rail_destination_is_a_fragment():
    """The regression itself. A fragment is DEAD on /canvas, which runs no router."""
    hrefs = _nav_hrefs()
    assert hrefs, "the NAV table has no destinations at all"
    bad = [h for h in hrefs if not h.startswith("/")]
    assert not bad, (
        f"rail items still link to fragments: {bad}. On /canvas nothing listens for a "
        "hashchange, so these change the URL and nothing else.")


def test_every_rail_destination_is_a_url_the_server_serves():
    """A path link is a real navigation, so a path with no route is a 404, not a no-op."""
    from dbsearch.server.app import SHELL_PATHS  # noqa: E402

    servable = set(SHELL_PATHS) | {"/canvas", "/"}
    for h in _nav_hrefs():
        assert h in servable, (
            f"the rail links to {h}, which no route serves. Add it to SHELL_PATHS in "
            "app.py (and to login.js) or point the item somewhere real.")


def test_the_workspace_surfaces_are_all_reachable():
    # #632: /chat is no longer among them. It was never a distinct surface - the same
    # backend behind a second skin - so the rail now offers Ask once instead of twice.
    # Its doorway is still open (GET /chat 308s to /ask, pinned in selftest_ui_static);
    # what is gone is the second NAV ITEM, which is what this list is about.
    for want in ("/ask", "/draft", "/canvas", "/admin", "/developer"):
        assert want in _nav_hrefs(), f"the rail lost its {want} destination"
    assert "/chat" not in _nav_hrefs(), (
        "the rail offers Chat as a peer of Ask again - the merge (#632) put one door on "
        "one conversational surface, and two would be two names for the same thing")


def test_the_shell_router_reads_the_path_first():
    """The hash may only survive as a legacy alias. If it still WINS, /ask#/chat is back."""
    assert "location.pathname" in ROUTER, "router.js no longer resolves a path"
    body = ROUTER.split("function currentRoute", 1)[1].split("\n}", 1)[0]
    path_at = body.find("location.pathname")
    hash_at = body.find("location.hash")
    assert path_at != -1, "currentRoute() stopped reading the path"
    assert hash_at == -1 or path_at < hash_at, (
        "currentRoute() consults the hash before the path - that is the rule that made "
        "the path a lie on /ask#/chat")


def test_the_shell_navigates_without_reloading_itself():
    """Path links must stay instant WITHIN the shell, or every rail click is a full boot."""
    assert "pushState" in ROUTER, "router.js never pushes a path - rail clicks would reload"
    assert "popstate" in ROUTER, "router.js ignores Back/Forward after a pushState"


def test_a_legacy_hash_url_normalises_instead_of_being_obeyed():
    """Old links and bookmarks (#/chat) must still land somewhere real, and heal the URL.

    #632 changed WHERE, not whether: #/chat now resolves to /ask, because Chat merged into
    it. A bookmark that predates both changes still works, in one hop rather than two."""
    assert "replaceState" in ROUTER, (
        "router.js does not rewrite a legacy #/x URL. Without it the fragment lives on as "
        "a second source of truth for which surface is showing.")
    assert re.search(r"#/|LEGACY|legacy", ROUTER), "no legacy-hash handling is left at all"


def test_every_rail_destination_is_rendered_in_document():
    """The replacement for `test_the_canvas_is_not_expected_to_route`, which this test's own
    docstring asked to be retired deliberately if the canvas ever grew a router.

    It did. #643 folded canvas.html into the shell, so the assumption that one rail item
    pointed at a document the router could not render is gone - and with it the last reason
    for a rail click to be a full page load. That WAS the defect the owner reported: Ask to
    Draft was a pushState, Ask to Connectors was 56KB and a new document, and the two felt
    completely different because they were.

    So the property is now the stronger one #560 was reaching for. Every destination in the
    NAV table must be a path the shell claims (SHELL_PATHS) AND has a route for, because
    router.js intercepts a click only when BOTH hold - miss either and the browser navigates
    for real, silently, with nothing on screen to say why."""
    shell_paths = set(re.findall(r'"(/[a-z]+)"',
                                 (STATIC / "js/login.js").read_text()
                                 .split("export const SHELL_PATHS", 1)[1].split("]", 1)[0]))
    routes = set(re.findall(r'^\s*"([a-z]+)":\s*mount', ROUTER, re.M))
    assert shell_paths and routes, "SHELL_PATHS or the ROUTES table could not be read"
    for href in _nav_hrefs():
        assert href in shell_paths, (
            f"the rail links {href}, which the shell does not claim - router.js will not "
            "intercept the click and it becomes a full document load")
        assert href.lstrip("/") in routes, (
            f"the shell claims {href} but has no route to mount for it, so the click is "
            "intercepted and renders the fallback surface instead")


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
