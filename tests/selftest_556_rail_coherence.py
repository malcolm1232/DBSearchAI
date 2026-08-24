"""#556 — the navigation must not redefine itself per page.

The rail is already ONE shared component (#413). The bug was a single argument: the canvas
passed `collapsed: true`, so walking Chat -> Connectors swapped a 248px labelled sidebar for
a 64px icon strip the user never asked for. It also dropped the Workspace / Operate group
headers and the permission note, on the page a visitor reaches first from marketing.

This pins the property rather than the pixels: every surface builds the rail from the same
module, and NO surface passes a collapse default. The user's own choice still wins and
persists — that is the supported way to get an icon strip, and the reason a per-page default
is never needed.

    PYTHONPATH=src python3 tests/selftest_556_rail_coherence.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/dbsearch/server/static"
#: #643: main.js is now the ONLY renderRail() call site. The canvas used to be the second,
#: and the divergence this test was written for lived in the difference between the two.
#: The list is kept as a list rather than collapsed to one path, so that a surface which
#: grows its own rail call in future is checked by the same rule instead of going unseen.
SURFACES = [STATIC / "js/main.js"]


def _render_calls(text):
    """Every renderRail(...) call site, with its argument text."""
    return re.findall(r"renderRail\(\s*(\{[^}]*\}|)\s*\)", text)


def test_every_surface_uses_the_shared_rail():
    for f in SURFACES:
        src = f.read_text()
        assert "ui/rail.js" in src, f"{f.name} does not import the shared rail"
        assert _render_calls(src), f"{f.name} imports the rail but never renders it"


def test_no_surface_forces_a_collapse_default():
    """The actual regression guard. A per-page default is the divergence itself."""
    for f in SURFACES:
        for args in _render_calls(f.read_text()):
            assert "collapsed" not in args, (
                f"{f.name} passes a collapse default: renderRail({args}). "
                "The Collapse control persists the user's own choice — use that instead.")


def test_the_rail_still_offers_collapsing_and_remembers_it():
    """Coherence must not cost the affordance: the user can still collapse, and it sticks —
    otherwise this 'fix' just takes the canvas's space away with no recourse."""
    src = (STATIC / "js/ui/rail.js").read_text()
    assert "navrail-toggle" in src, "the collapse control is gone"
    assert "localStorage.setItem(COLLAPSE_KEY" in src, "the collapse choice is not persisted"
    assert "stored === null ? collapsed : stored === \"1\"" in src, \
        "a stored user choice must win over any caller-supplied default"


def test_the_expanded_rail_carries_what_the_icon_strip_dropped():
    """The three things collapsing removed, and the reason expanded is the better default."""
    src = (STATIC / "js/ui/rail.js").read_text()
    assert '{ group: "Workspace" }' in src and '{ group: "Operate" }' in src, \
        "the group headers are gone — that split is the product's own mental model"
    assert "Permission-faithful by construction" in src, "the permission note is gone"
    assert "navrail-brand" in src, "the wordmark is gone"


def test_the_canvas_layout_can_seat_the_wider_rail():
    """The nav column must always be exactly as wide as the rail in it, expanded or collapsed.

    It used to get that by being `auto` - content-sized, so it grew with the rail. #634 had to
    take that away: `auto` is ZERO until rail.js lands (the rail is built by a dynamically
    imported module), so the canvas painted with no sidebar and content hard left, then jumped
    right when the rail arrived. The column is now RESERVED from --rail-reserve, which is
    stamped before first paint and tracks the collapse toggle.

    The requirement is unchanged and is what this asserts: whatever sizes the column must
    agree with the rail's own two widths. Asserting `auto 1fr` again would be pinning the old
    mechanism rather than the property it existed to give.

    #643: there is ONE grid now. The canvas had its own `.app` grid declaring its own nav
    column, which is why this used to read canvas.html; it is a surface inside the shell's
    .app-grid today, so the column it sits in is app.css's and that is what is checked."""
    src = (STATIC / "css/app.css").read_text()
    # minmax(0, 1fr) on the content track, not a bare 1fr: #639 measured this grid computing
    # 248px + 632px inside a 753px page, because a bare `1fr` carries a min-content floor it
    # cannot shrink below. The RAIL column is what this test is about and it is still reserved
    # at the rail's own width.
    assert re.search(r"grid-template-columns:\s*var\(--rail-reserve\)\s*minmax\(0,\s*1fr\)", src), \
        "the nav column is neither reserved nor auto-sized - a 248px rail would overflow"
    rail_css = (STATIC / "css/rail.css").read_text()
    for width in ("248px", "60px"):
        assert f"--rail-reserve: {width}" in rail_css, \
            f"the reserve has no {width} state, so it cannot match the rail at that width"
        assert f"--navrail-w: {width}" in rail_css, \
            f"the rail no longer has a {width} state; the reserve above must move with it"


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
