"""#775 - Stripe billing: checkout, the customer portal, and the webhook that grants a tier.

THE WEBHOOK IS THE ONLY THING THAT CAN GRANT PAID STORAGE, and it is an endpoint anybody on
the internet can post to. Everything here is written from that starting point: a request is
worth nothing until its signature is verified against the payload we actually received.

Signature verification is done here rather than through `stripe.Webhook.construct_event` for
one reason: the `stripe` package is a LAZY, optional import (LAW 7) so a self-hosted box that
sells nothing never needs it, and refusing a forged webhook must not depend on an optional
dependency being installed. The scheme is small and fully specified - HMAC-SHA256 over
`{timestamp}.{raw body}`, compared in constant time, inside a tolerance window - so it is
implemented directly and tested directly.

What the webhook may do is deliberately narrow: it reads the subscription's price, maps it to
a tier through the ladder (so what a price GRANTS stays configuration), and records the
result. It never trusts a tier name sent to it, because then the tier would be whatever the
caller typed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time

from dbsearch.server import entitlements, tiers

_log = logging.getLogger("dbsearch")

#: Stripe's own default tolerance. Older than this and a captured request is being replayed.
SIGNATURE_TOLERANCE_SECONDS = 300

#: The metadata key carrying OUR account id through Stripe and back. Set at checkout, echoed
#: on every subscription event, so a webhook never has to guess who it is about.
ACCOUNT_METADATA_KEY = "dbsearch_account_id"


class WebhookRefused(Exception):
    """The request is not provably from Stripe. Deliberately carries no detail worth
    leaking: an error that explains WHICH part of the signature failed is an oracle for
    tuning a forgery against."""


def webhook_secret() -> str:
    return (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()


def secret_key() -> str:
    return (os.environ.get("STRIPE_SECRET_KEY") or "").strip()


def configured() -> bool:
    """Whether this deployment can sell anything at all.

    A self-hosted box has no keys, sells nothing, and is free forever (ADR 0027 rule 6). Its
    UI must not draw an upgrade button, which is the #654 always-fails-tile lesson applied to
    a payment."""
    return bool(secret_key()) and any(t.sellable for t in tiers.tiers())


def verify_signature(payload: bytes, header: str, *, secret: "str | None" = None,
                     now: "float | None" = None) -> None:
    """Raise WebhookRefused unless `header` is a valid Stripe signature over `payload`.

    The signature covers the RAW BODY, so the caller must hand over the bytes it received,
    never a re-serialized parse of them: `json.dumps(json.loads(body))` can differ from the
    original by a space and would fail every time, or worse, be "fixed" by verifying the
    parse instead of the payload, which verifies nothing.
    """
    secret = webhook_secret() if secret is None else secret
    if not secret:
        raise WebhookRefused("billing is not configured")
    if not header:
        raise WebhookRefused("unsigned")

    ts, sigs = None, []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1":
            sigs.append(v)
    if ts is None or not sigs:
        raise WebhookRefused("malformed signature")
    try:
        ts_int = int(ts)
    except ValueError:
        raise WebhookRefused("malformed signature") from None

    now = time.time() if now is None else now
    # Both directions: a far-FUTURE timestamp is as suspicious as an old one, and allowing it
    # would let one captured request be replayed for as long as the forger cared to wait.
    if abs(now - ts_int) > SIGNATURE_TOLERANCE_SECONDS:
        raise WebhookRefused("stale")

    expected = hmac.new(secret.encode(), f"{ts_int}.".encode() + payload,
                        hashlib.sha256).hexdigest()
    # compare_digest, and against EVERY v1 the header carries: Stripe sends more than one
    # while a signing secret is being rotated.
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise WebhookRefused("bad signature")


def _price_id(sub: dict) -> str:
    for item in ((sub.get("items") or {}).get("data") or []):
        pid = (item.get("price") or {}).get("id")
        if pid:
            return str(pid)
    return ""


def _current_row(store, account_id: str) -> dict:
    """#838: what this account's entitlement says right now, for the ordering guards.

    Never raises: a billing lookup that fails must not make the webhook 5xx, because Stripe
    would then retry it - and a retry storm on a read error is how a transient blip becomes
    a queue. An unreadable row returns {} and both guards fall through to writing, which is
    the same behaviour this webhook had before the guards existed.
    """
    getter = getattr(store, "get_entitlement", None)
    if getter is None:
        return {}
    try:
        return getter(account_id) or {}
    except Exception as exc:
        _log.error("entitlement lookup failed inside the webhook: %s", type(exc).__name__)
        return {}


def handle_event(event: dict, store) -> str:
    """Apply one verified Stripe event. Returns a short word for the log and the response.

    Idempotent by construction rather than by bookkeeping: every path writes the subscription's
    CURRENT state wholesale, so the same event applied twice lands on the same row. Stripe
    delivers at least once and retries on any non-2xx, so duplicates are ordinary traffic.
    """
    etype = str(event.get("type") or "")
    obj = ((event.get("data") or {}).get("object") or {})

    if not etype.startswith("customer.subscription."):
        return "ignored"

    account_id = str((obj.get("metadata") or {}).get(ACCOUNT_METADATA_KEY) or "")
    customer = str(obj.get("customer") or "")
    if not account_id and customer:
        # A subscription changed in the Stripe dashboard carries no metadata of ours, so fall
        # back to the customer id we recorded at checkout.
        finder = getattr(store, "account_for_stripe_customer", None)
        account_id = (finder(customer) if finder else "") or ""
    if not account_id:
        _log.error("stripe webhook %s carries no account id and no known customer", etype)
        return "unattributed"

    price = _price_id(obj)
    tier = entitlements.tier_for_price(price)
    if tier is None:
        # Never guess. Upgrading to "whatever tier is first" on an unrecognised price is how
        # a pricing experiment in the dashboard silently hands out a terabyte.
        _log.error("stripe webhook names price %r, which no tier in the ladder grants", price)
        return "unknown-price"

    status = str(obj.get("status") or "")
    if etype.endswith(".deleted") and status not in ("canceled", "unpaid"):
        status = "canceled"          # the event IS the cancellation, whatever the object says

    # #838: "overwrite wholesale" needs something to overwrite AGAINST. Stripe does not
    # guarantee delivery order and retries a failed event for days, so an older event can
    # legitimately arrive after a newer one - no attacker required, just ordinary retry
    # traffic - and an unconditional write let it undo the newer state permanently, because
    # after a `.deleted` no further events arrive for that subscription to put it right.
    #
    # Both guards REFUSE only; neither invents a tier, and an in-order stream is written
    # exactly as before.
    subscription_id = str(obj.get("id") or "") or None
    event_at = int(event.get("created") or 0)
    current = _current_row(store, account_id)

    # (a) STALE: an event older than the one that last wrote this row. Equal timestamps still
    # apply, so a genuine duplicate stays idempotent. A row with no recorded timestamp (one
    # written before this shipped) cannot be ordered, so it is NOT refused - for money the
    # conservative direction is to keep serving, the same way #775's entitlement lookup leans.
    last_at = current.get("last_event_at") if current else None
    if event_at and last_at and event_at < int(last_at):
        _log.warning("stripe webhook %s is older than this account's last event "
                     "(%s < %s) - refusing to rewind the entitlement", etype, event_at, last_at)
        return "stale"

    # (b) FOREIGN SUBSCRIPTION: a cancellation for a subscription this account has already
    # moved on from. Cancel-then-resubscribe is an ordinary customer journey, and at the old
    # subscription's period end Stripe emits `.deleted` for it - which, applied blindly, drops
    # a customer who is currently PAYING on a different subscription down to free enforcement.
    if etype.endswith(".deleted") and current:
        recorded = current.get("stripe_subscription_id")
        if recorded and subscription_id and recorded != subscription_id:
            _log.warning("stripe webhook cancels subscription %s but this account is on %s "
                         "- refusing to cancel an entitlement it does not describe",
                         subscription_id, recorded)
            return "superseded-subscription"

    store.set_entitlement(
        account_id,
        tier=tier.name,
        status=status,
        stripe_customer_id=customer or None,
        stripe_subscription_id=subscription_id,
        cancel_at_period_end=bool(obj.get("cancel_at_period_end")),
        current_period_end=obj.get("current_period_end"),
        last_event_at=event_at or None,
    )
    return f"{tier.name}:{status}"


def parse_event(payload: bytes) -> dict:
    try:
        event = json.loads(payload)
    except ValueError:
        raise WebhookRefused("unparseable") from None
    if not isinstance(event, dict):
        raise WebhookRefused("unparseable")
    return event


# ---- outbound calls (lazy import, so a self-host box never needs the package) -------------

def _stripe():
    import stripe                                   # LAW 7: optional, imported at use

    key = secret_key()
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY is not set")
    stripe.api_key = key
    return stripe


def checkout_url(*, tier_name: str, account_id: str, success_url: str, cancel_url: str,
                 customer_email: "str | None" = None,
                 stripe_customer_id: "str | None" = None) -> str:
    """A Stripe Checkout session for one tier, carrying our account id in metadata.

    The metadata is what makes the webhook attributable: Stripe echoes it back on the
    subscription, so the grant lands on the right account without a lookup that could miss.
    """
    tier = tiers.tier(tier_name)
    if not tier.sellable:
        raise ValueError(f"tier {tier_name!r} has no Stripe price, so it cannot be sold")
    s = _stripe()
    kwargs = {
        "mode": "subscription",
        "line_items": [{"price": tier.stripe_price, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": account_id,
        "metadata": {ACCOUNT_METADATA_KEY: account_id},
        # Echoed onto the subscription itself, which is what the webhook reads.
        "subscription_data": {"metadata": {ACCOUNT_METADATA_KEY: account_id}},
    }
    if stripe_customer_id:
        kwargs["customer"] = stripe_customer_id
    elif customer_email:
        kwargs["customer_email"] = customer_email
    return s.checkout.Session.create(**kwargs).url


def portal_url(*, stripe_customer_id: str, return_url: str) -> str:
    """The Stripe-hosted portal, where a customer changes plan, updates a card, or cancels.
    Configured in the dashboard, so none of that is our UI to build or keep correct."""
    s = _stripe()
    return s.billing_portal.Session.create(
        customer=stripe_customer_id, return_url=return_url).url
