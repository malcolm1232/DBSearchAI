"""#646 / ADR 0023 - connecting Microsoft must not replace the account you are signed in as.

THE DEFECT: /auth/callback minted unconditionally. A user signed in as their email account who
reached Microsoft sign-in was silently re-principaled to the Entra oid - different account,
different partition, their workspace and conversations simply absent. Google has linked
instead of minting since #193; the asymmetry was the bug.

Every test drives the REAL callback with a stubbed code exchange, so the branch under test is
the one the browser hits.

    PYTHONPATH=src python3 tests/selftest_646_entra_linking.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("DBSEARCH_SESSION_KEY", "test-key-646")
os.environ.setdefault("SP_CONNECTOR_CLIENT_ID", "cid")
os.environ.setdefault("SP_CONNECTOR_CLIENT_SECRET", "csec")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import app as app_mod  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402

LOCAL_OID = "acct_local_malcolm"
ENTRA_OID = "entra-oid-1111"
OTHER_OID = "acct_someone_else"

ENTRA_USER = {"oid": ENTRA_OID, "tid": "tenant-1", "name": "Malcolm Tan",
              "email": "malcolm@corp.example", "refresh_token": "entra-rt"}


class _Ctx:
    """Stub the code exchange and the CSRF state, so the callback itself is exercised."""

    def __init__(self, user=None):
        self.user = dict(user if user is not None else ENTRA_USER)

    def __enter__(self):
        self._ex = user_auth.exchange_code
        self._st = app_mod._state_ok
        user_auth.exchange_code = lambda code, **k: dict(self.user)
        app_mod._state_ok = lambda request, params: True
        return self

    def __exit__(self, *a):
        user_auth.exchange_code = self._ex
        app_mod._state_ok = self._st


def _session(oid, idp="local"):
    return user_auth.sign_session({"oid": oid, "name": oid, "email": f"{oid}@x",
                                   "tid": "", "idp": idp,
                                   "exp": int(time.time()) + 600})


def _callback(session_oid=None, user=None, idp="local"):
    """Run /auth/callback with (or without) a session. Returns (response, client)."""
    user_auth.VAULT.drop(LOCAL_OID)
    user_auth.VAULT.drop(ENTRA_OID)
    c = TestClient(app_mod.app, follow_redirects=False)
    if session_oid:
        c.cookies.set(user_auth.COOKIE, _session(session_oid, idp))
    with _Ctx(user):
        r = c.get("/auth/callback", params={"code": "abc", "state": "s"})
    return r, c


def _session_oid_after(resp, fallback):
    """The oid the browser will carry AFTER this response - i.e. whether it was swapped."""
    cookie = resp.cookies.get(user_auth.COOKIE)
    if not cookie:
        return fallback          # no Set-Cookie: the session is untouched
    return (user_auth.read_session(cookie) or {}).get("oid")


# --- the defect itself -----------------------------------------------------------------

def test_linking_microsoft_does_not_swap_the_account():
    """THE BUG, pinned. A local session reaching the Entra callback must keep its own oid."""
    r, _ = _callback(session_oid=LOCAL_OID)
    assert _session_oid_after(r, LOCAL_OID) == LOCAL_OID, \
        "the session was re-principaled to the Entra oid - #646 is back"


def test_the_credential_is_vaulted_under_the_SESSION_oid():
    """And the link has to actually buy something: an entra credential redeemable AS the
    account the user is signed in as."""
    _callback(session_oid=LOCAL_OID)
    assert user_auth.VAULT.linked(LOCAL_OID) == ["entra"], user_auth.VAULT.linked(LOCAL_OID)
    assert user_auth.VAULT.linked(ENTRA_OID) == [], \
        "the token was vaulted under the Entra oid, which is the account nobody is using"


def test_a_link_writes_nothing_to_the_session():
    """ADR 0023: credential only. No tid, no partition change - so no Set-Cookie at all."""
    r, _ = _callback(session_oid=LOCAL_OID)
    assert user_auth.COOKIE not in r.cookies, \
        "the link rewrote the session cookie; it must leave the identity alone"


# --- the two branches that must NOT change ------------------------------------------------

def test_a_signed_out_user_still_becomes_their_microsoft_identity():
    """Unchanged behaviour, and the reason this is a branch rather than a blanket refusal."""
    r, _ = _callback(session_oid=None)
    assert _session_oid_after(r, None) == ENTRA_OID, \
        "a signed-out sign-in must still mint the Entra session"


def test_re_authenticating_as_yourself_is_a_mint_not_a_link():
    """The 'Sign in again' pill exists to re-mint a credential for the SAME oid. Treating
    that as a link would leave the one control for #210's stale vault unable to do its job."""
    r, _ = _callback(session_oid=ENTRA_OID, idp="entra")
    assert _session_oid_after(r, ENTRA_OID) == ENTRA_OID
    assert user_auth.VAULT.linked(ENTRA_OID) == ["entra"], user_auth.VAULT.linked(ENTRA_OID)


