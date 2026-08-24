"""#156 — wiring: sign-in callback vaults the refresh token; compose hands the
broker a vault-backed subject provider (env seam only when login disabled);
dev-seed endpoint is OFF by default.

Run: PYTHONPATH=src python3 tests/selftest_signin_vault_wiring.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import app, _subject_provider  # noqa: E402


def test_subject_provider_prefers_vault_when_login_enabled():
    # is_enabled() needs all three AUTH_* vars (_tenant_app_configured, per Task 2's
    # selftest_user_auth_vault.py) — AUTH_CLIENT_ID/SECRET alone fall back to the
    # (unset) SP_CONNECTOR_* pair and stay disabled.
    os.environ["AUTH_TENANT_ID"] = "tid"
    os.environ["AUTH_CLIENT_ID"] = "cid"
    os.environ["AUTH_CLIENT_SECRET"] = "sec"
    user_auth.VAULT.put("o-77", "rt-77")
    assert _subject_provider("o-77") == "rt-77"
    try:
        _subject_provider("o-unknown")
        raise AssertionError("expected NotSignedIn")
    except user_auth.NotSignedIn:
        pass
    # login disabled -> env seam
    for k in ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET",
              "SP_CONNECTOR_CLIENT_ID", "SP_CONNECTOR_CLIENT_SECRET"):
        os.environ.pop(k, None)
    os.environ["DBSEARCH_SUBJECT_TOKEN"] = "env-tok"
    assert _subject_provider("anyone") == "env-tok"
    os.environ.pop("DBSEARCH_SUBJECT_TOKEN", None)


def test_dev_seed_is_gated_and_seeds_vault_with_cookie():
    c = TestClient(app)
    os.environ.pop("DBSEARCH_DEV_SEED", None)
    r = c.post("/auth/dev/seed", json={"oid": "o-9", "name": "T",
                                       "refresh_token": "rt-9"})
    assert r.status_code == 404          # hidden when off
    os.environ["DBSEARCH_DEV_SEED"] = "1"
    r = c.post("/auth/dev/seed", json={"oid": "o-9", "name": "T",
                                       "refresh_token": "rt-9"})
    assert r.status_code == 200
    assert user_auth.VAULT.get("o-9") == "rt-9"
    sess = user_auth.read_session(r.cookies.get(user_auth.COOKIE, ""))
    assert sess and sess["oid"] == "o-9"
    os.environ.pop("DBSEARCH_DEV_SEED", None)


def main():
    test_subject_provider_prefers_vault_when_login_enabled()
    print("  PASS  subject provider: vault when login enabled (NotSignedIn "
          "fail-closed), env seam when disabled")
    test_dev_seed_is_gated_and_seeds_vault_with_cookie()
    test_the_app_actually_binds_a_durable_store_to_the_vault()
    print("  PASS  /auth/dev/seed: 404 unless DBSEARCH_DEV_SEED=1; seeds vault + "
          "signed session cookie")
    print("\n#156 SIGN-IN WIRING SELF-TEST PASSED.")



def test_the_app_actually_binds_a_durable_store_to_the_vault():
    """#435 anti-inert check. The vault only persists if app.py hands it the secrets store; the
    code can be perfectly correct and still ship as a memory-only vault if that one line is
    missing or lands before `_secrets` exists. That failure is invisible until a deploy silently
    logs everyone out - exactly the shape of the #420 chimera, where a feature was verified in
    isolation and shipped inert.

    Asserted against the imported app: bound is not None when a secrets store was configurable,
    and the reason is recorded when it was not (no key -> memory-only is legitimate)."""
    from dbsearch.server.app import _secrets, _secrets_unavailable_reason

    bound = user_auth.VAULT._store
    if _secrets is not None:
        assert bound is _secrets, (
            "VAULT is not bound to the app's secrets store - it will silently stay memory-only "
            "and every deploy will log every user out of data access")
    else:
        assert bound is None and _secrets_unavailable_reason, (
            "no secrets store, so the vault must be memory-only AND the reason recorded")
    print("  PASS  #435: vault durability is wired to the app's secrets store"
          f" (bound={bound is not None})")

if __name__ == "__main__":
    main()
