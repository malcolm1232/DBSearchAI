"""#183 (regression for #156 Task 3) — dev-header identity must be refused whenever
real Entra login is enabled. Without this coupling, `current_user` falls through
to the `X-DBSearch-User` dev header even when `user_auth.is_enabled()` is true,
letting an attacker with no session cookie impersonate any oid and redeem that
victim's vaulted Entra refresh token via `_subject_provider` -> `VAULT.get(oid)`.

Fail-closed (LAW 2): when real login is configured, only a valid signed session
cookie authenticates a request; anything else is 401. The dev-header path stays
intact when real login is NOT configured (self-host/dev must keep working).

    PYTHONPATH=src python3 tests/selftest_devheader_login_exclusion.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi import HTTPException  # noqa: E402

from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import current_user  # noqa: E402

_AUTH_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET")


class FakeRequest:
    """Minimal stand-in for fastapi.Request exposing only what current_user reads."""

    def __init__(self, cookies: dict | None = None, headers: dict | None = None,
                 query_params: dict | None = None):
        self.cookies = cookies or {}
        self.headers = headers or {}
        # current_user reads `?demo=` as a shareable-link demo selector (#279).
        self.query_params = query_params or {}


def _enable_real_login():
    os.environ["AUTH_TENANT_ID"] = "tid"
    os.environ["AUTH_CLIENT_ID"] = "cid"
    os.environ["AUTH_CLIENT_SECRET"] = "sec"


def _clear_auth_vars():
    for k in _AUTH_VARS:
        os.environ.pop(k, None)


def test_dev_header_refused_when_login_enabled():
    """The vulnerability: is_enabled() TRUE + no cookie + dev header claiming a
    victim's oid must NOT authenticate as that victim. Must raise HTTPException 401."""
    saved = {k: os.environ.get(k) for k in _AUTH_VARS}
    try:
        _enable_real_login()
        assert user_auth.is_enabled(), "test setup failed: is_enabled() should be True"
        req = FakeRequest(cookies={}, headers={"x-dbsearch-user": "victim-oid"})
        try:
            result = current_user(req)
        except HTTPException as e:
            assert e.status_code == 401, f"expected 401, got {e.status_code}"
        else:
            raise AssertionError(
                f"VULNERABLE: current_user returned {result!r} from the dev header "
                "while real login was enabled and no session cookie was present")
    finally:
        _clear_auth_vars()
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_valid_cookie_wins_over_conflicting_header_when_login_enabled():
    """is_enabled() TRUE + valid session cookie for real-oid + a conflicting dev
    header -> cookie wins, header is ignored entirely."""
    saved = {k: os.environ.get(k) for k in _AUTH_VARS}
    try:
        _enable_real_login()
        assert user_auth.is_enabled(), "test setup failed: is_enabled() should be True"
        token = user_auth.sign_session({"oid": "real-oid", "exp": int(time.time()) + 3600})
        req = FakeRequest(
            cookies={user_auth.COOKIE: token},
            headers={"x-dbsearch-user": "someone-else"},
        )
        result = current_user(req)
        assert result == "real-oid", f"expected 'real-oid' (cookie), got {result!r}"
    finally:
        _clear_auth_vars()
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_dev_header_preserved_when_login_disabled():
    """is_enabled() FALSE (no AUTH_* vars) -> dev header path unchanged, so
    self-host/local development keeps working exactly as today."""
    saved = {k: os.environ.get(k) for k in _AUTH_VARS}
    try:
        _clear_auth_vars()
        assert not user_auth.is_enabled(), "test setup failed: is_enabled() should be False"
        req = FakeRequest(cookies={}, headers={"x-dbsearch-user": "dev-user"})
        result = current_user(req)
        assert result == "dev-user", f"expected 'dev-user' (dev path), got {result!r}"
    finally:
        _clear_auth_vars()
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


_GQL = '{ search(question: "anything") { authorizedDocs } }'


def _graphql(client, headers=None, session=""):
    """POST the query to /graphql. The session cookie goes through the client's jar (httpx
    ignores a per-request `cookies=` for a mounted sub-app).

    Returns the body with the HTTP status folded in as `_status`: since #432 the surface is a
    real route carrying `current_user`, so an unauthenticated caller is refused at the transport
    (401/403) instead of receiving a 200 with a GraphQL `errors` envelope. Callers assert that it
    did not ANSWER, and accept either shape of refusal."""
    client.cookies.clear()
    if session:
        client.cookies.set(user_auth.COOKIE, session)
    try:
        r = client.post("/graphql", json={"query": _GQL}, headers=headers or {})
        body = r.json()
        if isinstance(body, dict):
            body = {**body, "_status": r.status_code}
        return body
    finally:
        client.cookies.clear()


def test_graphql_dev_header_refused_when_login_enabled():
    """#184 — the SAME property, on the OTHER transport. /graphql is a separately mounted
    ASGI app whose context called `resolve_identity` directly, so it never inherited #183's
    coupling: with a real login configured, an unauthenticated caller could POST
    `X-DBSearch-User: <victim-oid>` to /graphql and receive that victim's permission-trimmed
    documents (a straight LAW 2 breach — the same class of hole #183 closed on REST).

    Both transports now call ONE resolver (`dbsearch.api.auth.resolve_identity`, with the
    header AND cookie seams), so they cannot drift apart again."""
    from fastapi.testclient import TestClient

    from dbsearch.server.app import app

    saved = {k: os.environ.get(k) for k in _AUTH_VARS}
    client = TestClient(app)
    try:
        _enable_real_login()
        assert user_auth.is_enabled(), "test setup failed: is_enabled() should be True"

        body = _graphql(client, headers={"x-dbsearch-user": "victim-oid"})
        assert body.get("data") is None, (
            "VULNERABLE: /graphql answered a dev-header caller while real login was "
            f"configured — {body}")
        # Either shape of refusal is fine; that it refused is the invariant (see _graphql).
        assert body.get("errors") or body.get("_status") in (401, 403), body
        assert "unauthenticated" in str(body.get("errors") or body.get("detail", "")).lower() \
            or "sign in" in str(body.get("detail", "")).lower(), body

        # positive control 1: a real signed session DOES authenticate on /graphql
        token = user_auth.sign_session({"oid": "real-oid", "exp": int(time.time()) + 3600})
        ok = _graphql(client, session=token)
        assert ok.get("errors") is None, ok
        assert ok["data"]["search"] is not None, ok

        # positive control 2: with NO real login configured, the dev header still works
        # (self-host/dev must keep working — the coupling is to real login, not a blanket ban)
        _clear_auth_vars()
        assert not user_auth.is_enabled()
        dev = _graphql(client, headers={"x-dbsearch-user": "dev-user"})
        assert dev.get("errors") is None, dev
        assert dev["data"]["search"] is not None, dev
    finally:
        _clear_auth_vars()
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def main():
    test_dev_header_refused_when_login_enabled()
    print("  PASS  dev header refused (401) when real login is enabled — closes "
          "cross-user vaulted-token redemption (#183)")
    test_valid_cookie_wins_over_conflicting_header_when_login_enabled()
    print("  PASS  valid session cookie wins over a conflicting dev header when "
          "real login is enabled")
    test_dev_header_preserved_when_login_disabled()
    print("  PASS  dev header path unchanged when real login is disabled "
          "(self-host/dev unaffected)")
    test_graphql_dev_header_refused_when_login_enabled()
    print("  PASS  /graphql shares the SAME chokepoint: dev header refused under a real "
          "login, session cookie honored, dev header intact without a login (#184)")
    print("\n#183/#184 DEV-HEADER / LOGIN EXCLUSION SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
