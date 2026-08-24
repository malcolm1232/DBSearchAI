"""#574 - local email/password sign-in, REMOVABLE by unsetting DBSEARCH_LOCAL_AUTH.

LAW 2, the invariant that matters most here: the email is a LOGIN HANDLE only. It is
unverified (nobody proved they own it), so it must never become the session identity,
never key a workspace, and never enter principal expansion. The session `oid` for a
local account is the opaque `acct_*` id minted by ACCOUNTS.create_local_user - this file
tests that directly (test_signup_creates_an_opaque_account_never_keyed_by_email and the
oid assertion inside test_signup_then_login_mints_a_session_and_upload_persists_for_it),
not just asserted in prose.

Code review (260807) added a second invariant that matters just as much: a deployment
that sets DBSEARCH_LOCAL_AUTH=1 with no real session-signing key must get local auth
OFF, not a working-but-forgeable one - test_local_auth_refuses_to_enable_without_a_
real_signing_key and its neighbors below prove that directly against local_auth.
is_enabled() and user_auth._key(), not just against the route's observable behavior.

    PYTHONPATH=src python3 tests/selftest_574_local_auth.py
"""
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from _upload import settle  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import app  # noqa: E402


def test_hash_and_verify_roundtrip():
    from dbsearch.server import local_auth
    salt, h = local_auth.hash_password("correct horse battery staple")
    assert local_auth.verify_password("correct horse battery staple", salt, h)
    assert not local_auth.verify_password("wrong", salt, h)


def test_signup_creates_an_opaque_account_never_keyed_by_email():
    from dbsearch.server.accounts import InMemoryAccountStore
    from dbsearch.server import local_auth
    s = InMemoryAccountStore()
    salt, h = local_auth.hash_password("hunter2hunter2")
    acc = s.create_local_user("dana@example.com", salt, h)
    assert acc.startswith("acct_") and "@" not in acc
    assert s.resolve("local", "dana@example.com") == acc


def test_duplicate_signup_is_refused():
    from dbsearch.server.accounts import InMemoryAccountStore
    from dbsearch.server import local_auth
    s = InMemoryAccountStore()
    salt, h = local_auth.hash_password("hunter2hunter2")
    s.create_local_user("dana@example.com", salt, h)
    try:
        s.create_local_user("dana@example.com", salt, h)
        assert False, "duplicate email accepted"
    except ValueError:
        pass


def test_email_validation_rejects_embedded_whitespace_and_malformed_at_shapes():
    """Code review Finding 5: the original check only rejected the literal space
    character and only required an "@" to appear somewhere in the middle - so
    "a@b\\nc.com" (a newline, not a space) and "@@" (two "@"s, both parts empty
    around the first) both slipped through. Neither should."""
    from dbsearch.server import local_auth
    assert not local_auth.valid_email("a@b\nc.com")
    assert not local_auth.valid_email("a@b\tc.com")
    assert not local_auth.valid_email("@@")
    assert not local_auth.valid_email("@example.com")      # empty local part
    assert not local_auth.valid_email("dana@")              # empty domain part
    assert not local_auth.valid_email("dana@a@b.com")       # more than one "@"
    assert local_auth.valid_email("dana@example.com")


# ---- Finding 1 (CRITICAL): local auth must refuse to enable without a real signing
# key, so a deployment cannot mint a session anyone can forge with the public
# "dev-secret" literal in user_auth._key(). ---------------------------------------------
# All auth-relevant env vars this file ever touches, saved and restored around every
# test via _EnvSandbox - the constraint is RESTORE, not unset: a process that had a
# live Entra config before this file ran must still have it after, and no test here may
# leak a value into the next one.
_ALL_AUTH_VARS = (
    "DBSEARCH_LOCAL_AUTH", "DBSEARCH_SESSION_KEY",
    "AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET",
    "SP_CONNECTOR_CLIENT_ID", "SP_CONNECTOR_CLIENT_SECRET",
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
)


