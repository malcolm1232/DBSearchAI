#!/usr/bin/env python3
"""#975 / #976 / #978 - the Connectors canvas has to stay USABLE below 920px.

WHAT THIS GUARDS, and why it is a browser test rather than a string check.

The defect was one CSS line - `.canvas-surface .rail, .canvas-surface .panel {display:none}`
inside `@media (max-width:920px)`. The canvas is a three-column grid (rail | canvas | panel),
and collapsing the two side TRACKS on a phone is right; hiding their CONTENTS is what cost the
surface every connector it has. Below 920px a user could not add a source (the rail is the only
"Add a source" affordance, four provider rows opening fifteen services) and could not configure
or authorise one (the panel holds the connection fields AND "Connect with Microsoft"). What was
left was the Google pill in .cv-head, which is why the surface looked like it supported exactly
one provider.

None of that is visible to a selector-grep. `display:none` on a grid child, a flex row that
overflows its own container, and an item pushed past the viewport edge are all LAYOUT facts,
and the only thing that knows them is a browser that has done the layout. DESIGN_SYSTEM s10
asks for exactly this: 1440 and 390, no horizontal scroll, scrollWidth == clientWidth, tap
targets at least 24px.

And it asserts the PROPERTY, not the mechanism (s10 again). It does not check for the string
"sheet-rail" or for `position:fixed`; it checks that the provider rows are on the screen and
that the panel's fields can be reached. A future pass is free to replace the bottom sheet with
a drawer, a tab or a modal, and this test should still pass.

It SERVES ITSELF, from the tree it is sitting in, on an ephemeral port. That is not
convenience: scripts/mutate_guards.py runs a guard inside a `git archive` scratch tree with one
edit applied, so a guard that pointed at an already-running localhost would measure the
UNMUTATED working tree and report every mutation caught while proving nothing - the "caught for
the wrong reason" failure that script's own docstring warns about. Pass a base_url to aim it at
a deployed origin instead (that is how it is run against prod after a deploy).

Run:  python3 tests/selftest_975_connectors_on_mobile.py              # serves itself
      python3 tests/selftest_975_connectors_on_mobile.py https://dbsearch.ai
"""
import contextlib
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE = sys.argv[1] if len(sys.argv) > 1 else None


def _free_port():
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _origin():
    """Either the URL we were handed, or an app served out of THIS tree."""
    if BASE:
        yield BASE.rstrip("/")
        return
    os.environ.setdefault("SELFHOST_BACKEND", "memory")
    os.environ.setdefault("DBSEARCH_DEV_AUTH", "1")
    os.environ.setdefault("DBSEARCH_DEV_SEED", "1")
    from _e2eserver import serving                      # noqa: E402  #846, one way to serve
    from dbsearch.server.app import app                 # noqa: E402

    port = _free_port()
    with serving(app, "127.0.0.1", port):
        yield "http://127.0.0.1:%d" % port

# 920 is the breakpoint. 919/921 straddle it; the rest are real devices plus the desktop the
# design system names. A bug that only shows up ON the boundary is the usual kind.
MOBILE = [360, 390, 430, 768, 919]
DESKTOP = [921, 1024, 1280, 1440]

passed, failed = [], []


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail and not ok else ""))


