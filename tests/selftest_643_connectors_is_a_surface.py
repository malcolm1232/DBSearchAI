"""#643 - Connectors is a view of the shell, and the two things that keep it safe there.

THE DEFECT, in the owner's words: "when i change from /ask to connector, the whole page
refreshes, as if it looks like its hard refresh. but when i change from /ask to /draft, it's
like a simple tab switch." He diagnosed it from the topbar: Connectors said "Data Canvas /
Your databases / Malcolm Tan signed in / Sign out", every other surface said "self-host /
No content leaves your cloud / Model / avatar". Two topbars means two documents.

Measured in Chrome on prod before any change. A page-lifetime marker set on /ask survived a
click on Draft with the navigation entry unchanged; the same marker was gone after a click on
Connectors, with a new navigation entry, 56KB transferred and a different document.title.

THE ROUTING half of the fix is asserted next door, in selftest_634 (SHELL_PATHS + a route)
and selftest_560 (every rail destination is renderable in-document). This file covers the two
properties the merge itself depends on, neither of which had a guard before:

  1. THE SCOPE. css/canvas.css and css/app.css share 17 class names - .panel, .topbar,
     .brand, .active, .sources, .spacer - from their years as two documents that never met.
     They meet on every page load now. One unscoped `.panel` here restyles Ask's citation
     panel, and it would be found in a browser rather than here.

  2. THE TEARDOWN. Every other surface's state is entirely in the DOM the router throws away.
     The canvas is the first that is not: it hangs an Escape handler, a resize handler and a
     capture-phase pointerdown handler off window/document, watches data-theme, and polls a
     SharePoint ingest. Left behind, its Escape handler reaches for #spPicker on every
     keypress in Ask.

    python3 tests/selftest_643_connectors_is_a_surface.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/dbsearch/server/static"

CANVAS_JS = (STATIC / "js/surfaces/canvas.js").read_text()
CANVAS_CSS = (STATIC / "css/canvas.css").read_text()
ROUTER = (STATIC / "js/router.js").read_text()

SCOPE = ".canvas-surface"

#: Selectors that are ABOUT the surface's placement in the shell rather than about its
#: contents, so they legitimately name the shell's own host element.
HOST_SELECTORS = (".surface--bleed",)


def _rules(css):
    """Every selector list in the stylesheet, with comments and at-rule preludes removed.

    Brace-matched rather than regexed line by line: the file is dense, several rules share a
    line, and a line-oriented parser reported the tail of one rule as the head of the next."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, buf, depth = [], "", 0
    for ch in css:
        if ch == "{":
            head = buf.strip()
            if depth == 0 and head and not head.startswith("@"):
                out.append(head)
            elif depth > 0 and head and not head.startswith("@"):
                out.append(head)         # inside @media / @supports
            buf = ""
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
            buf = ""
        else:
            buf += ch
    return out


def test_every_selector_in_the_canvas_stylesheet_is_scoped():
    """The one rule this file exists to keep. See note 1 above."""
    unscoped = []
    for rule in _rules(CANVAS_CSS):
        for part in rule.split(","):
            p = part.strip()
            if not p or p.startswith("@") or re.fullmatch(r"\d+%|from|to", p):
                continue          # keyframe stops carry no selector
            if SCOPE in p or any(h in p for h in HOST_SELECTORS):
                continue
            unscoped.append(p)
    assert not unscoped, (
        "css/canvas.css has selectors that escape the surface and will restyle the rest of "
        f"the shell: {unscoped[:8]}\n"
        "This file and app.css share 17 class names. Every rule must be under "
        f"`{SCOPE}` (or name the host explicitly, see HOST_SELECTORS).")
    print("  PASS  every canvas rule is scoped to the surface")