class _EnvSandbox:
    """Save the current value (or absence) of a set of env vars on entry, restore them
    exactly on exit - present vars come back present with their old value, absent vars
    come back absent. Never used to prove isolation between test files (each selftest
    runs in its own process); only to guarantee one test in THIS file cannot leak state
    into the next one, or into whatever ran this file's process before it."""

    def __init__(self, names):
        self._names = names
        self._saved = {}

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._names}
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False

    def clear(self, *names):
        for k in names:
            os.environ.pop(k, None)

    def set(self, **kv):
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _enable_local_auth_with_entra_secret(env: _EnvSandbox):
    """The shape used by the route-level tests below: DBSEARCH_LOCAL_AUTH=1 plus a real
    Entra secret, which doubles as a real signing key (has_real_signing_key() reads
    client_secret() too) - so real_login_enabled() is true and the session-cookie branch
    of resolve_tenant/resolve_identity is exercised, matching how a real deployment with
    local auth on actually behaves."""
    env.clear(*_ALL_AUTH_VARS)
    env.set(DBSEARCH_LOCAL_AUTH="1", AUTH_TENANT_ID="tid-home",
            AUTH_CLIENT_ID="cid", AUTH_CLIENT_SECRET="sec")


def test_local_auth_refuses_to_enable_without_a_real_signing_key():
    from dbsearch.server import local_auth
    with _EnvSandbox(_ALL_AUTH_VARS) as env:
        env.clear(*_ALL_AUTH_VARS)
        env.set(DBSEARCH_LOCAL_AUTH="1")     # flag on, but no signing key anywhere
        assert local_auth.is_enabled() is False, (
            "local auth reported enabled with no signing key configured - "
            "sessions minted this way would be forgeable with the public dev-secret")


def test_local_auth_enables_with_a_dedicated_session_key_alone():
    """A local-only deployment (no Entra, no Google) does not have to fake a client
    secret to get a real signing key - DBSEARCH_SESSION_KEY on its own is sufficient."""
    from dbsearch.server import local_auth
    with _EnvSandbox(_ALL_AUTH_VARS) as env:
        env.clear(*_ALL_AUTH_VARS)
        env.set(DBSEARCH_LOCAL_AUTH="1", DBSEARCH_SESSION_KEY="a-real-secret-value")
        assert local_auth.is_enabled() is True


def test_signup_is_404_when_the_flag_is_set_but_no_signing_key_exists():
    """The route-level proof of the same property: with DBSEARCH_LOCAL_AUTH=1 and every
    signing secret absent, /auth/local/signup must not mint a session at all - the
    existing disabled-route 404 is the correct, safe outcome here."""
    from dbsearch.server import app as appmod
    from dbsearch.server.accounts import InMemoryAccountStore
    with _EnvSandbox(_ALL_AUTH_VARS) as env:
        env.clear(*_ALL_AUTH_VARS)
        env.set(DBSEARCH_LOCAL_AUTH="1")
        saved_accounts = appmod.ACCOUNTS
        appmod.ACCOUNTS = InMemoryAccountStore()
        try:
            client = TestClient(app)
            r = client.post("/auth/local/signup",
                            json={"email": "ivy@example.com", "password": "hunter2hunter2"})
            assert r.status_code == 404, (
                f"local auth minted a session with no real signing key configured: "
                f"{r.status_code} {r.text}")
        finally:
            appmod.ACCOUNTS = saved_accounts


