"""#809 - a palette-added Redshift store carries its aws_keys delegation, untoggled.

The defect (found live in wave-2's #780 connect audit): _ALWAYS_DELEGATED held s3 only,
so a Redshift node added from the palette by a signed-in prod user emitted a manifest
entry with NO delegation block - and prod has no ambient AWS identity, so compose landed
in "build/probe failed: Unable to locate credentials" every time. ADR 0024: AWS kinds
delegate through the caller's own vaulted keys; there is no server identity that could
stand in (#673 established this for s3). Very plausibly the original #727 trigger.

Three clauses, a scenario each - any clause alone going missing turns its own scenario red:
 - palette_redshift_delegates: entryOf's _ALWAYS_DELEGATED now holds redshift, so the
   PUT row carries {kind: aws_keys, resource: redshift} with require_signin untouched.
 - yaml_preview: manifest() (the drawer preview) applies the SAME rule - before #809 its
   "Same rule as entryOf" comment was false even for s3, previewing `delegation: null`
   for a store that composes with one.
 - panel_switch: the redshift panel drops the require_signin switch - an always-delegated
   kind has no identity choice, and a switch that does nothing is the hollow-offer shape
   (#654/#656/#660; s3 precedent).
Controls (green before AND after - they fail an over-broad fix): azure_sql and the
pre-existing csv entry stay undelegated in the PUT row and the preview; azure_sql keeps
its require_signin switch.

    PYTHONPATH=src python3 tests/selftest_809_canvas_delegation_dom.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402  the shared jsdom gate (#792)

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_delegation_dom_probe.mjs"

_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the canvas delegation DOM check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the canvas delegation rules ({scenario})")
    return _domgate.resolve(_dom[scenario])


def test_palette_added_redshift_carries_aws_keys_delegation():
    r = _report("palette_redshift_delegates")
    if r is None:
        return
    assert r["putCount"] >= 1, "Cmd+S flushed no PUT - the fixture never saved the row"
    assert r["delegations"].get("redshift") == {"kind": "aws_keys", "resource": "redshift"}, (
        "a palette-added redshift entry reached the row WITHOUT its aws_keys delegation - "
        "on prod (no ambient AWS) this store can only ever compose to 'Unable to locate "
        f"credentials' (#809; got {r['delegations'].get('redshift')})")


def test_non_aws_kinds_stay_undelegated():
    r = _report("palette_redshift_delegates")
    if r is None:
        return
    assert r["delegations"].get("azure_sql") is None, (
        "an azure_sql entry with require_signin=no gained a delegation block - the #809 "
        f"fix over-reached past the AWS kinds ({r['delegations'].get('azure_sql')})")
    assert r["delegations"].get("csv") is None, (
        f"the pre-existing csv entry gained a delegation block ({r['delegations'].get('csv')})")


def test_yaml_preview_shows_the_delegation_it_will_compose():
    r = _report("yaml_preview")
    if r is None:
        return
    yaml = r["yaml"]
    assert "delegation: { kind: aws_keys, resource: redshift }" in yaml, (
        "the manifest drawer previews a palette-added redshift WITHOUT its delegation "
        "line - the YAML the user reads is not the YAML that composes (#809)")
    assert "delegation: { kind: aws_keys, resource: s3 }" in yaml, (
        "the manifest drawer previews a palette-added s3 store without its delegation "
        "line - manifest() still applies the signin-only rule entryOf left behind in #673")
    assert yaml.count("delegation:") == 2, (
        "the preview shows a delegation line for a store that composes without one "
        f"(expected exactly redshift + s3; preview:\n{yaml})")


def test_redshift_panel_offers_no_signin_switch():
    r = _report("panel_switch")
    if r is None:
        return
    assert r["redshiftHasSwitch"] is False, (
        "the redshift panel still offers require_signin - the switch does nothing for an "
        "always-delegated kind, which is the hollow-offer shape (#654/#656/#660)")
    assert r["azureSqlHasSwitch"] is True, (
        "the azure_sql panel lost its require_signin switch - the #809 panel change "
        "over-reached past redshift")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
