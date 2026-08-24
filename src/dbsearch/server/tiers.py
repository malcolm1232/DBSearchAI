"""#775 / ADR 0027 rule 5 - the storage tier ladder, as CONFIGURATION.

The ladder is one env var, `DBSEARCH_TIERS`, holding a JSON list of tiers. Each tier carries
its own quota AND its own price, so the thing a business actually tunes - how much storage a
dollar buys - moves without a source change. That was the owner's requirement in their own
words: "make sure we can change the amount of storage per dollar via a variable".

    DBSEARCH_TIERS='[{"name":"free","quota_gb":10,"price_cents":0},
                     {"name":"plus","quota_gb":50,"price_cents":99,"stripe_price":"price_..."},
                     {"name":"pro","quota_gb":1024,"price_cents":899,"stripe_price":"price_..."}]'

Unset, it is the ladder the owner agreed on 2026-08-18: 10GB free, 50GB for $0.99, 1TB for
$8.99. Those tiers are straight STORAGE. LLM usage is billed separately as pass-through, so
a storage tier never has to absorb an unbounded query bill.

WHY THIS REFUSES INSTEAD OF FALLING BACK. Everywhere else in this codebase a config it cannot
read degrades to something safe. Here there is no such thing. A malformed ladder that quietly
became the default would bill one amount while enforcing another, or hand out storage nobody
paid for, and it would do it with nothing in the log to notice. So every way of being wrong
raises `TierConfigError` at the first read, which is boot. Same reasoning as refusing to
answer with an empty result set when a store is down: silence that looks like success is the
failure mode worth engineering against.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

BYTES_PER_GB = 1024 ** 3

#: The ladder agreed 2026-08-18. Prices are in CENTS to keep money out of floating point.
DEFAULT_TIERS = [
    {"name": "free", "quota_gb": 10, "price_cents": 0},
    {"name": "plus", "quota_gb": 50, "price_cents": 99},
    {"name": "pro", "quota_gb": 1024, "price_cents": 899},
]


class TierConfigError(Exception):
    """DBSEARCH_TIERS cannot be read. Fatal on purpose - see the module docstring."""


class UnknownTier(Exception):
    """An account names a tier this deployment's ladder does not have."""


@dataclass(frozen=True)
class Tier:
    name: str
    #: May be fractional: a 500MB tier is a legitimate thing to sell, and the smallest
    #: whole gigabyte is far too coarse to test enforcement against.
    quota_gb: float
    price_cents: int
    #: The Stripe price id, once one exists. A paid tier may legitimately be configured
    #: BEFORE its Stripe price is created - the ladder is decided first - so this is
    #: optional, and `sellable` is what a checkout surface must consult.
    stripe_price: "str | None" = None

    @property
    def quota_bytes(self) -> int:
        return int(self.quota_gb * BYTES_PER_GB)

    @property
    def is_free(self) -> bool:
        return self.price_cents == 0

    @property
    def sellable(self) -> bool:
        """Whether a customer can actually buy this today. A paid tier with no Stripe price
        is not offerable: offering it would draw a button whose checkout can only fail, which
        is the always-fails-tile trap (#551) with a payment attached."""
        return not self.is_free and bool(self.stripe_price)


_cache: "list[Tier] | None" = None


def reset_cache() -> None:
    """Drop the parsed ladder. For tests and for a deliberate re-read; normal operation
    parses once at first use, so a config edit takes a restart (ADR 0027 rule 5)."""
    global _cache
    _cache = None


def _fail(why: str, raw: str) -> "TierConfigError":
    shown = raw if len(raw) <= 300 else raw[:300] + "..."
    return TierConfigError(
        f"DBSEARCH_TIERS is unusable: {why}. Refusing to start rather than guess, because a "
        f"guessed billing ladder charges the wrong amount silently. Value was: {shown!r}")


