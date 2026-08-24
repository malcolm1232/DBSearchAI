"""#775 - the storage and upgrade section of the account panel.

Two failures are worth more than the feature itself, and both are about NOT drawing things:

  * an Upgrade button on a deployment that cannot take a payment. A self-hosted box is free
    forever (ADR 0027 rule 6) and a hosted one before its live keys land cannot charge
    anybody, so offering to sell them something is #551's always-fails tile with a card
    number attached.
  * a usage bar drawn at 0% when the backend cannot meter. `used_bytes: null` means "cannot
    say", and painting an empty bar tells somebody who might be full that they have all their
    space left. Unknown is not zero - the same rule corpus_status already follows.

    PYTHONPATH=src python3 tests/selftest_775_account_storage_dom.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402

ACCOUNT = ROOT / "src/dbsearch/server/static/js/ui/account.js"
PROBE = ROOT / "tests/account_storage_dom_probe.mjs"

_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the #775 storage panel check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(ACCOUNT), scenario],
                f"the account storage panel ({scenario})")
    return _domgate.resolve(_dom[scenario])


def test_a_sellable_deployment_shows_usage_and_offers_the_other_tiers():
    r = _report("sellable")
    if r is None:
        return
    assert r["present"], "the storage section is missing on a deployment that can sell"
    assert r["hasBar"] and r["barPct"] == "30", r          # 3GB of 10GB
    assert "3.0 GB" in r["line"] and "10.0 GB" in r["line"], r["line"]
    assert not r["barFull"], "the bar is amber at 30% used, so the colour means nothing"
    joined = " | ".join(r["buttons"]).lower()
    assert "upgrade to plus" in joined and "upgrade to pro" in joined, r["buttons"]
    # Displayed capitalised: a raw config value in a sentence reads as a leak, not as copy.
    raw = " | ".join(r["buttons"])
    assert "Plus" in raw and "Pro" in raw, f"tier names not capitalised for display: {raw}"
    assert "50 gb" in joined and "$0.99" in joined, (
        f"the offer does not say what it buys or what it costs: {r['buttons']}")


def test_a_self_hosted_deployment_is_never_asked_for_money():
    """ADR 0027 rule 6. It may still SEE its usage - that is a fact about its own disk."""
    r = _report("self_host")
    if r is None:
        return
    assert r["hasBar"], "a metered self-host cannot see its own usage"
    assert r["buttons"] == [], f"a deployment that sells nothing offered: {r['buttons']}"


def test_an_unmeterable_backend_draws_no_bar_at_all():
    r = _report("unmetered")
    if r is None:
        return
    assert not r["hasBar"], (
        "a usage bar was drawn for a backend that cannot meter; an empty bar tells an "
        "account that might be full that it has all its space left")
    assert r["line"] is None, r["line"]
    assert any("upgrade" in b.lower() for b in r["buttons"]), r["buttons"]


def test_the_top_tier_is_offered_the_portal_and_never_its_own_plan():
    r = _report("already_pro")
    if r is None:
        return
    joined = " | ".join(r["buttons"]).lower()
    assert "manage billing" in joined, r["buttons"]
    assert "upgrade to pro" not in joined, (
        f"a pro subscriber is being offered pro again: {r['buttons']}")
    assert r["barFull"], (
        "at 980GB of 1024GB (95.7%) the bar is not amber, so the 90% 'act soon' line is not "
        "where it is supposed to be")
    assert r["plan"] and "pro" in r["plan"].lower(), r["plan"]


def test_a_failed_billing_lookup_does_not_take_the_panel_down():
    """The panel's real job is the connected-sources roster. A missing storage row is a much
    smaller failure than an error where the roster should be."""
    r = _report("billing_down")
    if r is None:
        return
    assert not r["present"], "a broken billing lookup still drew a storage section"
    assert r["rosterRows"] >= 3, (
        f"the provider roster did not survive a billing failure: {r['rosterRows']} rows")


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
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