def test_session_signing_key_is_never_the_dev_fallback_while_a_real_login_is_enabled():
    """The property Finding 1 is actually about, checked directly against
    user_auth._key() rather than inferred from route behavior: whenever ANY real login
    is enabled (Entra-shaped, or local with a real signing key), the key that signs
    dbs_session must not be the literal "dev-secret" - that string is public (it sits in
    this repo's source), so a session signed with it is forgeable by anyone who reads
    this file. The one state where "dev-secret" IS reachable is proven separately below
    to be the state where NO real login is enabled at all (plain self-host/dev)."""
    from dbsearch.server import google_auth, local_auth
    with _EnvSandbox(_ALL_AUTH_VARS) as env:
        env.clear(*_ALL_AUTH_VARS)
        env.set(AUTH_TENANT_ID="tid-home", AUTH_CLIENT_ID="cid", AUTH_CLIENT_SECRET="sec")
        assert user_auth.is_enabled()
        assert user_auth._key() != b"dev-secret"          # noqa: SLF001 - the seam under test

        env.clear(*_ALL_AUTH_VARS)
        env.set(DBSEARCH_LOCAL_AUTH="1", DBSEARCH_SESSION_KEY="another-real-secret")
        assert local_auth.is_enabled()
        assert user_auth._key() != b"dev-secret"           # noqa: SLF001

        env.clear(*_ALL_AUTH_VARS)
        env.set(DBSEARCH_LOCAL_AUTH="1")                    # flag on, nothing else
        assert not local_auth.is_enabled()
        assert not user_auth.is_enabled()
        assert not google_auth.is_enabled()
        # No real login is enabled anywhere in THIS state - "dev-secret" is reachable
        # here, and that is correct: it is plain self-host/dev, not a real login at all.
        assert user_auth._key() == b"dev-secret"            # noqa: SLF001


# ---- route level ---------------------------------------------------------------------
def test_signup_then_login_mints_a_session_and_upload_persists_for_it():
    from dbsearch.server import app as appmod
    from dbsearch.server.accounts import InMemoryAccountStore
    saved_accounts = appmod.ACCOUNTS
    appmod.ACCOUNTS = InMemoryAccountStore()
    env = _EnvSandbox(_ALL_AUTH_VARS)
    env.__enter__()
    _enable_local_auth_with_entra_secret(env)
    client = TestClient(app)
    try:
        r = client.post("/auth/local/signup",
                        json={"email": "Dana@Example.com", "password": "hunter2hunter2"})
        assert r.status_code == 200, r.text
        cookie = r.cookies.get(user_auth.COOKIE)
        assert cookie, "signup did not set a session cookie"
        sess = user_auth.read_session(cookie)
        assert sess is not None
        assert sess["oid"].startswith("acct_"), f"session oid is not opaque: {sess['oid']}"
        assert "@" not in sess["oid"], f"the email leaked into the session oid: {sess['oid']}"
        assert sess["tid"] == "", f"a local session must carry an empty tid, got {sess['tid']!r}"
        assert sess["email"] == "dana@example.com", sess["email"]

        # login (separately, as a second act) must mint a session for the SAME account.
        r2 = client.post("/auth/local/login",
                         json={"email": "dana@example.com", "password": "hunter2hunter2"})
        assert r2.status_code == 200, r2.text
        login_cookie = r2.cookies.get(user_auth.COOKIE)
        login_sess = user_auth.read_session(login_cookie)
        assert login_sess["oid"] == sess["oid"], "login landed on a different account than signup"

        body = b"Vendor payment terms are net 45 days for all F&N suppliers."
        up = client.post("/admin/upload", cookies={user_auth.COOKIE: login_cookie},
                         data={"title": "terms.txt"},
                         files={"file": ("terms.txt", io.BytesIO(body), "text/plain")})
        assert up.status_code == 202, f"local-account upload was refused: {up.status_code} {up.text[:200]}"
        job = settle(client, up, cookies={user_auth.COOKIE: login_cookie})
        assert job["status"] == "succeeded", job

        mine = json.dumps(client.post("/search", cookies={user_auth.COOKIE: login_cookie},
                                      json={"question": "vendor payment terms"}).json())
        assert "45" in mine, f"uploader cannot read their own upload back: {mine[:300]}"

        # a second, unrelated local account must not see it (LAW 2 / ADR 0018 partitioning).
        r3 = client.post("/auth/local/signup",
                         json={"email": "erin@example.com", "password": "hunter2hunter2"})
        assert r3.status_code == 200, r3.text
        other_cookie = r3.cookies.get(user_auth.COOKIE)
        other = json.dumps(client.post("/search", cookies={user_auth.COOKIE: other_cookie},
                                       json={"question": "vendor payment terms"}).json())
        assert "45" not in other, "a second local account saw the first account's document"
    finally:
        appmod.ACCOUNTS = saved_accounts
        env.__exit__(None, None, None)


