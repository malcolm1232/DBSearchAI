"""#634: moving to Connectors must not blank the page - the black sidebar stays put.

THE DEFECT, in the owner's words: "when i change connector, the whole page blanked out, as if
it were hard refreshed... i want it to change with the (black) sidebar still intact."

When this card was written it WAS a real document load: /canvas was its own document, so
router.js declined to intercept it and the browser navigated for real. #634 fixed the paint
and deliberately left the load alone - and the owner came back, because a load that looks
tidy is still a load. #643 removed it: /canvas is a shell path now and Connectors is a
surface. The pre-paint stamp below still matters, for the FIRST load of any surface.
NEITHER DOCUMENT SHIPPED ANY RAIL MARKUP - the rail is built by
`renderRail()` from a dynamically imported module, dynamic on purpose so the URL can carry the
build id (#415). Timed on prod build cc91830790b1: the document finished loading at ~534ms and
rail.js only arrived at ~716ms. For ~180ms the page rendered with NO sidebar and content at
the far left, then a sidebar appeared and everything jumped right.

THE FIX IS NOT TO HARD-CODE THE RAIL INTO BOTH PAGES. #413 spent a session deleting exactly
that, and design-system rule 6 says there is one definition. What is duplicated is the rail's
GEOMETRY - a width and a background colour - so the column can be held open and painted while
the real rail is still downloading.

WHAT THIS FILE PINS, and each is a way the fix could rot:
  1. Both documents stamp `data-rail`/`data-theme` in a SYNCHRONOUS inline <head> script.
     Move it into a module and it runs after first paint, which is the whole bug.
  2. The script sits before the stylesheets, so the attributes exist when CSS first matches.
  3. Both hosts size the nav column from --rail-reserve, not `auto`. `auto` is zero until the
     rail lands, which is what made the content jump.
  4. The collapsed width is honoured, so a collapsed user does not get a 248px band that
     snaps to 60px.
  5. rail.js keeps the attribute in step when the user toggles.

    python3 tests/selftest_634_rail_survives_navigation.py
"""
import os
import re
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)

#: #643: these are the same document now - /canvas serves index.html like every other shell
#: surface. Both are still requested on purpose: it is the cheapest possible proof that the
#: merge really happened, and if /canvas is ever split back out the seam re-appears here
#: first rather than in a browser.
DOCS = {"shell": "/ask", "canvas": "/canvas"}


def _doc(path: str) -> str:
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    return r.text


def test_both_documents_stamp_the_rail_state_before_first_paint():
    for name, path in DOCS.items():
        html = _doc(path)
        head = html.split("</head>", 1)[0] if "</head>" in html else html[:6000]
        assert "data-rail" in head, (
            f"{name}: nothing stamps data-rail in the <head> - the column cannot be held "
            "open before the rail module lands")
        assert "dbsearch_rail_collapsed" in head, (
            f"{name}: the head script does not read the collapse key, so a collapsed user "
            "gets a band the wrong width")
        # Synchronous: a module or a deferred script runs AFTER first paint, which is the bug.
        block = re.search(r"<script(?![^>]*\bsrc=)[^>]*>", head)
        assert block, f"{name}: no inline script in the head at all"
        assert "type=\"module\"" not in block.group(0), (
            f"{name}: the pre-paint script is a module - modules are deferred by definition, "
            "so it would run after the page has already painted without a sidebar")
        assert "defer" not in block.group(0) and "async" not in block.group(0), (
            f"{name}: the pre-paint script is deferred/async and would run too late")
        print(f"  PASS  {name}: rail state is stamped synchronously before first paint")


