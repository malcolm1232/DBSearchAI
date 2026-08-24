"""#429: a foreign org must be able to SIGN IN without already having an Azure SQL
service principal in its tenant.

The bug this pins down was found by the #423 foreign-tenant proof (260730). Base sign-in
requested `https://database.windows.net/user_impersonation`, so Microsoft rejected the whole
login with AADSTS650052 ("your organization lacks a service principal for Azure SQL
Database") BEFORE /auth/callback ever saw a code. Most orgs have never touched Azure SQL, so
the product was unreachable for them - sign-in is not the moment to ask for data-plane
consent.

The rule: identity scopes at sign-in, resource scopes when the user connects that resource
(the fewest-steps connector objective - nobody consents to a database they haven't added).

    PYTHONPATH=src python3 tests/selftest_incremental_db_consent.py
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from dbsearch.server import user_auth

_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_MULTI_TENANT")
TENANT = {"AUTH_TENANT_ID": "tid-1", "AUTH_CLIENT_ID": "cid", "AUTH_CLIENT_SECRET": "sec"}
MULTI = {**TENANT, "DBSEARCH_MULTI_TENANT": "1"}


def _with(env, fn):
    old = {k: os.environ.get(k) for k in _VARS}
    try:
        for k in _VARS:
            os.environ.pop(k, None)
        os.environ.update(env)
        return fn()
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_base_signin_does_not_request_the_db_resource():
    """THE #429 REGRESSION. A tenant with no Azure SQL SP 650052s on this scope alone."""
    url = _with(MULTI, lambda: user_auth.login_url("st"))
    assert "user_impersonation" not in url, f"DB resource scope leaked into sign-in: {url}"
    assert "database.windows.net" not in url, f"DB resource leaked into sign-in: {url}"


def test_base_signin_still_asks_for_identity_and_a_refresh_token():
    """offline_access must stay: the vault needs a refresh token to redeem later, and
    offline_access needs no resource service principal, so it is safe for any org."""
    url = _with(MULTI, lambda: user_auth.login_url("st"))
    for want in ("openid", "profile", "email", "offline_access"):
        assert want in url, f"{want} missing from sign-in scope: {url}"


def test_db_consent_url_requests_the_db_resource():
    """The incremental round - used when the user actually connects an Azure SQL-family
    node. This is where 650052 is ALLOWED to happen, and it is actionable there because the
    user is mid-connect on a database they chose."""
    url = _with(MULTI, lambda: user_auth.db_consent_url("st"))
    assert "user_impersonation" in url, f"DB scope missing from the consent round: {url}"
    assert "offline_access" in url, url


def test_db_consent_round_is_reachable_only_with_a_tenant_app():
    """No tenant app means no consented DB delegation to ask for (data_scopes() is '' today
    for the same reason) - the DB round must not pretend otherwise."""
    assert _with({}, user_auth.db_consent_scopes) == ""


def test_data_scopes_still_carries_the_db_resource_for_the_broker():
    """The broker's token redemption still needs the resource scope - the fix moves WHEN it
    is requested, not whether the product can delegate at all (that would regress #156)."""
    s = _with(MULTI, user_auth.data_scopes)
    assert "user_impersonation" in s, s



# ---- broker: an un-consented DB resource must be actionable, not an AADSTS code ----------
from dbsearch.router.identity_broker import TokenExchangeError


def test_missing_consent_is_not_mislabelled_as_an_expired_signin():
    """Entra returns `invalid_grant` for BOTH a dead refresh token and a resource the tenant
    never consented to. If consent loses this tie-break the user is told to sign in again,
    does so successfully, and hits the same wall forever."""
    e = TokenExchangeError(400, "invalid_grant",
                           "AADSTS65001: The user or administrator has not consented to use "
                           "the application with ID '...'.")
    assert e.needs_db_consent is True
    assert e.expired_grant is False


def test_650052_no_service_principal_is_a_consent_problem():
    """The exact #423-proof failure, now surfacing at connect time where it is fixable."""
    e = TokenExchangeError(400, "invalid_client",
                           "AADSTS650052: The app is trying to access a service "
                           "'022907d3-0f1b-48f7-badc-1ba6abab6d66'(Azure SQL Database) that "
                           "your organization lacks a service principal for.")
    assert e.needs_db_consent is True


def test_a_genuinely_dead_grant_still_says_sign_in_again():
    e = TokenExchangeError(400, "invalid_grant",
                           "AADSTS700082: The refresh token has expired due to inactivity.")
    assert e.expired_grant is True
    assert e.needs_db_consent is False


def test_broker_rewrites_missing_consent_into_plain_language():
    from dbsearch.router.identity_broker import EntraRefreshExchange

    def boom(url, form):
        raise TokenExchangeError(400, "invalid_grant",
                                 "AADSTS65001: The user or administrator has not consented.")

    b = EntraRefreshExchange("t", "c", "s", lambda oid: "rt", post=boom)
    try:
        b._exchange("o-1", "https://database.windows.net")
        raise AssertionError("expected the exchange to raise")
    except TokenExchangeError as e:
        assert "organization hasn't approved database access" in str(e), str(e)
        assert "AADSTS65001" in str(e), "the IdP's own reason must still be carried"

if __name__ == "__main__":
    test_base_signin_does_not_request_the_db_resource()
    test_base_signin_still_asks_for_identity_and_a_refresh_token()
    test_db_consent_url_requests_the_db_resource()
    test_db_consent_round_is_reachable_only_with_a_tenant_app()
    test_data_scopes_still_carries_the_db_resource_for_the_broker()
    test_missing_consent_is_not_mislabelled_as_an_expired_signin()
    test_650052_no_service_principal_is_a_consent_problem()
    test_a_genuinely_dead_grant_still_says_sign_in_again()
    test_broker_rewrites_missing_consent_into_plain_language()
    print("OK selftest_incremental_db_consent")
