"""#582 - ONE partition rule, shared by session-time and share-time resolution (ADR 0019 D2).

The rule that turns (tid, oid) into a partition used to live only inside `resolve_tenant`.
Share-time refusal needs the SAME rule, and two copies that drift would open a grant's
doorway onto a partition the grantee never reads - which is the #582 bug wearing a
different hat. `test_resolve_tenant_and_canonical_partition_agree` is the load-bearing one.

    PYTHONPATH=src python3 tests/selftest_582_partition_rule.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from dbsearch.api.auth import ACCT_TENANT_PREFIX, canonical_partition, resolve_tenant  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402

_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET")
CONST = "deployment-const"


def _real_login(on: bool):
    for k in _VARS:
        os.environ.pop(k, None)
    if on:
        os.environ.update({"AUTH_TENANT_ID": "tid-home", "AUTH_CLIENT_ID": "cid",
                           "AUTH_CLIENT_SECRET": "sec"})


def _cookie_getter(payload: dict):
    tok = user_auth.sign_session({**payload, "exp": int(time.time()) + 3600})
    return lambda name: tok if name == user_auth.COOKIE else None


def _no_header(name):
    return None


def test_canonical_partition_covers_every_branch():
    _real_login(True)
    assert canonical_partition("tid-home", "oid-1", CONST) == CONST
    assert canonical_partition("tid-foreign", "oid-2", CONST) == "tid-foreign"
    assert canonical_partition("", "acct_x1", CONST) == ACCT_TENANT_PREFIX + "acct_x1"
    assert canonical_partition("", "demo:someone", CONST) == ""
    assert canonical_partition("", "", CONST) == ""


def test_resolve_tenant_and_canonical_partition_agree():
    """If these two ever diverge, a grant opens a doorway onto a partition the grantee
    never reads. This test is the reason the extraction exists."""
    _real_login(True)
    for tid, oid in [("tid-home", "oid-1"), ("tid-foreign", "oid-2"), ("", "acct_x1"),
                     ("", "demo:someone"), ("", "")]:
        via_session = resolve_tenant(_no_header, _cookie_getter({"oid": oid, "tid": tid}),
                                     default_tenant=CONST)
        via_rule = canonical_partition(tid, oid, CONST)
        assert via_session == via_rule, (
            f"disagree for tid={tid!r} oid={oid!r}: {via_session!r} vs {via_rule!r}")


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
    _real_login(False)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
