"""#156 — TokenVault + sign-in scopes: one sign-in vaults a multi-resource refresh
token; login requests the Azure SQL delegated scope when the tenant app (AUTH_*) is
configured; creds resolve AUTH_* first, SP_CONNECTOR_* fallback.

Run: PYTHONPATH=src python3 tests/selftest_user_auth_vault.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.user_auth import NotSignedIn, TokenVault  # noqa: E402


def test_vault_is_multi_idp():
    v = TokenVault()
    v.put("alice@gmail.com", "rt-entra")                   # legacy 2-arg call -> entra
    v.put("alice@gmail.com", "rt-google", idp="google")
    assert v.get("alice@gmail.com") == "rt-entra"          # legacy 1-arg get -> entra
    assert v.get("alice@gmail.com", idp="google") == "rt-google"
    assert sorted(v.linked("alice@gmail.com")) == ["entra", "google"]


def test_unlinked_idp_fails_closed_and_names_the_idp():
    v = TokenVault()
    v.put("bob@gmail.com", "rt-entra")
    try:
        v.get("bob@gmail.com", idp="google")
    except NotSignedIn as e:
        assert e.idp == "google"
        assert "Google" in str(e)
    else:
        raise AssertionError("unlinked google must fail closed")


def test_drop_one_idp_leaves_the_other():
    v = TokenVault()
    v.put("carol@gmail.com", "rt-entra")
    v.put("carol@gmail.com", "rt-google", idp="google")
    v.drop("carol@gmail.com", idp="google")
    assert v.get("carol@gmail.com") == "rt-entra"
    assert v.linked("carol@gmail.com") == ["entra"]
    v.drop("carol@gmail.com")                              # no idp -> drop ALL (logout)
    assert v.linked("carol@gmail.com") == []


def test_vault_put_get_drop_and_fail_closed():
    v = TokenVault()
    try:
        v.get("alice")
        raise AssertionError("expected NotSignedIn")
    except NotSignedIn as e:
        assert "sign in" in str(e).lower()
    v.put("alice", "rt-1")
    assert v.get("alice") == "rt-1"
    v.put("alice", "rt-2")           # rotation overwrites
    assert v.get("alice") == "rt-2"
    v.drop("alice")
    try:
        v.get("alice")
        raise AssertionError("expected NotSignedIn after drop")
    except NotSignedIn:
        pass


def test_scopes_and_authority_follow_auth_env():
    try:
        for k in ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET"):
            os.environ.pop(k, None)
        os.environ["SP_CONNECTOR_CLIENT_ID"] = "sp-cid"
        os.environ["SP_CONNECTOR_CLIENT_SECRET"] = "sp-sec"
        # without AUTH_*: SP app, /organizations, no data scopes
        assert user_auth.client_id() == "sp-cid"
        assert user_auth.data_scopes() == ""
        url = user_auth.login_url("st")
        assert "/organizations/" in url and "database.windows.net" not in url
        # with AUTH_*: tenant app, tenant authority, offline_access at sign-in — but the DB
        # resource scope moved to the INCREMENTAL round (#429): asking for it at sign-in
        # 650052'd every org without an Azure SQL service principal.
        os.environ["AUTH_TENANT_ID"] = "tid-123"
        os.environ["AUTH_CLIENT_ID"] = "auth-cid"
        os.environ["AUTH_CLIENT_SECRET"] = "auth-sec"
        assert user_auth.client_id() == "auth-cid"
        assert "offline_access" in user_auth.data_scopes()
        url = user_auth.login_url("st")
        assert "/tid-123/" in url
        assert "offline_access" in url
        assert "database.windows.net" not in url, "sign-in must not request the DB resource"
        assert "https%3A%2F%2Fdatabase.windows.net%2Fuser_impersonation" in \
            user_auth.db_consent_url("st"), "the DB round must still request it"
    finally:
        for k in ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET",
                  "SP_CONNECTOR_CLIENT_ID", "SP_CONNECTOR_CLIENT_SECRET"):
            os.environ.pop(k, None)


def test_exchange_code_returns_refresh_token():
    def fake_post(url, form):
        idt_payload = ("eyJhbGciOiJub25lIn0."           # {"alg":"none"}
                       "eyJvaWQiOiJvLTEiLCJ0aWQiOiJ0LTEiLCJuYW1lIjoiQWxpY2UifQ."
                       "")                               # {"oid":"o-1","tid":"t-1","name":"Alice"}
        return {"id_token": idt_payload, "refresh_token": "rt-live",
                "access_token": "at-db"}

    u = user_auth.exchange_code("code", post=fake_post)
    assert u["oid"] == "o-1" and u["refresh_token"] == "rt-live"


def test_partial_auth_config_degrades_to_sp_connector():
    """Partial AUTH_* config (missing AUTH_CLIENT_SECRET) falls back cleanly to
    SP connector app — no hybrid identity mixing."""
    try:
        for k in ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET",
                  "SP_CONNECTOR_CLIENT_ID", "SP_CONNECTOR_CLIENT_SECRET"):
            os.environ.pop(k, None)
        os.environ["SP_CONNECTOR_CLIENT_ID"] = "sp-cid-fallback"
        os.environ["SP_CONNECTOR_CLIENT_SECRET"] = "sp-sec-fallback"
        # Partial: AUTH_CLIENT_ID set, but AUTH_CLIENT_SECRET and AUTH_TENANT_ID missing
        os.environ["AUTH_CLIENT_ID"] = "auth-cid-partial"
        assert user_auth.client_id() == "sp-cid-fallback", "partial AUTH_* should fall back to SP client_id"
        assert user_auth.client_secret() == "sp-sec-fallback", "partial AUTH_* should fall back to SP client_secret"
        assert user_auth.data_scopes() == "", "partial AUTH_* should have no data scopes"
        url = user_auth.login_url("st")
        assert "/organizations/" in url, "partial AUTH_* should use /organizations authority"
        assert "database.windows.net" not in url, "partial AUTH_* should not include DB scope"
    finally:
        for k in ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET",
                  "SP_CONNECTOR_CLIENT_ID", "SP_CONNECTOR_CLIENT_SECRET"):
            os.environ.pop(k, None)


def main():
    test_vault_put_get_drop_and_fail_closed()
    print("  PASS  TokenVault: put/get/drop, rotation overwrite, NotSignedIn fail-closed")
    test_scopes_and_authority_follow_auth_env()
    print("  PASS  creds/authority/scopes: AUTH_* tenant app first, SP_CONNECTOR_* "
          "fallback; DB scope only when tenant app configured")
    test_partial_auth_config_degrades_to_sp_connector()
    print("  PASS  partial AUTH_* config: missing AUTH_CLIENT_SECRET cleanly falls back to "
          "SP connector (no hybrid identity)")
    test_exchange_code_returns_refresh_token()
    print("  PASS  exchange_code returns refresh_token alongside verified claims")
    test_vault_is_multi_idp()
    print("  PASS  TokenVault is multi-IdP: (oid, idp) keyed, legacy 1/2-arg calls default to entra")
    test_unlinked_idp_fails_closed_and_names_the_idp()
    print("  PASS  unlinked idp fails closed and names the idp on NotSignedIn")
    test_drop_one_idp_leaves_the_other()
    print("  PASS  drop(idp=...) drops one cloud; drop() with no idp drops all (logout)")
    print("\n#156 USER-AUTH VAULT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