def rects(page):
    """Geometry the assertions below are written against, measured after layout."""
    return page.evaluate(
        """() => {
      const r = e => { if(!e) return null; const b = e.getBoundingClientRect();
        return {x:b.x, y:b.y, w:b.width, h:b.height, right:b.right, bottom:b.bottom}; };
      const vis = e => { if(!e) return false; const s = getComputedStyle(e);
        return s.display !== 'none' && s.visibility !== 'hidden' && e.getBoundingClientRect().width > 0; };
      const onScreen = e => { if(!vis(e)) return false; const b = e.getBoundingClientRect();
        return b.right <= innerWidth + 0.5 && b.x >= -0.5 && b.bottom <= innerHeight + 0.5 && b.y >= -0.5; };
      const d = document.documentElement;
      const qrow = document.querySelector('.canvas-surface .qrow');
      const sb = document.getElementById('statusbar');
      return {
        w: innerWidth, h: innerHeight,
        noHScroll: d.scrollWidth === d.clientWidth,
        railRows: [...document.querySelectorAll('.canvas-surface .rail .prov')]
                    .map(e => ({t: e.innerText.trim(), onScreen: onScreen(e), h: r(e).h})),
        addSourceVisible: vis(document.getElementById('addSource')),
        addSourceOnScreen: onScreen(document.getElementById('addSource')),
        addSourceH: r(document.getElementById('addSource')) ? r(document.getElementById('addSource')).h : 0,
        dock: qrow ? [...qrow.children].map(c => ({id: c.id, onScreen: onScreen(c), h: r(c).h})) : [],
        statusItems: sb ? [...sb.children].filter(c => getComputedStyle(c).display !== 'none')
                            .map(c => ({t: c.innerText.trim().slice(0, 40), onScreen: onScreen(c)})) : [],
      };
    }"""
    )


def open_rail(page):
    """Reach the source catalogue however this build exposes it."""
    if page.evaluate("() => { const b=document.getElementById('addSource');"
                     "  return !!b && getComputedStyle(b).display!=='none'; }"):
        page.evaluate("() => document.getElementById('addSource').click()")
        page.wait_for_timeout(400)


