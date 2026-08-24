"""#781: the failure reason /router/compose already returns must REACH the user.

The server was never the problem. POST /router/compose returns, per skipped store, a precise
reason - "build/probe failed: postgres config missing [password] ..." - and composeUp()
already writes it onto the node as `n.reason`. The render layer then threw it away: the
status dot's tooltip was the status WORD ("planned"), the node card showed nothing, and the
status bar counted sources and connected without naming a single failure or cause. Reproduced
live on prod (card #781): the owner added an RDS store, pressed Compose up, and watched the
node turn red with the one-word tooltip "planned" while the wire response carried the answer.
The inspector panel DID render the reason - but only after you open the failing node, and
nothing on screen said which node to open. Meanwhile his EXISTING sharepoint store had been
silently skipped for "no ACL" on every compose for days (sub-finding (c)).

THE RULE: a reason the client already holds is surfaced at every place the failure itself is
rendered - the dot's tooltip, the node card, and the status bar - or the disagreement between
"Test connection" (which renders verdicts properly) and "Compose up" (which said nothing)
comes back.

Three clauses, one guard EACH (the 260817 lesson - a fixture rescued by two clauses at once
proves neither):
  1. the dot's title carries the reason, not just the status word;
  2. the node card renders a visible .nreason element;
  3. the status bar names the failing stores.
Plus the #786 half: the reason is SERVER text landing in an attribute sink (title) and a
content sink (.nreason), so the hostile fixture carries `"` and `<img onerror>` in one string
and the probe reports every element/handler it managed to create. And the clean-compose
control: no reasons, no warning - a fix that stamps warnings on healthy canvases fails here.

    python3 tests/selftest_781_compose_reason.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402  the shared jsdom gate (#792)

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_compose_reason_dom_probe.mjs"

RDS_REASON_MARK = "postgres config missing [password]"
HOSTILE_TEXT_MARK = "nobody can see this store"

_dom = {}


def _report(scenario):
    """One probe run per scenario, cached - crash cached as an exception and re-raised per
    caller (the selftest_602 rule)."""
    if scenario not in _dom:
        if not _domgate.gate(f"the compose-reason DOM check ({scenario})"):
            _dom[scenario] = None                  # permitted skip, already counted
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the compose surface ({scenario})")
    return _domgate.resolve(_dom[scenario])


def _node(r, store_id):
    n = next((n for n in r["nodes"] if n["id"] == store_id), None)
    assert n is not None, f"store {store_id} never rendered as a canvas node"
    return n


def test_the_dot_tooltip_carries_the_reason():
    """Clause 1. The tooltip the owner actually hovered said the one word 'planned'."""
    r = _report("skipped")
    if r is None:
        return
    rds = _node(r, "rds_postgres-1")
    assert rds["dotTitle"] and rds["dotTitle"].startswith("planned"), (
        "the status word left the tooltip - it should stay, with the reason after it")
    assert RDS_REASON_MARK in rds["dotTitle"], (
        f"the status dot's tooltip is {rds['dotTitle']!r} - the status word alone. The "
        f"compose response's reason ({RDS_REASON_MARK!r}) never reached it, which is the "
        f"exact tooltip the owner hovered on prod")
    print("  PASS  the status dot's tooltip carries the compose reason")


def test_the_node_card_shows_the_reason_visibly():
    """Clause 2. A tooltip needs a hover; the card itself must say why it is red."""
    r = _report("skipped")
    if r is None:
        return
    for sid, mark in (("rds_postgres-1", RDS_REASON_MARK), ("sharepoint", HOSTILE_TEXT_MARK)):
        n = _node(r, sid)
        assert n["reasonText"], (
            f"the {sid} node card renders NO visible reason element (.nreason) - the red "
            f"node explains itself only if you already know to open its panel")
        assert mark in n["reasonText"], (
            f"the {sid} reason line says {n['reasonText']!r}, which does not carry the "
            f"compose response's actual reason ({mark!r})")
        assert n["reasonTitle"] and mark in n["reasonTitle"], (
            f"the {sid} reason line has no title with the full text - the visible line is "
            f"clamped, so the full reason must survive somewhere hover reaches")
    print("  PASS  both skipped node cards render the reason visibly")


def test_the_statusbar_names_the_failing_stores():
    """Clause 3. '5 sources · 3 connected' told the owner neither WHICH failed nor WHY."""
    r = _report("skipped")
    if r is None:
        return
    bar = r["statusbar"] or ""
    assert "not connected" in bar, (
        f"the status bar says {bar!r} - counts only, no failure segment at all")
    for sid in ("rds_postgres-1", "sharepoint"):
        assert sid in bar, (
            f"the status bar's failure segment does not name {sid} - the owner still has "
            f"to hunt for the red node")
    print("  PASS  the status bar names the failing stores")


def test_the_hostile_reason_is_text_not_markup():
    """#786's lesson, applied at write time: the reason is server text entering an attribute
    sink AND a content sink. The fixture carries the breakout character for each in one
    string; anything either sink executes is a finding."""
    r = _report("skipped")
    if r is None:
        return
    assert r["injected_imgs"] == 0, (
        f"the hostile reason created {r['injected_imgs']} <img> element(s) - the content "
        f"sink (.nreason) is not escaping")
    assert r["handler_attrs"] == [], (
        f"the hostile reason created event-handler attributes {r['handler_attrs']} - an "
        f"attribute sink (title=) is not escaping, so `\"` breaks out")
    sp = _node(r, "sharepoint")
    assert sp["reasonText"] and "<img src=x" in sp["reasonText"], (
        "the hostile payload is not visible AS TEXT - escaping must neutralize it, not "
        "swallow the reason the user was owed")
    print("  PASS  the hostile reason renders as text in both sinks")


def test_a_clean_compose_shows_no_warnings():
    """The control. Every store composes: no reason lines, no failure segment, tooltips are
    the plain status word. A fix that cannot pass this is stamping warnings on healthy
    canvases - the same class of wrong as the defect."""
    r = _report("clean")
    if r is None:
        return
    for n in r["nodes"]:
        assert n["dotTitle"] == "connected", (
            f"clean compose, but {n['id']}'s dot title is {n['dotTitle']!r}")
        assert n["reasonText"] is None, (
            f"clean compose, but {n['id']} renders a reason line: {n['reasonText']!r}")
    bar = r["statusbar"] or ""
    assert "not connected" not in bar, (
        f"clean compose, but the status bar carries a failure segment: {bar!r}")
    print("  PASS  a clean compose renders no warnings")


def test_the_clamp_has_no_bottom_padding_window():
    """STATIC, deliberately: jsdom has no layout, and this defect was PIXELS - seen on prod,
    not by any probe. `overflow:hidden` clips at the PADDING edge, so a line-clamped element
    with bottom padding paints the clipped third line's top pixels inside that padding - a
    sliver of 'principals who may query it' rendered under the ellipsis. The rule is
    margin-below, never bottom-padding, whenever -webkit-line-clamp is in the declaration.
    This can only assert the declaration, not the paint; the browser pass owns the pixels."""
    css = (ROOT / "src/dbsearch/server/static/css/canvas.css").read_text()
    import re
    m = re.search(r"\.canvas-surface \.node \.nreason \{([^}]*)\}", css)
    assert m, "the .nreason rule is gone - the visible reason line has no styling at all"
    decl = m.group(1)
    assert "-webkit-line-clamp" in decl, (
        "the .nreason rule lost its line clamp - a long reason now pushes the card open")
    pad = re.search(r"padding:\s*([^;]+);", decl)
    assert pad, "the .nreason rule lost its padding declaration entirely"
    parts = pad.group(1).split()
    assert len(parts) == 4 and parts[2] in ("0", "0px"), (
        f"the .nreason rule declares bottom padding ({pad.group(1)!r}) alongside "
        f"line-clamp - overflow:hidden clips at the padding edge, so the clipped third "
        f"line paints through that window (the prod sliver). Use bottom MARGIN instead")
    assert re.search(r"margin:\s*0 0 \d+px?", decl) or "margin-bottom" in decl, (
        "the .nreason rule has neither bottom padding nor bottom margin - the reason now "
        "sits flush against the connector button")
    print("  PASS  the clamp clips clean: margin below, no bottom-padding window")


def test_the_connected_node_is_untouched_in_the_mixed_run():
    """Second control, inside the failing scenario: the store that DID compose must not
    inherit a neighbour's reason."""
    r = _report("skipped")
    if r is None:
        return
    ok = _node(r, "azure_sql-1")
    assert ok["dotTitle"] == "connected", (
        f"azure_sql-1 composed, but its dot title is {ok['dotTitle']!r}")
    assert ok["reasonText"] is None, (
        f"azure_sql-1 composed, but it renders a reason line: {ok['reasonText']!r}")
    print("  PASS  the connected node carries no reason")


if __name__ == "__main__":
    test_the_dot_tooltip_carries_the_reason()
    test_the_node_card_shows_the_reason_visibly()
    test_the_statusbar_names_the_failing_stores()
    test_the_hostile_reason_is_text_not_markup()
    test_a_clean_compose_shows_no_warnings()
    test_the_clamp_has_no_bottom_padding_window()
    test_the_connected_node_is_untouched_in_the_mixed_run()
    print("\nCOMPOSE REASON SELF-TEST PASSED.")