def test_the_canvas_does_not_redefine_the_shells_palette_globally():
    """The palette lives ON the surface, so it inherits into it and stops at its edge.

    canvas.html defined --bg, --border, --accent, --muted, --shadow, --serif and --mono on
    :root, and so does tokens.css, with different values. That was survivable while they were
    two documents. In one document a bare `:root {}` here silently repaints the rail, the
    topbar and every other surface."""
    for rule in _rules(CANVAS_CSS):
        for part in rule.split(","):
            p = part.strip()
            assert not (p == ":root" or p.startswith(":root ")), (
                f"css/canvas.css declares `{p}`, which reaches the whole document. Theme "
                "blocks must read `:root[data-theme=...] .canvas-surface`, so the values "
                "land on the surface and nowhere else.")
    print("  PASS  the canvas palette is scoped to the surface, not the document")


def test_the_surface_hands_back_a_teardown():
    """See note 2 above. mountCanvas must return the function that undoes it."""
    assert re.search(r"export function mountCanvas\(root\)", CANVAS_JS), \
        "mountCanvas is not exported as the surface's mount"
    assert re.search(r"return function unmountCanvas\(\)", CANVAS_JS), \
        "mountCanvas returns no teardown, so everything below is unreachable"
    body = CANVAS_JS[CANVAS_JS.index("return function unmountCanvas()"):]
    for needle, why in [
        ("themeWatch.disconnect()", "the data-theme observer keeps repainting a dead canvas"),
        ("for (const off of offs) off()", "the window/document listeners are never removed"),
        ("clearInterval", "a SharePoint ingest poll outlives the surface that started it"),
        ("surface--bleed", "the shell's reading column stays suppressed for the next surface"),
        ("host.remove()", "the canvas's DOM is left in the surface host"),
    ]:
        assert needle in body, f"the teardown does not {needle}: {why}"
    print("  PASS  the surface returns a teardown that gives back everything it took")


def test_no_listener_escapes_the_teardown():
    """Every window/document listener must go through `on(...)`, which records its removal.

    A bare `window.addEventListener` here is invisible to the teardown and is exactly how the
    Escape handler would survive onto Ask. Checked structurally because the alternative -
    noticing it in a browser - means noticing that Escape throws on a different surface."""
    stray = re.findall(r"\b(?:window|document)\.addEventListener\(\s*[\"'](\w+)", CANVAS_JS)
    assert not stray, (
        f"canvas.js registers {stray} directly on window/document, so the teardown cannot "
        "remove them. Use `on(window, ...)` / `on(document, ...)`, which records the removal.")
    print("  PASS  every global listener is tracked for removal")


def test_the_router_tears_down_before_it_mounts_the_next_surface():
    """And in that order: a teardown must still be able to find its own nodes."""
    assert "const teardown = mount(root)" in ROUTER, \
        "the router discards the teardown a mount returns"
    at_unmount = ROUTER.index("if (unmount) {")
    at_wipe = ROUTER.index('root.innerHTML = ""')
    assert at_unmount < at_wipe, (
        "the router wipes the DOM before calling the teardown, so the teardown runs against "
        "a tree that is already gone")
    print("  PASS  the router tears the old surface down before wiping it")


def test_a_late_callback_cannot_draw_into_an_unmounted_surface():
    """Removing listeners is not a complete teardown: a fetch already in flight still lands.

    FOUND ON PROD, after the teardown was already in. Leaving Connectors while the SharePoint
    sync was in flight threw `positionHub -> null.style` and `renderPanel -> null.innerHTML`
    from a continuation that resolved after the DOM was gone.

    The console noise is the harmless half. The canvas resolves its elements with
    document.getElementById, which after a quick return finds the NEW mount's nodes - so a
    stale callback would write the old catalog into the fresh surface, silently and with
    nothing thrown. Hence a flag rather than more null-guards: null-guards would have made the
    console clean and left that case intact."""
    assert re.search(r"\blet alive = true\b", CANVAS_JS), \
        "the surface has no liveness flag, so a late callback cannot tell it has been unmounted"
    td = CANVAS_JS[CANVAS_JS.index("return function unmountCanvas()"):]
    assert "alive = false" in td, "the teardown never marks the surface dead"
    assert td.index("alive = false") < td.index("host.remove()"), \
        "the flag is flipped after the DOM is torn down, which is the window the bug lived in"
    # THE ROOT. Every network continuation in this surface hangs off api() or one of the
    # handful of raw fetches, so abandoning there stops the whole cascade at its source.
    # Guarding the crash sites one at a time was chasing where the cascade surfaced: three
    # were reported from prod (positionHub, renderPanel, composeUp) and the chain is long.
    api = CANVAS_JS[CANVAS_JS.index("function api(path,opts){"):]
    assert "if(!alive) return ABANDONED;" in api[:120], (
        "api() does not abandon after unmount, so every .then downstream of an in-flight "
        "request still runs against a surface that is gone")
    assert "const ABANDONED = new Promise(() => {});" in CANVAS_JS, (
        "the abandoned chain must never SETTLE. Rejecting moves the problem into the .catch "
        "handlers on the chain, several of which recover by drawing something.")

    # ...and defence in depth on everything that reaches the DOM by another route.
    for fn in ("renderAll", "positionHub", "drawEdges", "renderPanel", "renderStatus",
               "toast", "composeUp"):
        body = CANVAS_JS[CANVAS_JS.index(f"function {fn}("):]
        head = body[:body.index("\n", body.index("{")) + 110]
        assert "if(!alive) return" in head, (
            f"{fn}() does not check `alive`, so a callback that resolves after unmount will "
            "draw into a surface that is gone - or worse, into the next one")
    print("  PASS  a callback that resolves after unmount draws nothing")