def test_login_wrong_password_is_a_uniform_401():
    from dbsearch.server import app as appmod
    from dbsearch.server.accounts import InMemoryAccountStore
    saved_accounts = appmod.ACCOUNTS
    appmod.ACCOUNTS = InMemoryAccountStore()
    env = _EnvSandbox(_ALL_AUTH_VARS)
    env.__enter__()
    _enable_local_auth_with_entra_secret(env)
    client = TestClient(app)
    try:
        client.post("/auth/local/signup",
                    json={"email": "frank@example.com", "password": "hunter2hunter2"})
        wrong_pw = client.post("/auth/local/login",
                               json={"email": "frank@example.com", "password": "totally-wrong"})
        unknown_email = client.post("/auth/local/login",
                                    json={"email": "nobody@example.com", "password": "irrelevant"})
        assert wrong_pw.status_code == 401, wrong_pw.text
        assert unknown_email.status_code == 401, unknown_email.text
        # no oracle: the same status AND the same body distinguish neither case.
        assert wrong_pw.json() == unknown_email.json(), (
            f"wrong-password and unknown-email responses differ: "
            f"{wrong_pw.json()} vs {unknown_email.json()}")
        assert wrong_pw.json()["detail"] == "invalid email or password"
    finally:
        appmod.ACCOUNTS = saved_accounts
        env.__exit__(None, None, None)


def test_short_password_is_refused():
    from dbsearch.server import app as appmod
    from dbsearch.server.accounts import InMemoryAccountStore
    saved_accounts = appmod.ACCOUNTS
    appmod.ACCOUNTS = InMemoryAccountStore()
    env = _EnvSandbox(_ALL_AUTH_VARS)
    env.__enter__()
    _enable_local_auth_with_entra_secret(env)
    client = TestClient(app)
    try:
        r = client.post("/auth/local/signup",
                        json={"email": "gina@example.com", "password": "short"})
        assert r.status_code == 400, r.text
        assert "8 characters" in r.json()["detail"], r.json()
    finally:
        appmod.ACCOUNTS = saved_accounts
        env.__exit__(None, None, None)


def test_disabled_flag_hides_the_routes():
    env = _EnvSandbox(_ALL_AUTH_VARS)
    env.__enter__()
    try:
        env.clear(*_ALL_AUTH_VARS)      # DBSEARCH_LOCAL_AUTH unset - removable by config
        client = TestClient(app)
        r1 = client.post("/auth/local/signup",
                         json={"email": "h@example.com", "password": "hunter2hunter2"})
        r2 = client.post("/auth/local/login",
                         json={"email": "h@example.com", "password": "hunter2hunter2"})
        assert r1.status_code == 404, r1.text
        assert r2.status_code == 404, r2.text
    finally:
        env.__exit__(None, None, None)


_COOKIE_VARS = _ALL_AUTH_VARS + ("DBSEARCH_COOKIE_SECURE", "AUTH_REDIRECT_URI",
                                 "GOOGLE_REDIRECT_URI")


def _reset_signup_rate_limit() -> None:
    """The signup limiter is 5 per IP per minute (#574 review Finding 2) and the whole file
    shares one process and one client address, so a test that signs up must hand the budget
    back or it starves whichever test runs next. Called on both sides of every signup below."""
    from dbsearch.server import app as appmod
    appmod._LOCAL_RATE_ATTEMPTS.clear()


