"""#582 - identities remember their verified tenant and email (ADR 0019 D2/D5).

Before this, ADR 0013's account store held `(idp, subject) -> account_id` rows and nothing
else. That is why `_refuse_cross_partition_share` had to GUESS from the shape of an
identifier, and why one case (a foreign-tenant Entra grantee) stayed silently broken: the
server had no record of any account's tenant, so the grantee's partition was uncomputable.

Recording it is storage only. The RULE that turns these rows into partitions lives in
api/auth.py, which is the layer that knows the deployment constant.

    PYTHONPATH=src python3 tests/selftest_582_identity_tenants.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.server.accounts import InMemoryAccountStore  # noqa: E402


def test_entra_identity_records_its_tid_and_email():
    s = InMemoryAccountStore()
    s.resolve("entra", "oid-1", preferred_account_id="oid-1",
              tid="tid-home", email="Ann@Corp.com")
    rows = s.identity_tenants("oid-1")
    assert len(rows) == 1, rows
    assert rows[0]["tid"] == "tid-home", rows
    assert rows[0]["idp"] == "entra", rows
    assert rows[0]["email"] == "ann@corp.com", "email must be normalised for lookup"


def test_a_google_identity_records_an_email_and_no_tid():
    s = InMemoryAccountStore()
    s.resolve("google", "bob@gmail.com", preferred_account_id="bob@gmail.com",
              email="bob@gmail.com")
    rows = s.identity_tenants("bob@gmail.com")
    assert rows[0]["tid"] == "", rows
    assert rows[0]["email"] == "bob@gmail.com", rows


def test_an_identity_recorded_before_582_reports_an_empty_tid():
    """The upgrade case: rows already in Postgres have no tid. They must read as ""
    (unrecorded) so the share-time rule can fail closed on them, never as a real tenant."""
    s = InMemoryAccountStore()
    s.resolve("entra", "oid-old", preferred_account_id="oid-old")
    assert s.identity_tenants("oid-old")[0]["tid"] == ""


def test_a_second_login_backfills_a_tid_recorded_late():
    """That upgrade case heals itself: the owner's next sign-in records the tid."""
    s = InMemoryAccountStore()
    s.resolve("entra", "oid-2", preferred_account_id="oid-2")
    s.resolve("entra", "oid-2", preferred_account_id="oid-2", tid="tid-home",
              email="d@corp.com")
    assert s.identity_tenants("oid-2")[0]["tid"] == "tid-home"


def test_a_later_login_without_a_tid_never_clears_a_recorded_one():
    """Backfill, never erase - otherwise one odd sign-in would make a known account
    unknowable again and silently start refusing valid shares."""
    s = InMemoryAccountStore()
    s.resolve("entra", "oid-3", preferred_account_id="oid-3", tid="tid-home")
    s.resolve("entra", "oid-3", preferred_account_id="oid-3")
    assert s.identity_tenants("oid-3")[0]["tid"] == "tid-home"


def test_email_resolves_to_its_account_case_insensitively():
    s = InMemoryAccountStore()
    s.resolve("local", "carol@x.com", preferred_account_id="acct_c", email="carol@x.com")
    assert s.account_for_email("CAROL@X.COM") == "acct_c"
    assert s.account_for_email("  carol@x.com  ") == "acct_c"
    assert s.account_for_email("nobody@x.com") is None
    assert s.account_for_email("") is None


def test_identity_tenants_is_empty_for_an_unknown_account():
    assert InMemoryAccountStore().identity_tenants("nobody") == []


def test_a_linked_account_reports_both_identities():
    """ADR 0013 lets one account carry several identities. Both must come back, because
    which one you sign in through decides which partition you land in."""
    s = InMemoryAccountStore()
    s.resolve("entra", "oid-4", preferred_account_id="oid-4", tid="tid-home")
    s.link("google", "e@g.com", "oid-4")
    rows = {r["idp"] for r in s.identity_tenants("oid-4")}
    assert rows == {"entra", "google"}, rows


def test_the_existing_resolve_contract_is_unchanged():
    """The new kwargs are additive: every pre-#582 caller keeps working untouched."""
    s = InMemoryAccountStore()
    assert s.resolve("entra", "oid-5", preferred_account_id="oid-5") == "oid-5"
    assert s.resolve("entra", "oid-5") == "oid-5", "second resolve must return the same account"
    assert s.get("oid-5")["account_id"] == "oid-5"
    acc = s.create_local_user("f@x.com", "salt", "hash")
    assert s.get_local_user("f@x.com")["account_id"] == acc


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