def _run(page, URL):
        # ---- below the breakpoint: everything must still be REACHABLE ----
        for w in MOBILE:
            page.set_viewport_size({"width": w, "height": 844})
            page.goto(URL, wait_until="load")
            page.wait_for_timeout(900)
            print("\n@%dpx" % w)

            m = rects(page)
            check("%d: no horizontal scroll (s10)" % w, m["noHScroll"])

            # THE defect: the source catalogue has to be reachable at all.
            check("%d: a door to the source catalogue exists" % w, m["addSourceVisible"] and m["addSourceOnScreen"])
            check("%d: that door is a >=24px tap target (s10)" % w, m["addSourceH"] >= 24,
                  "height %.1f" % m["addSourceH"])

            open_rail(page)
            m2 = rects(page)
            rows = m2["railRows"]
            check("%d: every provider row is on the screen" % w,
                  len(rows) >= 4 and all(r_["onScreen"] for r_ in rows),
                  "%d rows, offscreen: %s" % (len(rows), [r_["t"][:14] for r_ in rows if not r_["onScreen"]]))
            check("%d: provider rows are >=24px tap targets (s10)" % w,
                  bool(rows) and min(r_["h"] for r_ in rows) >= 24,
                  "min %.1f" % (min((r_["h"] for r_ in rows), default=0)))

            # A source can actually be added, and its config panel is then reachable. This is
            # the half that "only Google is there" was really about: the panel carries the
            # per-node connection fields and the Microsoft grant.
            added = page.evaluate(
                """async () => {
                  const rows=[...document.querySelectorAll('.canvas-surface .rail .prov')];
                  if(!rows.length) return {ok:false, why:'no provider rows'};
                  rows[0].dispatchEvent(new MouseEvent('click',{bubbles:true}));
                  await new Promise(r=>setTimeout(r,350));
                  const svc=[...document.querySelectorAll('#provmenu .svc')];
                  if(!svc.length) return {ok:false, why:'no services in the flyout'};
                  const onScreen = svc.every(e=>{const b=e.getBoundingClientRect();
                    return b.right<=innerWidth+0.5 && b.x>=-0.5;});
                  const before=document.querySelectorAll('.canvas-surface .node').length;
                  svc[0].click();
                  await new Promise(r=>setTimeout(r,700));
                  const panel=document.querySelector('.canvas-surface .panel');
                  const ps=getComputedStyle(panel);
                  const pb=panel.getBoundingClientRect();
                  return {ok:true, servicesOnScreen:onScreen,
                          nodeAdded: document.querySelectorAll('.canvas-surface .node').length>before,
                          panelShown: ps.display!=='none' && ps.visibility!=='hidden' && pb.height>0,
                          panelOnScreen: pb.top < innerHeight && pb.bottom > 0 && pb.width>0,
                          panelFields: panel.querySelectorAll('input,select,textarea,button').length};
                }"""
            )
            check("%d: the service flyout fits on the screen" % w,
                  added.get("ok") and added.get("servicesOnScreen"), str(added))
            check("%d: picking a service adds a node" % w, bool(added.get("nodeAdded")), str(added))
            check("%d: the node's config panel is reachable" % w,
                  bool(added.get("panelShown") and added.get("panelOnScreen")), str(added))
            check("%d: that panel carries its connection controls" % w,
                  added.get("panelFields", 0) >= 3, "%s controls" % added.get("panelFields"))

            # #976 + #978: nothing in the dock or the status bar may sit off the edge.
            m3 = rects(page)
            check("%d: every query-dock control is on the screen (#976)" % w,
                  bool(m3["dock"]) and all(c["onScreen"] for c in m3["dock"]),
                  "offscreen: %s" % [c["id"] for c in m3["dock"] if not c["onScreen"]])
            check("%d: dock controls are >=24px tap targets (s10)" % w,
                  bool(m3["dock"]) and min(c["h"] for c in m3["dock"]) >= 24,
                  "min %.1f" % (min((c["h"] for c in m3["dock"]), default=0)))
            check("%d: no status-bar item is off the screen (#978)" % w,
                  all(s["onScreen"] for s in m3["statusItems"]),
                  "offscreen: %s" % [s["t"] for s in m3["statusItems"] if not s["onScreen"]])

        # ---- above the breakpoint: the three-column canvas is untouched ----
        for w in DESKTOP:
            page.set_viewport_size({"width": w, "height": 900})
            page.goto(URL, wait_until="load")
            page.wait_for_timeout(900)
            print("\n@%dpx" % w)

            desk = page.evaluate(
                """() => {
                  const rail=document.querySelector('.canvas-surface .rail');
                  const main=document.querySelector('.canvas-surface .main');
                  const b=document.getElementById('addSource');
                  const d=document.documentElement;
                  const rb=rail.getBoundingClientRect();
                  return {cols:getComputedStyle(main).gridTemplateColumns,
                          railInFlow:getComputedStyle(rail).position==='static',
                          railVisible: getComputedStyle(rail).display!=='none' && rb.width>0,
                          railInline: rb.top>=0 && rb.bottom<=innerHeight+200,
                          doorHidden: !b || getComputedStyle(b).display==='none',
                          noHScroll: d.scrollWidth===d.clientWidth};
                }"""
            )
            check("%d: the source rail is still an in-flow column" % w,
                  desk["railInFlow"] and desk["railVisible"], str(desk))
            check("%d: the canvas keeps its multi-column grid" % w,
                  len(desk["cols"].split()) >= 2, desk["cols"])
            # The sheet door is a mobile-only affordance; a second way in beside a visible rail
            # is the duplicate affordance DESIGN_SYSTEM s11.2 warns about. It also caught a real
            # bug: `.canvas-surface .btn` is declared later with equal specificity and won.
            check("%d: the mobile-only 'Add a source' door is hidden" % w, desk["doorHidden"])
            check("%d: no horizontal scroll (s10)" % w, desk["noHScroll"])


def main():
    from playwright.sync_api import sync_playwright

    with _origin() as origin:
        url = origin + "/canvas"
        print("#975 Connectors-on-mobile self-test  ->  " + url)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            try:
                _run(page, url)
            finally:
                browser.close()

    print("\n%d passed, %d failed" % (len(passed), len(failed)))
    if failed:
        print("FAILED:")
        for f in failed:
            print("  - " + f)
        return 1
    print("\n#975 CONNECTORS-ON-MOBILE SELF-TEST PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