def _signup_set_cookie_header(email: str) -> str:
    """The raw Set-Cookie header the local signup route emits, so the `Secure` ATTRIBUTE can
    be read. `client.cookies` drops attributes, so asserting on it would silently pass with
    the flag missing - which is exactly how this went unnoticed on all three IdPs."""
    from dbsearch.server import app as appmod
    from dbsearch.server.accounts import InMemoryAccountStore
    saved = appmod.ACCOUNTS
    appmod.ACCOUNTS = InMemoryAccountStore()
    _reset_signup_rate_limit()
    try:
        r = TestClient(app).post("/auth/local/signup",
                                 json={"email": email, "password": "hunter2hunter2"})
        assert r.status_code == 200, r.text
        return r.headers.get("set-cookie", "")
    finally:
        appmod.ACCOUNTS = saved
        _reset_signup_rate_limit()


def test_the_session_cookie_is_secure_on_an_https_deployment():
    """FINAL REVIEW ledger item: no cookie-setting path set `secure=True`. Pre-existing on
    all three IdPs, but #574 is what makes a PASSWORD mint one, so a plaintext hop now hands
    over a session derived from a credential the user typed.

    The signal is the deployment's own configured OAuth redirect URI - the one value that
    already has to be truthful about the scheme users reach this box on - never a
    caller-controlled header like X-Forwarded-Proto."""
    with _EnvSandbox(_COOKIE_VARS) as env:
        _enable_local_auth_with_entra_secret(env)
        env.set(AUTH_REDIRECT_URI="https://dbsearch.ai/auth/callback")
        header = _signup_set_cookie_header("secure1@example.com")
        assert "secure" in header.lower(), (
            f"an https deployment minted a session cookie with no Secure flag: {header}")


def test_a_plain_http_dev_rig_still_gets_a_usable_cookie():
    """The guard is the whole point: a `Secure` cookie is DISCARDED by the browser over
    http, so flipping this on unconditionally would make every local rig and every plain-http
    self-host box unable to sign in at all - a worse failure than the one being fixed. Verify
    the rig rather than assume it: sign up on the default (http) config and then USE the
    cookie the response set."""
    from dbsearch.server import app as appmod
    from dbsearch.server.accounts import InMemoryAccountStore
    saved = appmod.ACCOUNTS
    appmod.ACCOUNTS = InMemoryAccountStore()
    with _EnvSandbox(_COOKIE_VARS) as env:
        try:
            _enable_local_auth_with_entra_secret(env)   # no *_REDIRECT_URI -> http defaults
            client = TestClient(app)
            _reset_signup_rate_limit()
            r = client.post("/auth/local/signup",
                            json={"email": "httprig@example.com", "password": "hunter2hunter2"})
            assert r.status_code == 200, r.text
            assert "secure" not in r.headers.get("set-cookie", "").lower(), (
                "a plain-http rig minted a Secure cookie, which the browser discards - "
                f"nobody can sign in: {r.headers.get('set-cookie')}")
            # And the session it minted actually authenticates a real request.
            me = client.get("/auth/me", cookies={user_auth.COOKIE: r.cookies[user_auth.COOKIE]})
            assert me.status_code == 200 and me.json().get("signed_in") is True, me.text[:200]
        finally:
            appmod.ACCOUNTS = saved
            _reset_signup_rate_limit()


def test_cookie_secure_can_be_forced_for_tls_terminated_upstream():
    """A box whose TLS is terminated by Caddy while its configured redirect URI still says
    http would otherwise never set the flag. One explicit override, both directions."""
    with _EnvSandbox(_COOKIE_VARS) as env:
        _enable_local_auth_with_entra_secret(env)
        env.set(DBSEARCH_COOKIE_SECURE="1")
        assert "secure" in _signup_set_cookie_header("forceon@example.com").lower(), \
            "DBSEARCH_COOKIE_SECURE=1 did not force the flag on"

        env.set(DBSEARCH_COOKIE_SECURE="0",
                AUTH_REDIRECT_URI="https://dbsearch.ai/auth/callback")
        assert "secure" not in _signup_set_cookie_header("forceoff@example.com").lower(), \
            "DBSEARCH_COOKIE_SECURE=0 did not force the flag off"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
