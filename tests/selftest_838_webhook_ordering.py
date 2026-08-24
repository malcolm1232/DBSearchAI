"""#838 - a late Stripe event cannot rewind an entitlement.

FOUND by the cold adversarial review of the 260818 money path (#837), by two independent
readers, and confirmed by tracing both write layers.

`handle_event` wrote the subscription's whole current state wholesale and called itself
"idempotent by construction". That is true only for IDENTICAL events. It said nothing about
ORDER - and Stripe explicitly does not guarantee delivery order and retries a failed event
for up to three days. So "overwrite wholesale" had nothing to overwrite against:

  * a `.updated`(active) that 5xx'd, retried after the `.deleted` that cancelled it, wrote
    the account back to paid - PERMANENTLY, because once a subscription is deleted Stripe
    sends no further events for it to put things right;
  * a customer who cancelled and resubscribed got dropped to free enforcement when the OLD
    subscription's `.deleted` landed at its period end and clobbered the row that recorded
    the NEW, paying one.

Neither needs an attacker. Both are ordinary retry and lifecycle traffic.

BOTH GUARDS ONLY REFUSE. Neither invents a tier, neither upgrades anyone, and an in-order
event stream is written exactly as it was before - which is what
`test_an_in_order_stream_is_unchanged` pins. This is money, so the failure direction is
chosen deliberately: an event that cannot be ordered (a row written before `last_event_at`
existed) is APPLIED rather than refused, because refusing a real change is worse than
briefly over-serving.

THREE CLAUSES, THREE MUTATIONS:
  1. stale events refused        -> test_a_retried_older_event_cannot_restore_a_cancellation
  2. foreign cancels refused     -> test_an_old_subscriptions_cancellation_cannot_downgrade
  3. the timestamp is persisted  -> test_the_event_timestamp_is_recorded

    PYTHONPATH=src python3 tests/selftest_838_webhook_ordering.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ["DBSEARCH_TIERS"] = json.dumps([
    {"name": "free", "quota_gb": 10, "price_cents": 0},
    {"name": "pro", "quota_gb": 1024, "price_cents": 899, "stripe_price": "price_pro"},
])

from dbsearch.server import billing, entitlements  # noqa: E402
from dbsearch.server import tiers as T  # noqa: E402
from dbsearch.server.accounts import InMemoryAccountStore  # noqa: E402

T.reset_cache()
ACCOUNT = "acct_838"


def _event(etype, sub, status, created, price="price_pro"):
    return {"type": etype, "created": created, "data": {"object": {
        "id": sub, "status": status, "customer": "cus_838",
        "metadata": {billing.ACCOUNT_METADATA_KEY: ACCOUNT},
        "items": {"data": [{"price": {"id": price}}]}}}}


def _tier(store):
    t = entitlements.effective_tier(store, ACCOUNT)
    return None if t is None else t.name


def test_a_retried_older_event_cannot_restore_a_cancellation():
    """CLAUSE 1. Ordinary retry traffic: the `.updated` 5xx'd, was retried, and landed after
    the `.deleted`. Nothing follows a deletion, so the wrong answer would be permanent."""
    s = InMemoryAccountStore()
    billing.handle_event(_event("customer.subscription.updated", "sub_A", "active", 100), s)
    billing.handle_event(_event("customer.subscription.deleted", "sub_A", "canceled", 200), s)
    assert _tier(s) == "free", "fixture is broken: the cancellation did not take effect"

    verdict = billing.handle_event(
        _event("customer.subscription.updated", "sub_A", "active", 100), s)
    assert _tier(s) == "free", (
        f"#838: a RETRIED older event restored a cancelled subscription to {_tier(s)!r}. "
        f"No further Stripe event follows a deletion, so the account keeps paid entitlement "
        f"for good.")
    assert verdict == "stale", verdict


def test_an_old_subscriptions_cancellation_cannot_downgrade():
    """CLAUSE 2. Cancel-then-resubscribe is an ordinary journey, and at the old
    subscription's period end Stripe emits `.deleted` for it."""
    s = InMemoryAccountStore()
    billing.handle_event(_event("customer.subscription.updated", "sub_OLD", "active", 100), s)
    billing.handle_event(_event("customer.subscription.created", "sub_NEW", "active", 300), s)
    assert _tier(s) == "pro", "fixture is broken: the new subscription did not take"

    verdict = billing.handle_event(
        _event("customer.subscription.deleted", "sub_OLD", "canceled", 400), s)
    assert _tier(s) == "pro", (
        f"#838: the OLD subscription's cancellation dropped a PAYING customer to "
        f"{_tier(s)!r}. They are on sub_NEW and would get 402 'upgrade your plan' at the "
        f"free quota while paying for 1TB.")
    assert verdict == "superseded-subscription", verdict


