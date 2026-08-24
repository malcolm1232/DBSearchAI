"""#775 - which tier is this account actually on, and what may it therefore store?

Stripe is the source of truth for what somebody is paying for. We cache the answer locally,
because the alternative is a network call to Stripe on the upload path: slow, and it makes
Stripe's availability a precondition for using a product they have already paid for.

The decisions this file pins down are the ones that are easy to get wrong in the direction
that hurts a paying customer:

  * an account nobody has told us about is on the FREE tier, not "no tier" and not "unknown"
  * a subscription stays effective until its period actually ends, because Stripe's
    cancel-at-period-end is the setting we chose in the portal: the customer paid for the
    month and keeps the month
  * a cache naming a tier the ladder no longer has does NOT downgrade the customer. Quota is
    a billing concern, not a security one, so the safe direction when OUR config is broken is
    to keep serving and complain loudly, never to refuse a paying customer's upload

    PYTHONPATH=src python3 tests/selftest_775_entitlement.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.server import entitlements as E  # noqa: E402
from dbsearch.server import tiers as T  # noqa: E402
from dbsearch.server.accounts import InMemoryAccountStore  # noqa: E402

GB = 1024 ** 3


def _ladder():
    os.environ["DBSEARCH_TIERS"] = json.dumps([
        {"name": "free", "quota_gb": 10, "price_cents": 0},
        {"name": "plus", "quota_gb": 50, "price_cents": 99, "stripe_price": "price_plus"},
        {"name": "pro", "quota_gb": 1024, "price_cents": 899, "stripe_price": "price_pro"},
    ])
    T.reset_cache()


def test_an_account_nobody_told_us_about_is_on_the_free_tier():
    _ladder()
    store = InMemoryAccountStore()
    assert E.effective_tier(store, "acct-new").name == "free"
    assert E.quota_bytes(store, "acct-new") == 10 * GB


def test_a_paid_subscription_is_read_back():
    _ladder()
    store = InMemoryAccountStore()
    store.set_entitlement("acct-1", tier="pro", status="active",
                          stripe_customer_id="cus_1", stripe_subscription_id="sub_1")
    assert E.effective_tier(store, "acct-1").name == "pro"
    assert E.quota_bytes(store, "acct-1") == 1024 * GB
    row = store.get_entitlement("acct-1")
    assert row["stripe_customer_id"] == "cus_1", row
    assert row["stripe_subscription_id"] == "sub_1", row


def test_a_cancelled_subscription_falls_back_to_free():
    _ladder()
    store = InMemoryAccountStore()
    store.set_entitlement("acct-2", tier="pro", status="canceled")
    assert E.effective_tier(store, "acct-2").name == "free", (
        "a cancelled subscription still grants its paid quota")


def test_a_subscription_cancelling_at_period_end_keeps_the_month_it_paid_for():
    """The portal is configured to cancel at the END of the billing period, so Stripe keeps
    reporting `active` until then. Downgrading the moment they click cancel would take away
    storage they have already paid for."""
    _ladder()
    store = InMemoryAccountStore()
    store.set_entitlement("acct-3", tier="plus", status="active", cancel_at_period_end=True)
    assert E.effective_tier(store, "acct-3").name == "plus"


def test_a_past_due_subscription_still_serves_while_stripe_retries():
    """Stripe retries a failed payment for days before giving up. Cutting storage off at the
    first failed charge would punish an expired card, which is the single most common and
    most recoverable billing failure there is."""
    _ladder()
    store = InMemoryAccountStore()
    store.set_entitlement("acct-4", tier="plus", status="past_due")
    assert E.effective_tier(store, "acct-4").name == "plus"


def test_an_entitlement_naming_a_tier_we_no_longer_have_does_not_downgrade_anyone():
    """Our config broke, not their payment. Quota is billing, not security, so the safe
    direction is to keep serving. Enforcement asks for `quota_bytes`, and None means
    'do not enforce' rather than zero."""
    _ladder()
    store = InMemoryAccountStore()
    store.set_entitlement("acct-5", tier="enterprise", status="active")
    assert E.quota_bytes(store, "acct-5") is None, (
        "an unknown tier resolved to a real quota, so a paying customer is being enforced "
        "against a number we invented")
    assert E.effective_tier(store, "acct-5") is None


def test_the_tier_for_a_stripe_price_is_found_from_the_ladder():
    """The webhook receives a PRICE id and has to name a tier. The mapping is the ladder, so
    changing what a price grants is a config edit like everything else."""
    _ladder()
    assert E.tier_for_price("price_pro").name == "pro"
    assert E.tier_for_price("price_plus").name == "plus"
    assert E.tier_for_price("price_unknown") is None


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {e}")
            except Exception as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    os.environ.pop("DBSEARCH_TIERS", None)
    T.reset_cache()
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
