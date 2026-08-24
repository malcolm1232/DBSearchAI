"""#775 - the tier ladder is CONFIGURATION, and it refuses to guess.

ADR 0027 rule 5: free and paid tiers, each carrying its own quota and price, ship as one
config structure read at boot. Changing a quota, a price, or the number of tiers is a config
edit, never a source change. The owner's requirement in their own words: "make sure we can
change the amount of storage per dollar via a variable".

The rule that shapes every test below: **a billing config that cannot be understood must
fail loudly, never quietly fall back to a default.** A silent fallback here does not degrade
a feature, it bills the wrong amount or hands out storage nobody paid for, and it does it
without a single line in the log. That is the "empty success hides an outage" shape applied
to money.

    PYTHONPATH=src python3 tests/selftest_775_tiers.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.server import tiers as T  # noqa: E402

GB = 1024 ** 3


def _with_env(value):
    """Set (or clear) DBSEARCH_TIERS and drop the cache, the way a boot would see it."""
    if value is None:
        os.environ.pop("DBSEARCH_TIERS", None)
    else:
        os.environ["DBSEARCH_TIERS"] = value
    T.reset_cache()


def test_the_default_ladder_is_the_one_the_owner_agreed():
    _with_env(None)
    names = [t.name for t in T.tiers()]
    assert names == ["free", "plus", "pro"], names
    free, plus, pro = T.tiers()
    assert (free.quota_gb, free.price_cents) == (10, 0), free
    assert (plus.quota_gb, plus.price_cents) == (50, 99), plus
    assert (pro.quota_gb, pro.price_cents) == (1024, 899), pro


def test_quota_is_reported_in_bytes_because_that_is_what_a_file_is_measured_in():
    _with_env(None)
    assert T.tier("free").quota_bytes == 10 * GB
    assert T.tier("pro").quota_bytes == 1024 * GB


def test_storage_per_dollar_is_changeable_by_configuration_alone():
    """The owner's explicit requirement. Both halves of the ratio have to move: the quota
    AND the price, with no code change and no redeploy of anything but the config."""
    _with_env(json.dumps([
        {"name": "free", "quota_gb": 25, "price_cents": 0},
        {"name": "plus", "quota_gb": 200, "price_cents": 149, "stripe_price": "price_abc"},
    ]))
    assert [t.name for t in T.tiers()] == ["free", "plus"]
    assert T.tier("free").quota_gb == 25
    assert T.tier("plus").quota_gb == 200
    assert T.tier("plus").price_cents == 149
    assert T.tier("plus").stripe_price == "price_abc"
    assert T.tier("plus").quota_bytes == 200 * GB


def test_a_quota_may_be_a_fraction_of_a_gigabyte():
    """A 500MB tier is a legitimate thing to sell, and a tiny one is the only way to test
    enforcement without writing a gigabyte to disk. Whole numbers stay exact."""
    _with_env(json.dumps([
        {"name": "free", "quota_gb": 0.5, "price_cents": 0},
        {"name": "tiny", "quota_gb": 0.000001, "price_cents": 100},
    ]))
    assert T.tier("free").quota_bytes == GB // 2
    assert T.tier("tiny").quota_bytes == int(0.000001 * GB)
    assert T.tier("tiny").quota_bytes > 0, "a tiny quota rounded away to nothing"


def test_the_free_tier_is_whichever_one_costs_nothing():
    _with_env(json.dumps([
        {"name": "starter", "quota_gb": 5, "price_cents": 0},
        {"name": "big", "quota_gb": 500, "price_cents": 500},
    ]))
    assert T.free_tier().name == "starter"
    assert T.free_tier().quota_bytes == 5 * GB


def test_an_unreadable_config_refuses_to_start_instead_of_guessing():
    """The whole point. Each of these used to be a plausible 'just fall back to defaults',
    which would silently bill the wrong amount or hand out storage nobody paid for."""
    for bad, why in [
        ("not json at all", "malformed JSON"),
        ("{}", "an object rather than a list of tiers"),
        ("[]", "an empty ladder, so no account could have any quota"),
        (json.dumps([{"quota_gb": 10, "price_cents": 0}]), "a tier with no name"),
        (json.dumps([{"name": "free", "price_cents": 0}]), "a tier with no quota"),
        (json.dumps([{"name": "free", "quota_gb": 10}]), "a tier with no price"),
        (json.dumps([{"name": "free", "quota_gb": -1, "price_cents": 0}]), "a negative quota"),
        (json.dumps([{"name": "free", "quota_gb": 10, "price_cents": -5}]), "a negative price"),
        (json.dumps([{"name": "a", "quota_gb": 10, "price_cents": 0},
                     {"name": "a", "quota_gb": 20, "price_cents": 100}]), "a duplicate name"),
        (json.dumps([{"name": "a", "quota_gb": 10, "price_cents": 100}]), "no free tier"),
    ]:
        _with_env(bad)
        try:
            T.tiers()
        except T.TierConfigError as e:
            assert str(e), "the refusal carries no message, so nobody can fix the config"
            continue
        raise AssertionError(f"{why} was accepted silently: {bad!r}")


def test_a_paid_tier_without_a_stripe_price_is_legal_but_says_so():
    """Ordering matters: the ladder is configured BEFORE the Stripe prices exist. A paid
    tier with no price id must be expressible, and must be reported as not yet sellable
    rather than offered to a customer and failing at checkout."""
    _with_env(json.dumps([
        {"name": "free", "quota_gb": 10, "price_cents": 0},
        {"name": "plus", "quota_gb": 50, "price_cents": 99},
    ]))
    plus = T.tier("plus")
    assert plus.stripe_price is None
    assert plus.sellable is False, "a paid tier with no Stripe price claims to be sellable"
    assert T.sellable_tiers() == [], T.sellable_tiers()
    _with_env(json.dumps([
        {"name": "free", "quota_gb": 10, "price_cents": 0},
        {"name": "plus", "quota_gb": 50, "price_cents": 99, "stripe_price": "price_x"},
    ]))
    assert [t.name for t in T.sellable_tiers()] == ["plus"]
    assert T.free_tier().sellable is False, "the free tier is not something you buy"


def test_the_ladder_survives_both_ways_an_env_file_is_loaded():
    """A JSON value in an env file arrives DIFFERENTLY depending on who reads the file.

    `set -a; . ./secrets/stripe.env` is shell, so it strips the quotes around a value and the
    JSON's own double quotes with them, unless the value is single-quoted in the file. Docker
    Compose's `env_file:` does no shell parsing at all, so single quotes written for the shell
    arrive as part of the value. One file, two transports, opposite requirements: whichever
    way it is written, the other reader breaks.

    Surrounding quotes are therefore stripped. This is not the module guessing at a ladder it
    cannot read - the ladder inside is byte-identical either way, and refusing it would turn a
    transport artefact into a failed boot. Hit for real writing secrets/stripe.env.
    """
    ladder = [{"name": "free", "quota_gb": 10, "price_cents": 0},
              {"name": "plus", "quota_gb": 50, "price_cents": 99, "stripe_price": "price_x"}]
    inner = json.dumps(ladder)
    for wrapped in (inner, f"'{inner}'", f'"{inner}"', f"  {inner}  "):
        _with_env(wrapped)
        got = T.tiers()
        assert [t.name for t in got] == ["free", "plus"], (wrapped[:40], got)
        assert got[1].stripe_price == "price_x", got[1]


def test_the_ladder_can_live_in_a_file_because_json_belongs_in_one():
    """The robust way to carry JSON through a deployment is a file, so DBSEARCH_TIERS_FILE
    names one. Inline still wins when both are set, so a one-off override needs no file
    edit."""
    import tempfile

    ladder = [{"name": "free", "quota_gb": 7, "price_cents": 0},
              {"name": "big", "quota_gb": 700, "price_cents": 700, "stripe_price": "price_f"}]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(ladder, fh)
        path = fh.name
    try:
        _with_env(None)
        os.environ["DBSEARCH_TIERS_FILE"] = path
        T.reset_cache()
        assert [t.name for t in T.tiers()] == ["free", "big"]
        assert T.tier("big").quota_gb == 700

        os.environ["DBSEARCH_TIERS"] = json.dumps(
            [{"name": "free", "quota_gb": 1, "price_cents": 0}])
        T.reset_cache()
        assert [t.name for t in T.tiers()] == ["free"], "inline did not win over the file"
        assert T.tier("free").quota_gb == 1

        os.environ.pop("DBSEARCH_TIERS", None)
        os.environ["DBSEARCH_TIERS_FILE"] = path + ".missing"
        T.reset_cache()
        try:
            T.tiers()
        except T.TierConfigError as e:
            assert "missing" in str(e) or "not" in str(e).lower(), str(e)
        else:
            raise AssertionError("a named tiers file that does not exist was ignored, so a "
                                 "typo in the path would silently bill the default ladder")
    finally:
        os.environ.pop("DBSEARCH_TIERS_FILE", None)
        os.unlink(path)
        T.reset_cache()


def test_an_unknown_tier_name_is_an_error_not_a_free_ride():
    """An account row naming a tier the config no longer has is a real situation (a tier was
    renamed or removed). Resolving it to free would silently downgrade a paying customer;
    resolving it to the biggest tier would give away storage. Refuse, and let the caller
    decide with the account in front of it."""
    _with_env(None)
    try:
        T.tier("enterprise")
    except T.UnknownTier as e:
        assert "enterprise" in str(e)
        return
    raise AssertionError("an unknown tier name resolved to something instead of refusing")


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
    _with_env(None)
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