def _parse(raw: str) -> "list[Tier]":
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise _fail(f"not valid JSON ({e})", raw) from None
    if not isinstance(data, list):
        raise _fail(f"the top level is {type(data).__name__}, not a list of tiers", raw)
    if not data:
        raise _fail("the ladder is empty, so no account could have any quota at all", raw)

    out: list[Tier] = []
    seen: set[str] = set()
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise _fail(f"entry {i} is {type(row).__name__}, not an object", raw)
        for field in ("name", "quota_gb", "price_cents"):
            if field not in row:
                raise _fail(f"entry {i} has no {field!r}", raw)
        name = row["name"]
        if not isinstance(name, str) or not name.strip():
            raise _fail(f"entry {i} has an empty name", raw)
        if name in seen:
            raise _fail(f"{name!r} appears twice, so a lookup would be ambiguous", raw)
        seen.add(name)
        quota, price = row["quota_gb"], row["price_cents"]
        # bool is an int in Python, and `True` as a quota is a config typo, not 1GB.
        if isinstance(quota, bool) or not isinstance(quota, (int, float)) or quota < 0:
            raise _fail(f"{name!r} has quota_gb={quota!r}, which is not a size in GB", raw)
        if isinstance(price, bool) or not isinstance(price, int) or price < 0:
            raise _fail(f"{name!r} has price_cents={price!r}, which is not a whole number of "
                        "cents", raw)
        sp = row.get("stripe_price")
        if sp is not None and (not isinstance(sp, str) or not sp.strip()):
            raise _fail(f"{name!r} has a stripe_price that is not a string", raw)
        out.append(Tier(name=name, quota_gb=quota, price_cents=price, stripe_price=sp))

    if not any(t.is_free for t in out):
        raise _fail("no tier costs nothing, so a new account would have nowhere to land", raw)
    return out


def _unwrap(raw: str) -> str:
    """Strip one layer of matched surrounding quotes.

    A JSON value in an env file arrives differently depending on who reads the file.
    `set -a; . file` is shell: it strips the quotes around a value, and the JSON's own double
    quotes with them unless the whole value is single-quoted. Docker Compose's `env_file:`
    does no shell parsing at all, so single quotes written to satisfy the shell arrive as part
    of the value. One file, two transports, opposite requirements.

    This is not guessing at an unreadable ladder: the JSON inside is byte-identical either
    way, and refusing it would turn a transport artefact into a failed boot.
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1].strip()
    return raw


def tiers() -> "list[Tier]":
    """The ladder, in configured order. Parsed once; `reset_cache()` forces a re-read.

    Sources, in precedence order: `DBSEARCH_TIERS` (inline JSON, so a one-off override needs
    no file), then `DBSEARCH_TIERS_FILE` (a path, which is the robust way to carry JSON
    through a deployment), then the default ladder.
    """
    global _cache
    if _cache is not None:
        return list(_cache)

    raw = _unwrap(os.environ.get("DBSEARCH_TIERS", ""))
    if raw:
        _cache = _parse(raw)
        return list(_cache)

    path = _unwrap(os.environ.get("DBSEARCH_TIERS_FILE", ""))
    if path:
        # A named file that is not there is a typo, and silently billing the default ladder
        # instead is exactly the failure this module exists to refuse.
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as e:
            raise TierConfigError(
                f"DBSEARCH_TIERS_FILE names {path!r}, which could not be read ({e}). Refusing "
                "to fall back to the default ladder, because a mistyped path would then bill "
                "the wrong amount with nothing in the log.") from None
        _cache = _parse(raw)
        return list(_cache)

    _cache = _parse(json.dumps(DEFAULT_TIERS))
    return list(_cache)


def tier(name: str) -> Tier:
    """One tier by name.

    Raises rather than resolving an unknown name. An account row naming a tier the ladder no
    longer has is a real situation (renamed, removed), and both silent answers are wrong:
    falling back to free downgrades a paying customer, falling back to the largest gives
    storage away. The caller has the account in hand and can decide; this function does not.
    """
    for t in tiers():
        if t.name == name:
            return t
    raise UnknownTier(
        f"no tier named {name!r} in this deployment's ladder "
        f"({', '.join(t.name for t in tiers())}). An account referencing it needs an explicit "
        "decision, not a default.")


def free_tier() -> Tier:
    """Where a new account lands: the first tier that costs nothing."""
    for t in tiers():
        if t.is_free:
            return t
    raise TierConfigError("no free tier")     # unreachable: _parse already refuses this


def sellable_tiers() -> "list[Tier]":
    """The tiers a checkout surface may actually offer today."""
    return [t for t in tiers() if t.sellable]
