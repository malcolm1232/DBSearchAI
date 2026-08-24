"""ADR 0011: _authority() returns /organizations when DBSEARCH_MULTI_TENANT=1 and the
tenant app is configured - so the tenant app's DB delegation scopes survive the flip.

    PYTHONPATH=src python3 tests/selftest_multi_tenant_authority.py
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from dbsearch.server import user_auth

_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_MULTI_TENANT")


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


TENANT = {"AUTH_TENANT_ID": "tid-1", "AUTH_CLIENT_ID": "cid", "AUTH_CLIENT_SECRET": "sec"}

def test_single_tenant_unchanged():
    a = _with(TENANT, user_auth._authority)
    assert a.endswith("/tid-1"), a

def test_flag_switches_to_organizations():
    a = _with({**TENANT, "DBSEARCH_MULTI_TENANT": "1"}, user_auth._authority)
    assert a.endswith("/organizations"), a
    # scopes must SURVIVE: this is the whole point of flipping the tenant app itself
    s = _with({**TENANT, "DBSEARCH_MULTI_TENANT": "1"}, user_auth.data_scopes)
    assert "user_impersonation" in s, s

def test_baseline_without_flag_or_tenant_app_is_organizations():
    """The fourth cell of the table, and the only one that was untested: flag UNSET and no
    tenant app. It must stay on today's unconfigured fallback (/organizations, the SP
    connector app), or an unconfigured box would start pointing sign-in at a tenant it has
    no client for - and the flag would look load-bearing where it is not."""
    a = _with({}, user_auth._authority)
    assert a.endswith("/organizations"), a

def test_flag_without_tenant_app_is_todayfallback():
    a = _with({"DBSEARCH_MULTI_TENANT": "1"}, user_auth._authority)
    assert a.endswith("/organizations"), a   # same as today's unconfigured fallback

if __name__ == "__main__":
    test_single_tenant_unchanged()
    test_flag_switches_to_organizations()
    test_baseline_without_flag_or_tenant_app_is_organizations()
    test_flag_without_tenant_app_is_todayfallback()
    print("OK selftest_multi_tenant_authority")
