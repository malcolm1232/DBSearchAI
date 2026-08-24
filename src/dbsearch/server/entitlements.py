"""#775 - what is this account entitled to store, and why.

Stripe is the source of truth for what somebody is paying for; this is the local cache of
that answer plus the policy for reading it. The cache exists so the upload path never has to
call Stripe: that would be slow, and it would make Stripe's availability a precondition for
using a product the customer has already paid for.

Every rule here leans the same way, and deliberately. Quota is a BILLING concern, not a
security one, so when something is ambiguous or broken on our side the safe direction is to
keep serving and complain loudly. Refusing a paying customer's upload because our config
drifted is a worse failure than briefly over-serving one. LAW 2 is untouched by any of this:
nothing here changes what a caller may READ (ADR 0027, LAW 2 section).
"""
from __future__ import annotations

import logging

from dbsearch.server import tiers

_log = logging.getLogger("dbsearch")

#: Subscription statuses that still grant the paid tier.
#:
#: `active` covers the ordinary case, including a subscription set to cancel at period end -
#: the portal is configured that way, and the customer keeps the month they paid for.
#: `trialing` is a trial we have not started yet but might.
#: `past_due` is the important one: Stripe retries a failed charge for days before giving up,
#: and cutting storage off at the first failed charge would punish an expired card, which is
#: the most common and most recoverable billing failure there is. When Stripe finally gives
#: up it moves the subscription to `canceled` or `unpaid`, and those are not here.
SERVING_STATUSES = frozenset({"active", "trialing", "past_due"})


def _row(store, account_id: str) -> "dict | None":
    getter = getattr(store, "get_entitlement", None)
    if getter is None:
        return None                      # a store too old to know; everyone is on free
    try:
        return getter(account_id)
    except Exception as e:               # a billing lookup must never 500 an upload
        _log.error("entitlement lookup failed for one account: %s", type(e).__name__)
        return None


def effective_tier(store, account_id: str) -> "tiers.Tier | None":
    """The tier this account may use right now.

    Returns the FREE tier for an account nobody has told us about, which is every new signup.
    Returns None only when the cache names a tier this deployment's ladder does not have -
    an operator error, not a customer one - and the caller must read that as "cannot say,
    so do not enforce" rather than as "no quota".
    """
    row = _row(store, account_id)
    if not row or str(row.get("status") or "") not in SERVING_STATUSES:
        return tiers.free_tier()
    name = str(row.get("tier") or "")
    try:
        return tiers.tier(name)
    except tiers.UnknownTier:
        _log.error(
            "account is entitled to tier %r, which is not in this deployment's ladder (%s). "
            "NOT enforcing a quota for it: the configuration is wrong, not the payment.",
            name, ", ".join(t.name for t in tiers.tiers()))
        return None


def quota_bytes(store, account_id: str) -> "int | None":
    """The byte ceiling for this account, or None meaning "do not enforce"."""
    tier = effective_tier(store, account_id)
    return None if tier is None else tier.quota_bytes


def tier_for_price(stripe_price: str) -> "tiers.Tier | None":
    """Which tier a Stripe price grants.

    The webhook is handed a price id and has to name a tier, and the mapping lives in the
    ladder, so changing what a price grants stays a config edit like everything else. An
    unrecognised price returns None: the webhook must then refuse to guess rather than
    upgrade somebody to whatever tier happens to be first.
    """
    if not stripe_price:
        return None
    for t in tiers.tiers():
        if t.stripe_price and t.stripe_price == stripe_price:
            return t
    return None