def test_a_real_cancellation_still_cancels():
    """THE CONTROL THAT MATTERS. A guard that refused cancellations would 'pass' both tests
    above and mean we can never stop billing anyone's entitlement."""
    s = InMemoryAccountStore()
    billing.handle_event(_event("customer.subscription.updated", "sub_A", "active", 100), s)
    verdict = billing.handle_event(
        _event("customer.subscription.deleted", "sub_A", "canceled", 200), s)
    assert _tier(s) == "free", (
        f"a genuine cancellation of the CURRENT subscription no longer cancels: {_tier(s)!r}")
    assert verdict == "pro:canceled", verdict


def test_an_in_order_stream_is_unchanged():
    """CONTROL: normal traffic must be written exactly as before. The guards refuse only."""
    s = InMemoryAccountStore()
    assert billing.handle_event(
        _event("customer.subscription.created", "sub_A", "trialing", 100), s) == "pro:trialing"
    assert billing.handle_event(
        _event("customer.subscription.updated", "sub_A", "active", 200), s) == "pro:active"
    assert billing.handle_event(
        _event("customer.subscription.updated", "sub_A", "past_due", 300), s) == "pro:past_due"
    assert _tier(s) == "pro", "past_due must keep serving (#775): Stripe retries for days"
    assert billing.handle_event(
        _event("customer.subscription.deleted", "sub_A", "canceled", 400), s) == "pro:canceled"
    assert _tier(s) == "free"


def test_a_duplicate_event_is_still_idempotent():
    """CONTROL: equal timestamps must still APPLY - Stripe delivers at least once, so exact
    duplicates are ordinary traffic and must not be treated as stale."""
    s = InMemoryAccountStore()
    e = _event("customer.subscription.updated", "sub_A", "active", 100)
    assert billing.handle_event(e, s) == "pro:active"
    assert billing.handle_event(e, s) == "pro:active", "a duplicate was refused as stale"
    assert _tier(s) == "pro"


def test_an_unorderable_row_is_not_refused():
    """CONTROL, and the deliberate failure direction. A row written before `last_event_at`
    existed cannot be ordered; for money, applying a real change beats refusing it."""
    s = InMemoryAccountStore()
    s.set_entitlement(ACCOUNT, tier="pro", status="active",
                      stripe_customer_id="cus_838", stripe_subscription_id="sub_A")
    assert (s.get_entitlement(ACCOUNT) or {}).get("last_event_at") is None
    assert billing.handle_event(
        _event("customer.subscription.deleted", "sub_A", "canceled", 500), s) == "pro:canceled"
    assert _tier(s) == "free", "a legacy row could never be updated again"


def test_the_event_timestamp_is_recorded():
    """CLAUSE 3. Without persisting it there is nothing to compare against, and clause 1
    silently becomes a no-op that still passes its own happy path."""
    s = InMemoryAccountStore()
    billing.handle_event(_event("customer.subscription.updated", "sub_A", "active", 12345), s)
    row = s.get_entitlement(ACCOUNT) or {}
    assert row.get("last_event_at") == 12345, (
        f"#838: the event timestamp was not persisted, so nothing can order the next "
        f"event against it: {row!r}")


def test_a_lookup_failure_does_not_break_the_webhook():
    """A read error inside the guard must not 5xx: Stripe would retry, and a retry storm on
    a transient blip is how a hiccup becomes a queue. Fall through to the old behaviour."""
    class _Broken(InMemoryAccountStore):
        def get_entitlement(self, account_id):
            raise RuntimeError("db down")

    s = _Broken()
    assert billing.handle_event(
        _event("customer.subscription.updated", "sub_A", "active", 100), s) == "pro:active"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}\n        {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
