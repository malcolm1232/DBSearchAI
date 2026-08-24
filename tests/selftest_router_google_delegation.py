"""#193 -- google_refresh delegation: the vaulted Google refresh token is redeemed for an
access token, cached per (user, resource), and the broker hands it to the store as the
delegated credential. A 2-arg subject provider is bound to the RIGHT idp per kind, so an
entra store and a google store on the same session each get their own cloud's credential.

Run: PYTHONPATH=src python3 tests/selftest_router_google_delegation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.identity_broker import (  # noqa: E402
    GoogleRefreshExchange, IdentityBroker, exchange_from_config,
)


class FakeIdentity:
    def expand_groups(self, oid):
        return [oid]


def test_google_refresh_redeems_and_caches():
    calls = []

    def fake_post(url, form):
        calls.append((url, form))
        return {"access_token": "at-1", "expires_in": 3600}

    ex = GoogleRefreshExchange("cid", "csec", lambda oid: "rt-" + oid, post=fake_post)
    assert ex.exchange("alice@gmail.com", "bigquery") == "at-1"
    assert ex.exchange("alice@gmail.com", "bigquery") == "at-1"   # cached, no 2nd POST
    assert len(calls) == 1
    url, form = calls[0]
    assert url == "https://oauth2.googleapis.com/token"
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "rt-alice@gmail.com"
    assert form["client_id"] == "cid" and form["client_secret"] == "csec"


def test_google_refresh_surfaces_error():
    def fake_post(url, form):
        return {"error": "invalid_grant", "error_description": "token expired or revoked"}
    ex = GoogleRefreshExchange("cid", "csec", lambda oid: "rt", post=fake_post)
    try:
        ex.exchange("alice@gmail.com", "bigquery")
    except RuntimeError as e:
        assert "revoked" in str(e)
    else:
        raise AssertionError("a failed redemption must raise, never yield a bad token")


def test_config_binds_each_kind_to_its_own_idp():
    """The subject provider is (oid, idp) -> token. A google_refresh block must ask the
    vault for the GOOGLE credential, an entra_refresh block for the ENTRA one."""
    asked = []

    def subject(oid, idp="entra"):
        asked.append((oid, idp))
        return "rt-" + idp

    posts = []

    def fake_post(url, form):
        posts.append(form)
        return {"access_token": "at", "expires_in": 60}

    g, res = exchange_from_config(
        {"kind": "google_refresh", "client_id": "cid", "client_secret": "csec"}, subject)
    assert res == "bigquery"
    g._post = fake_post
    g.exchange("alice@gmail.com", "bigquery")
    assert asked[-1] == ("alice@gmail.com", "google")
    assert posts[-1]["refresh_token"] == "rt-google"

    e, _res = exchange_from_config(
        {"kind": "entra_refresh", "tenant_id": "t", "client_id": "c",
         "client_secret": "s"}, subject)
    e._post = fake_post
    e.exchange("alice@gmail.com", "https://database.windows.net")
    assert asked[-1] == ("alice@gmail.com", "entra")
    assert posts[-1]["refresh_token"] == "rt-entra"


def test_legacy_one_arg_provider_still_works_for_entra_kinds():
    """The env dev seam (env_subject_token_provider) takes ONE arg and hands back an ENTRA
    assertion. Binding must not break it for the kinds that federate FROM Entra."""
    for block in ({"kind": "entra_refresh", "tenant_id": "t", "client_id": "c",
                   "client_secret": "s"},
                  {"kind": "entra_obo", "tenant_id": "t", "client_id": "c",
                   "client_secret": "s"},
                  {"kind": "gcp_wif", "audience": "//iam.googleapis.com/aud"},
                  {"kind": "aws_sts", "role_arn": "arn:aws:iam::1:role/r"}):
        exchange_from_config(block, lambda oid: "rt-legacy")     # must not raise
    ex, _ = exchange_from_config(
        {"kind": "entra_refresh", "tenant_id": "t", "client_id": "c", "client_secret": "s"},
        lambda oid: "rt-legacy")
    ex._post = lambda url, form: {"access_token": "at-" + form["refresh_token"],
                                  "expires_in": 60}
    assert ex.exchange("alice@gmail.com", "https://database.windows.net") == "at-rt-legacy"
    # `static` consults no subject provider at all - any shape composes
    ex2, _ = exchange_from_config({"kind": "static", "tokens": {"alice": "tok-a"}},
                                  lambda oid: "rt-legacy")
    assert ex2.exchange("alice", "dev") == "tok-a"


def test_legacy_one_arg_provider_is_refused_for_a_non_entra_cloud():
    """The refusal lives at the BINDING site (_for_idp), not at one caller: the 1-arg seam is
    an ENTRA seam, so binding it to google_refresh would POST an Entra assertion to Google's
    token endpoint - one cloud's credential handed to another cloud (LAW 2). exchange_from_config
    is public API, so any caller (not just register_delegations) inherits the refusal."""
    try:
        exchange_from_config(
            {"kind": "google_refresh", "client_id": "cid", "client_secret": "csec"},
            lambda oid: "rt-legacy")
    except ValueError as e:
        assert "google_refresh" in str(e) and "idp" in str(e), e
        assert "ENTRA seam" in str(e), e
    else:
        raise AssertionError(
            "a legacy 1-arg (entra) provider must never be bound to a google delegation")


def test_broker_hands_google_credential_to_the_store():
    broker = IdentityBroker(FakeIdentity())
    ex, res = exchange_from_config(
        {"kind": "google_refresh", "client_id": "cid", "client_secret": "csec"},
        lambda oid, idp="entra": "rt")
    ex._post = lambda url, form: {"access_token": "at-bq", "expires_in": 60}
    broker.register_delegation("gcp-sales", ex, res)
    ctx = broker.access_for("alice@gmail.com", "gcp-sales")
    assert ctx.delegated_credential == "at-bq"
    assert ctx.row_policy is None      # delegation wins; we never inject a predicate


def test_unknown_kind_still_raises():
    try:
        exchange_from_config({"kind": "gcp_typo"}, lambda oid: "x")
    except ValueError as e:
        assert "google_refresh" in str(e)   # the known-kinds list must mention it
    else:
        raise AssertionError("a typo must never silently mean 'no delegation'")


def test_ambiguous_two_arg_provider_raises_not_guesses():
    """A provider whose 2nd parameter is NOT named `idp` (e.g. `request=None`) must never
    be arity-matched into the multi-idp slot: that would pass "google" positionally into
    an unrelated parameter and the provider would silently hand back whatever credential
    it always hands back (e.g. the Entra one) - now mislabeled as the google credential and
    POSTed to Google's token endpoint. Refuse instead of guessing (LAW 2)."""
    def ambiguous(oid, request=None):
        return "some-token"

    try:
        exchange_from_config(
            {"kind": "google_refresh", "client_id": "cid", "client_secret": "csec"},
            ambiguous)
    except ValueError as e:
        assert "no recognizable shape" in str(e)
    else:
        raise AssertionError(
            "an ambiguous 2-arg provider must raise, never silently bind entra/google")


