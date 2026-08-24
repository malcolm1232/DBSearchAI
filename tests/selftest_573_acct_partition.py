"""#573 - a verified session without an Entra tid gets a PRIVATE per-account partition
instead of the fail-closed "" that made Google/local logins decorative (ADR 0018).

    PYTHONPATH=src python3 tests/selftest_573_acct_partition.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from dbsearch.api.auth import ACCT_TENANT_PREFIX, resolve_tenant  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402

_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET")


def _real_login(on: bool):
    for k in _VARS:
        os.environ.pop(k, None)
    if on:
        os.environ.update({"AUTH_TENANT_ID": "tid-home", "AUTH_CLIENT_ID": "cid",
                           "AUTH_CLIENT_SECRET": "sec"})


def _cookie_getter(payload: dict):
    tok = user_auth.sign_session({**payload, "exp": int(time.time()) + 3600})
    return lambda name: tok if name == user_auth.COOKIE else None


def _no_header(name):
    return None


def test_a_session_with_no_tid_partitions_to_its_own_account():
    _real_login(True)
    got = resolve_tenant(_no_header, _cookie_getter({"oid": "acct_x1", "tid": ""}),
                         default_tenant="deployment-const")
    assert got == ACCT_TENANT_PREFIX + "acct_x1", got


def test_home_tenant_sessions_are_unchanged():
    _real_login(True)
    got = resolve_tenant(_no_header, _cookie_getter({"oid": "oid-1", "tid": "tid-home"}),
                         default_tenant="deployment-const")
    assert got == "deployment-const", got


def test_foreign_tenant_sessions_are_unchanged():
    _real_login(True)
    got = resolve_tenant(_no_header, _cookie_getter({"oid": "oid-2", "tid": "tid-foreign"}),
                         default_tenant="deployment-const")
    assert got == "tid-foreign", got


def test_anonymous_still_fails_closed():
    _real_login(True)
    assert resolve_tenant(_no_header, lambda n: None, default_tenant="deployment-const") == ""


def test_two_accounts_get_two_partitions():
    _real_login(True)
    a = resolve_tenant(_no_header, _cookie_getter({"oid": "acct_a", "tid": ""}), "d")
    b = resolve_tenant(_no_header, _cookie_getter({"oid": "acct_b", "tid": ""}), "d")
    assert a != b and a and b


def test_a_session_with_neither_tid_nor_oid_fails_closed():
    """The one path that still reaches the final `return ""` in resolve_tenant: a REAL
    signed session (not `_no_header`/no-cookie-at-all, which is a different branch -
    see test_anonymous_still_fails_closed) that carries no oid and no tid. There is no
    account to key a private partition off of, so ADR 0018 does not apply and the old
    fail-closed floor still holds."""
    _real_login(True)
    got = resolve_tenant(_no_header, _cookie_getter({"tid": ""}),
                         default_tenant="deployment-const")
    assert got == "", got


def test_a_no_tid_session_can_now_upload_and_only_it_can_read_back():
    """The point of the ADR: upload works for a non-Entra account, stays isolated."""
    import io, json
    from fastapi.testclient import TestClient
    from dbsearch.server.app import app
    client = TestClient(app)
    _real_login(True)
    body = b"Vendor payment terms are net 45 days for all F&N suppliers."

    def cookies(oid, tid):
        return {user_auth.COOKIE: user_auth.sign_session(
            {"oid": oid, "tid": tid, "exp": int(time.time()) + 3600})}

    r = client.post("/admin/upload", cookies=cookies("acct_g1", ""),
                    data={"title": "terms.txt"},
                    files={"file": ("terms.txt", io.BytesIO(body), "text/plain")})
    assert r.status_code == 202, f"non-Entra upload still refused: {r.status_code} {r.text[:200]}"
    from _upload import settle
    job = settle(client, r, cookies=cookies("acct_g1", ""))
    assert job["status"] == "succeeded", job

    mine = json.dumps(client.post("/search", cookies=cookies("acct_g1", ""),
                                  json={"question": "vendor payment terms"}).json())
    assert "net 45" in mine or "45" in mine, f"uploader cannot read their own doc: {mine[:300]}"

    other = json.dumps(client.post("/search", cookies=cookies("acct_g2", ""),
                                   json={"question": "vendor payment terms"}).json())
    assert "net 45" not in other, "acct partition leaked across accounts"

    home = json.dumps(client.post("/search", cookies=cookies("oid-home-user", "tid-home"),
                                  json={"question": "vendor payment terms"}).json())
    assert "net 45" not in home, "acct partition leaked into the home tenant corpus"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
    _real_login(False)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
