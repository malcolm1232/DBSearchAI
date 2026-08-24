"""#631: the rail can carry a surface's content without the navigation moving.

The owner's conversations used to be stacked in the reading column - "Shared with you", the
form, two buttons, then "Your conversations", then the thread - so her filing cabinet
competed with the answer she was reading, and on a first visit the empty state collided with
the list (#625). They now live in the rail, which is where every product with a thread list
puts one.

THE RULE THIS HAD TO NOT BREAK is design-system rule 6: navigation never moves. It is the one
that was learned the hard way, twice in one session. So the slot is additive and its position
is asserted - between the nav items and the foot - and the NAV definition itself is checked
unchanged, ids, order and destinations.

Driven in a real DOM rather than grepped, because "the slot is inside the rail, after the
items, before the foot" is a fact about a rendered tree and a regex cannot see it.

    python3 tests/selftest_631_rail_slot.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import _domgate  # noqa: E402  the shared jsdom gate (#792)

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)

RAIL = ROOT / "src/dbsearch/server/static/js/ui/rail.js"
JSDOM = _domgate.JSDOM
PROBE = ROOT / "tests/rail_slot_dom_probe.mjs"
_dom = {}


def _report():
    """Mount the real rail in a real DOM. A crash is cached and re-raised, never collapsed
    into a skip - selftest_602's rule, because one broken surface silently turning five
    assertions green is worse than no test at all."""
    if "r" not in _dom:
        if not _domgate.gate("the rail DOM check"):
            _dom["r"] = None                           # permitted skip, already counted
        else:
            _dom["r"] = _domgate.run_node(
                ["node", str(PROBE), str(JSDOM), str(RAIL)], "the rail")
    return _domgate.resolve(_dom["r"])


def _skip():
    """The DOM half did not run. `_report` has already printed and counted why."""
    return True


def test_the_served_rail_is_the_one_the_probe_drives():
    served = client.get("/static/js/ui/rail.js").text
    assert served == RAIL.read_text(), \
        "the served rail.js differs from the file on disk - the DOM probe proves nothing"
    print("  PASS  the probe drives the module the browser receives")


def test_the_slot_exists_inside_the_rail():
    r = _report()
    if r is None:
        return _skip()
    assert r["slot_found"], "railSlot() finds nothing"
    assert r["slot_is_in_the_rail"], "the slot is not inside the rail element"
    assert r["filled"] and r["emptied"], (
        "a surface cannot fill the slot, or the router cannot empty it again")
    print("  PASS  one slot, fillable by a surface and clearable by the router")


def test_the_slot_sits_between_the_nav_and_the_foot():
    """Position IS the rule. Above the items it would push the navigation down the page as
    the thread list grew; below the foot it would be under the Collapse control."""
    r = _report()
    if r is None:
        return _skip()
    assert r["brand_index"] < r["last_item_index"] < r["slot_index"] < r["foot_index"], (
        f"slot is misplaced: {r['children']}")
    print("  PASS  the slot sits below the navigation and above the foot")


def test_the_navigation_itself_did_not_move():
    """Rule 6, asserted literally: same items, same order, same destinations. The slot is
    allowed to add a region; it is not allowed to disturb one."""
    r = _report()
    if r is None:
        return _skip()
    assert r["nav_ids"] == ["ask", "draft", "canvas", "admin", "developer"], r["nav_ids"]
    assert r["nav_hrefs"] == ["/ask", "/draft", "/canvas", "/admin", "/developer"], \
        r["nav_hrefs"]
    assert r["rendered_item_ids"] == r["nav_ids"], (
        "the rendered rail disagrees with the NAV definition")
    assert r["active_id"] == "ask", r["active_id"]
    print("  PASS  the navigation is unchanged - same items, order and destinations")


def test_the_router_clears_the_slot_between_surfaces():
    """Otherwise Ask's conversation list is still in the rail while Admin is on screen - a
    list of threads beside a page that cannot open one."""
    router = client.get("/static/js/router.js").text
    assert "navrail-slot" in router, "router.js never clears the slot on a route change"
    print("  PASS  the router empties the slot on every route change")


def test_ask_fills_the_slot_and_survives_its_absence():
    """The canvas mounts this rail too and has no slot to fill on some paths; a surface that
    assumed one would blow up the page rather than render without a list."""
    ask = client.get("/static/js/surfaces/ask.js").text
    assert 'querySelector(".navrail-slot")' in ask, "Ask never looks for the slot"
    assert "} else {" in ask.split('querySelector(".navrail-slot")', 1)[1][:1500], (
        "Ask has no fallback when the slot is absent - the owner would lose the door back "
        "to her own threads entirely (#602)")
    print("  PASS  Ask fills the slot, and still renders its lists without one")


def test_ask_does_not_statically_import_the_rail():
    """#415: main.js imports rail.js through a BUILD-VERSIONED dynamic URL. A static
    specifier anywhere in the shell's module graph reintroduces an unversioned /static/js/ui/
    rail.js, which is the exact cached-module hole that once deleted the whole navigation for
    every warm browser. Ask reads the slot off the DOM instead."""
    ask = client.get("/static/js/surfaces/ask.js").text
    assert 'from "../ui/rail.js"' not in ask, (
        "ask.js statically imports rail.js - that URL cannot carry the build id")
    print("  PASS  no unversioned static import of the rail")


def test_the_collapsed_strip_hides_the_list_not_the_nav():
    css = client.get("/static/css/rail.css").text
    assert ".navrail--icons .navrail-slot" in css, (
        "the 60px icon strip still renders thread titles it has no room for")
    assert ".navrail-slot" in css and "overflow-y: auto" in css, (
        "the slot does not scroll inside itself - a long list would push the foot away")
    print("  PASS  collapsed hides the list; the list scrolls inside its own region")


def test_the_rows_are_buttons_with_hairline_icons_not_emoji():
    """Design system rule 6: emoji render as flat glyphs on Windows and coloured blobs on
    macOS, so the same rail looked like a different product per OS. And a clickable div is
    unreachable by keyboard - these rows are the primary way back into a conversation."""
    ask = client.get("/static/js/surfaces/ask.js").text
    for emoji in ("👥", "💬"):
        assert emoji not in ask, f"an emoji row icon survived into the rail: {emoji}"
    assert 'el("button", {' in ask and "rail-thread" in ask, \
        "thread rows are not real buttons"
    print("  PASS  rows are buttons with hairline SVG icons")


def test_865_the_idempotence_check_and_the_rail_agree_on_the_class():
    """#865: main.js guarded rail mounting with `.rail`, and nothing has that class.

    The root is `navrail`; the only `rail`-prefixed classes are `rail-thread`,
    `rail-slot-head`, `rail-new`. A class selector matches whole tokens, so `.rail` matched
    nothing, the condition was always true, and the guard read as "mount at most once" while
    meaning nothing.

    Latent, not live: `boot()` runs once per document, so a second rail was never inserted.
    That is a fact about the CALLER, and it is the kind that changes quietly - a retry on a
    failed /config, a re-init after sign-in. Two rails would both match `.navrail-slot`, and
    `railSlot()` returns the FIRST, so Ask would write its conversation list into a rail that
    is not the one on screen.

    DRIVEN, NOT GREPPED. The probe runs main.js's actual predicate against a container that
    already holds a real rail, then runs the whole mount step again and counts. Asserting the
    string ".navrail" would pass for a rail that had since been renamed - which is the failure
    this card IS."""
    r = _report()
    if r is None:
        return _skip()
    assert r["rail_mounted_finds_a_real_rail"] is True, (
        f"railMounted() cannot see a rail it just built (root class "
        f"{r['rendered_root_class']!r}) - the predicate and renderRail have drifted")
    assert r["rail_mounted_on_empty"] is False, "railMounted() is true for an empty container"
    assert r["rails_after_one_mount"] == 1, r["rails_after_one_mount"]
    assert r["rails_after_two_mounts"] == 1, (
        f"mounting twice produced {r['rails_after_two_mounts']} rails - the idempotence "
        "guard is not guarding")
    # The CONTROL, and the reason this card existed: the old selector finds nothing. Stated as
    # a measurement so the defect is recorded rather than remembered.
    assert r["dead_selector_matches"] == 0, (
        "'.rail' now matches something, so the pre-#865 selector was not dead after all - "
        "re-read this card before trusting either")


def test_865_main_js_asks_the_rail_rather_than_matching_a_class():
    """The probe proves `railMounted` works; this proves main.js USES it.

    Split deliberately. An earlier draft of the probe re-implemented main.js's `if` inline,
    so it proved the rail's class was self-consistent and would have stayed green if main.js
    went back to `.rail` - it tested around the defect instead of at it. The behavioural half
    cannot see the call site, so the call site gets its own assertion.

    Phrased as "no class-matching here", not "contains railMounted": the failure being
    prevented is main.js holding its own opinion about how the rail is built."""
    src = (ROOT / "src/dbsearch/server/static/js/main.js").read_text()
    assert "railMounted(grid)" in src, (
        "main.js no longer asks rail.js whether a rail is mounted")
    # COMMENTS STRIPPED FIRST. The first draft of this asserted over the whole file and failed
    # on its own explanatory comment, which names `.navrail` to say why not to match on it.
    # A guard that cannot tell prose from code will be silenced rather than obeyed.
    code = "\n".join(line.split("//")[0] for line in src.splitlines())
    assert 'querySelector(".rail")' not in code, "the dead .rail selector is back"
    assert "navrail" not in code, (
        "main.js names the rail's class in code again - that is the pair that drifted; ask "
        "railMounted() instead")


if __name__ == "__main__":
    test_the_served_rail_is_the_one_the_probe_drives()
    test_the_slot_exists_inside_the_rail()
    test_the_slot_sits_between_the_nav_and_the_foot()
    test_the_navigation_itself_did_not_move()
    test_the_router_clears_the_slot_between_surfaces()
    test_865_the_idempotence_check_and_the_rail_agree_on_the_class()
    test_865_main_js_asks_the_rail_rather_than_matching_a_class()
    test_ask_fills_the_slot_and_survives_its_absence()
    test_ask_does_not_statically_import_the_rail()
    test_the_collapsed_strip_hides_the_list_not_the_nav()
    test_the_rows_are_buttons_with_hairline_icons_not_emoji()
    print("\nRAIL SLOT SELF-TEST PASSED.")