def test_the_stamp_runs_before_the_stylesheets_it_feeds():
    """CSS keys --rail-reserve off `data-rail`. If the stylesheet is linked first the browser
    can match rules before the attribute exists, and the default width wins for a frame."""
    for name, path in DOCS.items():
        html = _doc(path)
        at_script = html.find("data-rail")
        # The LINK ELEMENT, not the bare string: prose above it mentions rail.css by name,
        # and matching that would compare the script against a comment. (This test failed
        # exactly that way on its first run.)
        m = re.search(r'<link[^>]+rail\.css', html)
        assert at_script != -1 and m, f"{name}: missing script or rail.css link"
        at_css = m.start()
        assert at_script < at_css, (
            f"{name}: rail.css is linked before the script that stamps data-rail")
        print(f"  PASS  {name}: the stamp precedes the stylesheet that reads it")


def test_the_host_reserves_the_column_instead_of_sizing_it_auto():
    """`auto` means zero until the rail exists, so the content painted full-width and then
    jumped right. Reserving keeps it where it will be.

    #643: one host, not two. The canvas had its own `.app` grid declaring its own nav column,
    and keeping the two in step was a standing cost - this test existed partly to pay it.
    There is a single .app-grid now, so there is nothing left to disagree with."""
    shell_css = client.get("/static/css/app.css").text
    grid = shell_css.split(".app-grid {", 1)[1].split("}", 1)[0]
    assert "var(--rail-reserve)" in grid, f"the shell grid still sizes the nav column: {grid}"
    assert "auto 1fr" not in grid, f"the shell grid is back to auto: {grid}"
    # And no surface may reintroduce a second grid that sizes the nav column its own way.
    canvas_css = client.get("/static/css/canvas.css").text
    assert "--rail-reserve" not in canvas_css, (
        "the Connectors surface declares its own nav column again; the reserve belongs to "
        "the ONE grid in app.css, and two copies is how #634's fix rotted before")
    print("  PASS  the nav column is reserved at a known width, in one place")


def test_the_reserve_honours_the_collapsed_width():
    css = client.get("/static/css/rail.css").text
    assert re.search(r":root\s*{\s*--rail-reserve:\s*248px", css), \
        "the expanded reserve is not 248px - it must match .navrail's own width"
    assert re.search(r':root\[data-rail="icons"\]\s*{\s*--rail-reserve:\s*60px', css), \
        "the collapsed reserve is not 60px - a collapsed user would watch the band snap"
    assert "--navrail-w: 248px" in css and "--navrail-w: 60px" in css, \
        "the rail's own widths moved; the reserve above must move with them"
    print("  PASS  the reserved width matches the rail's real width, collapsed and expanded")


def test_the_band_is_painted_by_css_not_by_javascript():
    css = client.get("/static/css/rail.css").text
    band = [ln for ln in css.splitlines() if "linear-gradient" in ln and "16161a" in ln]
    assert band, "no dark band is painted behind the nav column"
    assert "--rail-reserve" in "\n".join(band), \
        "the band's width is not tied to the reserved column, so the two can disagree"
    print("  PASS  the black column is painted by CSS, available on the first frame")


def test_the_toggle_keeps_the_reserve_in_step():
    """Otherwise collapsing the rail leaves the NEXT navigation holding a 248px band."""
    js = client.get("/static/js/ui/rail.js").text
    toggle = js.split("toggle.addEventListener", 1)[1][:600]
    assert 'setAttribute("data-rail"' in toggle, (
        "the collapse toggle does not update data-rail - the next page load would reserve "
        "the old width")
    assert "COLLAPSE_KEY" in toggle, "the toggle no longer persists the collapse state"
    print("  PASS  toggling the rail updates the reserve for the next navigation")