def test_varargs_shim_raises_not_falls_back():
    """A `lambda *a: provider(*a)` shim has exactly one Parameter object (`*a`), so a naive
    arity/len(params) check mis-reads it as the legacy 1-arg seam and silently falls back
    to it (skipping idp binding entirely). It must raise instead."""
    def real_provider(oid, idp="entra"):
        return "rt-" + idp

    shim = lambda *a: real_provider(*a)  # noqa: E731

    try:
        exchange_from_config(
            {"kind": "google_refresh", "client_id": "cid", "client_secret": "csec"}, shim)
    except ValueError as e:
        assert "no recognizable shape" in str(e)
    else:
        raise AssertionError("a varargs shim must raise, never silently fall back to entra")


def test_google_refresh_no_expires_in_does_not_cache():
    """A malformed token response with no expires_in must not buy cache time - matching
    EntraRefreshExchange's fail-safe default of 0, not a full hour."""
    calls = []

    def fake_post(url, form):
        calls.append((url, form))
        return {"access_token": "at-1"}   # no expires_in

    ex = GoogleRefreshExchange("cid", "csec", lambda oid: "rt", post=fake_post)
    assert ex.exchange("alice@gmail.com", "bigquery") == "at-1"
    assert ex.exchange("alice@gmail.com", "bigquery") == "at-1"
    assert len(calls) == 2   # not cached -> a second exchange() means a second POST


for fn in [test_google_refresh_redeems_and_caches, test_google_refresh_surfaces_error,
           test_config_binds_each_kind_to_its_own_idp,
           test_legacy_one_arg_provider_still_works_for_entra_kinds,
           test_legacy_one_arg_provider_is_refused_for_a_non_entra_cloud,
           test_broker_hands_google_credential_to_the_store, test_unknown_kind_still_raises,
           test_ambiguous_two_arg_provider_raises_not_guesses,
           test_varargs_shim_raises_not_falls_back,
           test_google_refresh_no_expires_in_does_not_cache]:
    fn()
