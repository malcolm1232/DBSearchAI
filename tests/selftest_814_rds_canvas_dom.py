"""#814 / ADR 0026, the canvas half - RDS stores are always-delegated aws_keys kinds.

The defect (wave-2 #780 audit; the owner's live 260813 dead end): a palette-added
rds_postgres node carried no delegation and its panel could not honestly collect the
password the base engine demanded - Test connection failed 'postgres config missing
[password]' with nothing to type it into.

Two clauses, driven through tests/canvas_delegation_dom_probe.mjs (the #809 rig):
 - palette_rds_delegates: both RDS kinds emit {kind: aws_keys, resource: rds} untoggled
   (the _AWS_KINDS/_ALWAYS_DELEGATED clause + the delegationFor resource map).
 - rds_panel_no_password: the panel stops collecting a password (the IAM token IS the
   password, minted server-side from the caller's vaulted keys) but keeps the db user
   field - the token is minted FOR that user. Control: azure_sql keeps its password.
The #809 controls (csv/azure_sql undelegated) keep guarding against an over-broad set.

    PYTHONPATH=src python3 tests/selftest_814_rds_canvas_dom.py
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
        if not _domgate.gate(f"the RDS delegation DOM check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the RDS canvas rules ({scenario})")
    return _domgate.resolve(_dom[scenario])


def test_palette_added_rds_kinds_carry_aws_keys_delegation():
    r = _report("palette_rds_delegates")
    if r is None:
        return
    want = {"kind": "aws_keys", "resource": "rds"}
    assert r["delegations"].get("rds_postgres") == want, (
        "a palette-added rds_postgres entry reached the row without its aws_keys "
        f"delegation (#814; got {r['delegations'].get('rds_postgres')})")
    assert r["delegations"].get("rds_mysql") == want, (
        f"rds_mysql missed the aws_keys rail ({r['delegations'].get('rds_mysql')})")


def test_rds_panel_collects_no_password_but_keeps_the_db_user():
    r = _report("rds_panel_no_password")
    if r is None:
        return
    assert r["rdsHasPassword"] is False, (
        "the rds_postgres panel still collects a password - the IAM token is the "
        "password now, minted server-side (ADR 0026); a typed one belongs only in a "
        "hand-written self-host manifest")
    assert r["rdsHasUser"] is True, (
        "the rds_postgres panel lost its db user field - the IAM token is minted FOR "
        "that user; without it no delegated connection can open")
    assert r["azureSqlHasPassword"] is True, (
        "azure_sql lost its password field - the #814 panel change over-reached past "
        "the RDS kinds")


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