def test_connectors_is_a_tab_switch_and_not_a_document_load():
    """The inversion of what this test used to assert, and the reason #643 exists.

    It read: "/canvas must NOT be in SHELL_PATHS - the shell would intercept the click and
    mount nothing, because the canvas is a different document". True when written, and it
    passed while the owner was looking at the very defect he had reported. #634 was scoped to
    the paint and this test faithfully guarded that scope; what it guarded, in effect, was the
    reload. The canvas is a surface now, so the assertion is the opposite one.

    Both halves are required and neither is sufficient. A path in SHELL_PATHS with no route
    is intercepted and renders the fallback surface; a route with no path is never intercepted
    and the browser does a full load. Getting one without the other is a silent regression to
    exactly the behaviour reported."""
    js = client.get("/static/js/login.js").text
    paths = js.split("SHELL_PATHS = new Set(", 1)[1].split(")", 1)[0]
    assert "/canvas" in paths, (
        "/canvas left SHELL_PATHS, so a rail click on Connectors is a full document load "
        "again - the shell blanks, and the topbar changes under the user")
    router = client.get("/static/js/router.js").text
    assert re.search(r'"canvas":\s*mountCanvas', router), (
        "the router has no canvas route, so an intercepted click on Connectors would mount "
        "the fallback surface instead of the canvas")
    # The document itself must be gone, or the two can drift apart again in silence.
    assert client.get("/static/canvas.html").status_code == 404, (
        "canvas.html is still being served; a second front-end that nothing links to is a "
        "copy waiting to be edited by mistake")
    print("  PASS  Connectors is an in-document route, not a second front-end")


def test_the_shell_does_not_ship_every_page_invisible():
    """#638, and it is the reason #634 looked like it had not worked on the shell.

    Both views used to carry the `hidden` attribute, and `initShell` unhid one only after
    loadConfig()'s /config fetch and the whole module graph had resolved. So Connectors ->
    Admin painted a COMPLETELY WHITE document first - not a missing sidebar, nothing at all.
    #634's dark band could not help: the band is painted on .app-grid, and .app-grid IS
    #view-app, which was hidden.

    The path decides, before first paint, with no fetch and no server involvement."""
    for path in ("/ask", "/admin", "/draft", "/developer"):
        html = _doc(path)
        assert not re.search(r'id="view-app"[^>]*\bhidden', html), (
            f"{path} ships the app view hidden - the page is blank until JS unhides it")
        assert not re.search(r'id="view-landing"[^>]*\bhidden', html), (
            f"{path} ships the landing view hidden via an attribute; visibility must come "
            "from data-view so it can be decided before first paint")
        assert "data-view" in html.split("</head>", 1)[0], \
            f"{path}: nothing stamps data-view in the head"
    print("  PASS  no shell page ships with its views hidden")


def test_a_head_script_failure_still_shows_something_real():
    """The stamp is wrapped in try/catch (localStorage throws outright in some privacy modes).
    If it ever fails, the CSS default must still render a usable page rather than the blank
    document this card exists to remove."""
    css = client.get("/static/css/app.css").text
    assert ":root:not([data-view]) #view-app" in css, (
        "with no data-view attribute nothing is displayed - a head-script failure would "
        "reproduce the blank page exactly")
    print("  PASS  a missing data-view still paints the app, not a blank page")


def test_show_view_and_the_head_script_use_the_same_mechanism():
    """Two mechanisms for one fact is how they drift: the attribute would say `app` while a
    stale `hidden` said otherwise."""
    js = client.get("/static/js/login.js").text
    body = js.split("export function showView", 1)[1][:400]
    assert 'setAttribute("data-view"' in body, "showView no longer drives data-view"
    assert ".hidden =" not in body, (
        "showView is toggling `hidden` again alongside data-view - one of the two will win "
        "and it will not always be the same one")
    print("  PASS  showView and the pre-paint stamp share one mechanism")


if __name__ == "__main__":
    test_both_documents_stamp_the_rail_state_before_first_paint()
    test_the_stamp_runs_before_the_stylesheets_it_feeds()
    test_the_host_reserves_the_column_instead_of_sizing_it_auto()
    test_the_reserve_honours_the_collapsed_width()
    test_the_band_is_painted_by_css_not_by_javascript()
    test_the_toggle_keeps_the_reserve_in_step()
    test_connectors_is_a_tab_switch_and_not_a_document_load()
    test_the_shell_does_not_ship_every_page_invisible()
    test_a_head_script_failure_still_shows_something_real()
    test_show_view_and_the_head_script_use_the_same_mechanism()
    print("\nRAIL-SURVIVES-NAVIGATION SELF-TEST PASSED.")