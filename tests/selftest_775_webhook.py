"""#775 - the Stripe webhook is the only thing that can grant a paid tier, so it is the only
thing that must never be fooled.

A webhook endpoint is a hole in the side of the application that anybody on the internet can
post to, and what it does is UPGRADE ACCOUNTS. If it can be talked into acting on an unsigned
body, then storage is free to whoever can spell the JSON. So the tests here are mostly about
refusing:

  * an unsigned body is refused
  * a body signed with the wrong secret is refused
  * a correctly signed body whose CONTENT was then changed is refused (that is the whole
    point of signing the payload rather than the ids inside it)
  * an old signature is refused, so a captured request cannot be replayed forever
  * a price the ladder does not know does not silently upgrade anyone

And one that is about not losing money the other way: the same event delivered twice must
leave the account in the same state, because Stripe retries and at-least-once delivery is
the contract it offers.

    PYTHONPATH=src python3 tests/selftest_775_webhook.py
"""
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import tiers as T  # noqa: E402
from dbsearch.server.app import ACCOUNTS, app  # noqa: E402

client = TestClient(app)
SECRET = "whsec_test_775_secret"
ACCT = "acct-webhook-775"

LADDER = json.dumps([
    {"name": "free", "quota_gb": 10, "price_cents": 0},
    {"name": "plus", "quota_gb": 50, "price_cents": 99, "stripe_price": "price_plus_775"},
    {"name": "pro", "quota_gb": 1024, "price_cents": 899, "stripe_price": "price_pro_775"},
])


def _setup():
    os.environ["DBSEARCH_TIERS"] = LADDER
    os.environ["STRIPE_WEBHOOK_SECRET"] = SECRET
    T.reset_cache()


def _sig(payload: bytes, secret: str = SECRET, ts: "int | None" = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _event(price: str = "price_pro_775", status: str = "active", customer: str = "cus_775",
           account_id: str = ACCT, event_id: str = "evt_775_1") -> bytes:
    return json.dumps({
        "id": event_id,
        "type": "customer.subscription.updated",
        "data": {"object": {
            "id": "sub_775", "customer": customer, "status": status,
            "cancel_at_period_end": False, "current_period_end": int(time.time()) + 86400,
            "metadata": {"dbsearch_account_id": account_id},
            "items": {"data": [{"price": {"id": price}}]},
        }},
    }).encode()


def _post(body: bytes, sig: "str | None"):
    headers = {"Stripe-Signature": sig} if sig else {}
    return client.post("/stripe/webhook", content=body, headers=headers)


def test_a_correctly_signed_subscription_grants_the_tier():
    _setup()
    body = _event()
    r = _post(body, _sig(body))
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    row = ACCOUNTS.get_entitlement(ACCT)
    assert row and row["tier"] == "pro", row
    assert row["status"] == "active", row
    assert row["stripe_customer_id"] == "cus_775", row


def test_an_unsigned_body_is_refused():
    _setup()
    body = _event(account_id="acct-unsigned-775")
    assert _post(body, None).status_code == 400
    assert ACCOUNTS.get_entitlement("acct-unsigned-775") is None, (
        "an unsigned webhook granted a paid tier")


def test_a_body_signed_with_the_wrong_secret_is_refused():
    _setup()
    body = _event(account_id="acct-wrongsecret-775")
    assert _post(body, _sig(body, secret="whsec_not_ours")).status_code == 400
    assert ACCOUNTS.get_entitlement("acct-wrongsecret-775") is None


def test_a_tampered_body_is_refused():
    """The signature covers the PAYLOAD. Signing one body and posting another - the classic
    'upgrade myself to pro' edit - has to fail."""
    _setup()
    honest = _event(price="price_plus_775", account_id="acct-tamper-775")
    sig = _sig(honest)
    tampered = honest.replace(b"price_plus_775", b"price_pro_775xx"[:14])
    assert len(tampered) == len(honest)
    assert _post(tampered, sig).status_code == 400
    assert ACCOUNTS.get_entitlement("acct-tamper-775") is None


def test_an_old_signature_is_refused():
    """Stripe's timestamp tolerance. Without it a captured request is a permanent free
    upgrade for anyone who kept a copy."""
    _setup()
    body = _event(account_id="acct-replay-775")
    old = _sig(body, ts=int(time.time()) - 3600)
    assert _post(body, old).status_code == 400
    assert ACCOUNTS.get_entitlement("acct-replay-775") is None


def test_a_price_the_ladder_does_not_know_upgrades_nobody():
    _setup()
    body = _event(price="price_not_in_the_ladder", account_id="acct-unknownprice-775")
    r = _post(body, _sig(body))
    assert r.status_code in (200, 202), r.status_code   # acknowledged, so Stripe stops retrying
    assert ACCOUNTS.get_entitlement("acct-unknownprice-775") is None, (
        "an unrecognised price granted a tier anyway")


def test_the_same_event_twice_leaves_the_same_state():
    """Stripe delivers at least once and retries on any non-2xx, so duplicates are normal
    traffic rather than an attack."""
    _setup()
    acct = "acct-idempotent-775"
    body = _event(price="price_plus_775", account_id=acct, event_id="evt_775_dupe")
    sig = _sig(body)
    assert _post(body, sig).status_code == 200
    first = ACCOUNTS.get_entitlement(acct)
    assert _post(body, sig).status_code == 200
    assert ACCOUNTS.get_entitlement(acct) == first, "a redelivery changed the entitlement"


def test_a_deleted_subscription_drops_the_account_to_free():
    _setup()
    acct = "acct-deleted-775"
    grant = _event(price="price_pro_775", account_id=acct)
    assert _post(grant, _sig(grant)).status_code == 200
    assert ACCOUNTS.get_entitlement(acct)["tier"] == "pro"

    gone = json.dumps({
        "id": "evt_775_del", "type": "customer.subscription.deleted",
        "data": {"object": {
            "id": "sub_775", "customer": "cus_775", "status": "canceled",
            "metadata": {"dbsearch_account_id": acct},
            "items": {"data": [{"price": {"id": "price_pro_775"}}]},
        }},
    }).encode()
    assert _post(gone, _sig(gone)).status_code == 200
    row = ACCOUNTS.get_entitlement(acct)
    assert row["status"] == "canceled", row


def test_the_endpoint_is_silent_about_why_it_refused():
    """A verification oracle would let somebody tune a forgery against the error text."""
    _setup()
    body = _event(account_id="acct-quiet-775")
    detail = (_post(body, _sig(body, secret="nope")).json().get("detail") or "").lower()
    for leak in ("secret", "expected", "computed", "hmac"):
        assert leak not in detail, f"the refusal explains itself too well: {detail!r}"


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
    for k in ("DBSEARCH_TIERS", "STRIPE_WEBHOOK_SECRET"):
        os.environ.pop(k, None)
    T.reset_cache()
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