# --- the refusals, inherited from Google's callback ---------------------------------------

def test_no_refresh_token_is_refused_rather_than_reported_as_connected():
    """Saying 'Connected' with nothing to redeem is a lie the user only discovers on their
    first delegated ask, long after the consent screen."""
    r, _ = _callback(session_oid=LOCAL_OID, user={**ENTRA_USER, "refresh_token": ""})
    assert r.status_code == 302, r.status_code
    assert "login=error" in r.headers["location"], r.headers["location"]
    assert user_auth.VAULT.linked(LOCAL_OID) == [], "a credential-less link still vaulted"


def test_an_entra_identity_owned_by_another_account_is_refused():
    """ACCOUNTS.link hands back the PRE-EXISTING owner rather than re-pointing. Vaulting
    anyway would say 'Connected' while the account graph disagrees."""
    orig = app_mod.ACCOUNTS.link
    app_mod.ACCOUNTS.link = lambda idp, subject, account_id: OTHER_OID
    try:
        r, _ = _callback(session_oid=LOCAL_OID)
    finally:
        app_mod.ACCOUNTS.link = orig
    assert "login=error" in r.headers["location"], r.headers["location"]
    assert user_auth.VAULT.linked(LOCAL_OID) == [], "linked despite the identity being owned"


def test_the_refusal_names_neither_account():
    """It must not become an oracle for who owns what."""
    import urllib.parse
    orig = app_mod.ACCOUNTS.link
    app_mod.ACCOUNTS.link = lambda idp, subject, account_id: OTHER_OID
    try:
        r, _ = _callback(session_oid=LOCAL_OID)
    finally:
        app_mod.ACCOUNTS.link = orig
    msg = urllib.parse.unquote(r.headers["location"])
    for secret in (OTHER_OID, LOCAL_OID, ENTRA_OID, ENTRA_USER["email"]):
        assert secret not in msg, f"the refusal leaked {secret!r}: {msg}"


# --- the entry point ----------------------------------------------------------------------

def test_the_link_route_requires_a_session():
    """Linking upgrades an identity we already trust; an anonymous caller has nothing to
    upgrade (the stance /auth/grant/db takes)."""
    r = TestClient(app_mod.app, follow_redirects=False).get("/auth/entra/link")
    assert r.status_code == 401, r.status_code


def test_the_link_route_starts_the_oauth_leg_for_a_signed_in_caller():
    c = TestClient(app_mod.app, follow_redirects=False)
    c.cookies.set(user_auth.COOKIE, _session(LOCAL_OID))
    r = c.get("/auth/entra/link")
    assert r.status_code == 302, r.status_code
    assert "oauth2/v2.0/authorize" in r.headers["location"], r.headers["location"]


def test_the_link_route_reuses_the_configured_redirect_uri():
    """No second Azure redirect URI - that is what keeps this a code-only fix."""
    import urllib.parse
    c = TestClient(app_mod.app, follow_redirects=False)
    c.cookies.set(user_auth.COOKIE, _session(LOCAL_OID))
    loc = c.get("/auth/entra/link").headers["location"]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    assert q["redirect_uri"][0] == user_auth.redirect_uri(), q.get("redirect_uri")


# --- the pill that started it -------------------------------------------------------------

def test_the_connect_pill_no_longer_points_at_canvas():
    """#646's first half. Asserting on the ROSTER's emitted hrefs, not on prose."""
    src = (Path(__file__).resolve().parents[1]
           / "src/dbsearch/server/static/js/ui/account.js").read_text(encoding="utf-8")
    roster = src[src.index("const ROSTER = ["):src.index("];", src.index("const ROSTER = ["))]
    assert '"/auth/entra/link"' in roster, roster
    assert '"/auth/google/login"' in roster, roster
    assert '"/canvas"' not in roster, "a provider still points its Connect pill at /canvas"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    print("FAILED" if fails else "all green")
    sys.exit(1 if fails else 0)
