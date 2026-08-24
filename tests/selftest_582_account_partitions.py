"""#582 - recorded identity rows become the partitions an account can reach (ADR 0019 D2).

This is the half that turns the share-time refusal from a shape heuristic into a statement
of fact. It reuses `canonical_partition`, so share-time resolution and session-time
resolution cannot disagree (pinned by selftest_582_partition_rule.py).

The load-bearing case is `test_an_entra_identity_with_no_recorded_tid_is_unknowable`:
"we have no record" must never be read as "no tenant".

    PYTHONPATH=src python3 tests/selftest_582_account_partitions.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")
os.environ["AUTH_TENANT_ID"] = "tid-home"

from dbsearch.api.auth import ACCT_TENANT_PREFIX, account_partitions  # noqa: E402

CONST = "deployment-const"


def _id(idp, subject, tid="", email=""):
    return {"idp": idp, "subject": subject, "tid": tid, "email": email}


def test_a_home_entra_account_reaches_the_deployment_partition():
    got = account_partitions([_id("entra", "oid-1", "tid-home")], "oid-1", CONST)
    assert got == {CONST}, got


def test_a_foreign_entra_account_reaches_its_own_tenant():
    got = account_partitions([_id("entra", "oid-2", "tid-foreign")], "oid-2", CONST)
    assert got == {"tid-foreign"}, got


def test_a_google_account_reaches_its_private_account_partition():
    got = account_partitions([_id("google", "b@g.com", "", "b@g.com")], "b@g.com", CONST)
    assert got == {ACCT_TENANT_PREFIX + "b@g.com"}, got


def test_a_local_account_reaches_its_private_account_partition():
    got = account_partitions([_id("local", "c@x.com", "", "c@x.com")], "acct_c", CONST)
    assert got == {ACCT_TENANT_PREFIX + "acct_c"}, got


def test_an_entra_identity_with_no_recorded_tid_is_unknowable():
    """FAIL CLOSED. Returning a partition here would hand a foreign-tenant Entra user an
    `acct:` partition they do not actually have, which is the silent breakage #582 exists
    to close - now dressed as a helpful default."""
    assert account_partitions([_id("entra", "oid-old")], "oid-old", CONST) is None


def test_unknowable_wins_over_a_knowable_sibling_identity():
    """One unrecorded Entra identity poisons the whole answer on purpose: the account CAN
    sign in through it, so its partition is genuinely not known."""
    got = account_partitions(
        [_id("google", "d@g.com", "", "d@g.com"), _id("entra", "oid-9")], "oid-9", CONST)
    assert got is None, got


def test_a_linked_account_reaches_both_of_its_partitions():
    """ADR 0013 + 0018: which partition you land in depends on the route you signed in
    through, so this is a SET, not a scalar."""
    got = account_partitions(
        [_id("entra", "oid-3", "tid-home"), _id("google", "c@g.com", "", "c@g.com")],
        "oid-3", CONST)
    assert got == {CONST, ACCT_TENANT_PREFIX + "oid-3"}, got


def test_an_account_with_no_identities_reaches_nothing():
    """Empty, NOT unknowable: there is no identity that could sign in, so there is nothing
    to be uncertain about."""
    assert account_partitions([], "ghost", CONST) == set()


def test_a_non_entra_identity_without_a_tid_is_not_unknowable():
    """Only Entra identities are expected to carry a tid. A Google or local row with none
    is complete information, not a gap."""
    assert account_partitions([_id("google", "e@g.com")], "e@g.com", CONST) is not None


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