def test_only_a_function_is_treated_as_a_teardown():
    """A mount may be async, or return something that is not a teardown. Both are fine.

    THIS SHIPPED TO PROD AND BROKE NAVIGATION. The first cut read `unmount = mount(root) ||
    null` - a truthiness check. `mountAdmin` is `async`, so it returns a PROMISE: truthy, and
    not callable. The next route change called it, threw `unmount is not a function` BEFORE
    the wipe and the mount, and left the previous surface frozen on screen while the URL and
    the rail both moved. Leaving Admin appeared to break every other surface.

    Nothing caught it. Three of the four older surfaces return nothing, so `|| null` was
    accidentally right for them, and the local browser pass only walked Ask <-> Connectors -
    it never left Admin. Found by walking all five on prod.
    """
    assert "unmount = mount(root) || null" not in ROUTER, (
        "the router is back to the truthiness check that shipped and broke navigation: an "
        "async mount's Promise is truthy, and calling it throws before the next render")
    assert 'typeof teardown === "function" ? teardown : null' in ROUTER, (
        "the router does not type-check the value a mount returns. An async mount returns a "
        "Promise, which is truthy and not callable, and calling it kills the next render.")
    print("  PASS  only a callable is kept as a teardown")


def test_a_failing_teardown_cannot_freeze_the_app():
    """One bad unmount must not take the next surface with it.

    Same failure shape as above and worth its own guard: anything that throws between the
    click and the mount leaves the user looking at the surface they are trying to leave,
    with the rail and the URL insisting they already left it."""
    block = ROUTER[ROUTER.index("if (unmount)"):ROUTER.index('root.innerHTML = ""')]
    assert "try {" in block and "catch" in block, (
        "the teardown call is unguarded - a surface that throws on the way out freezes the "
        "app on itself, which is indistinguishable from the navigation being broken")
    print("  PASS  a teardown that throws is logged, and the render continues")


def test_the_canvas_no_longer_answers_the_identity_question():
    """#414's open subtask, closed as a consequence of the merge.

    Two controls answering "who am I and what am I connected to" on one screen is the exact
    confusion #630 set out to remove, and while they were two documents it was merely
    duplicated. In one document they would sit three inches apart."""
    assert 'class="wsub' not in CANVAS_JS and 'class="who"' not in CANVAS_JS, \
        "the canvas renders an identity chip again; ui/account.js owns that for every surface"
    assert "msSignin" not in CANVAS_JS, \
        "the canvas offers its own 'Sign in with Microsoft' again - #630 unified that"
    # ...but the one grant affordance account.js POINTS AT must still be here, or its
    # "Connect" link for Google lands on a page with nothing to click.
    assert "/auth/google/login" in CANVAS_JS, (
        "the Google grant affordance is gone from Connectors, and ui/account.js links "
        "'Connect' straight to /canvas expecting to find it")
    print("  PASS  identity belongs to the account control; the grant flow stays here")


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
