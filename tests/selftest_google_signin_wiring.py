"""#193 — Google sign-in wiring: /auth/google/login redirects to Google; the callback vaults
the refresh token under the GOOGLE idp and mints a session; linking from an EXISTING session
keeps that session's oid AND mints no new cookie (account linking, one identity + N cloud
creds); /auth/me reports linked clouds; the dev header stays fail-closed when Google login is
configured (#183 parity).

Security properties pinned here (each FAILED before the Task-4 review fixes):
  - CSRF: the OAuth `state` must be a real per-browser token. A missing state, a forged
    state, and a state minted for ANOTHER browser must all be refused (400). Otherwise an
    attacker completes their own Google consent, lures a signed-in victim to a top-level GET
    callback carrying the attacker's `code` (SameSite=Lax sends the victim's session cookie
    on a top-level navigation), and the ATTACKER's refresh token gets vaulted under the
    VICTIM's oid - every later BigQuery/Drive query the victim runs then executes as the
    attacker's Google identity.
  - LAW 1: the refresh token never reaches a cookie, a response body, or a redirect URL.
  - LAW 2: the ENTRA env dev seam is never substituted for another cloud's credential.

Run: PYTHONPATH=src python3 tests/selftest_google_signin_wiring.py
"""
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Google login ONLY: an ambient SP_CONNECTOR_*/AUTH_* in the shell would make
# user_auth.is_enabled() True, and the fail-closed test below would then pass through the OLD
# Entra branch - proving nothing about the Google clause it names. Clear them BEFORE importing.
for _k in ("SP_CONNECTOR_CLIENT_ID", "SP_CONNECTOR_CLIENT_SECRET", "SP_CONNECTOR_REDIRECT_URI",
           "AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET"):
    os.environ.pop(_k, None)
os.environ["GOOGLE_CLIENT_ID"] = "cid"
os.environ["GOOGLE_CLIENT_SECRET"] = "csec"
os.environ["GOOGLE_REDIRECT_URI"] = "http://localhost:8080/auth/google/callback"

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import app as app_mod  # noqa: E402
from dbsearch.server import google_auth, sp_connect, user_auth  # noqa: E402

client = TestClient(app_mod.app, follow_redirects=False)

RT = "rt-google"          # the secret that must never leave the vault (LAW 1)


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "hdr." + body + ".sig"


def _fake_google(email="alice@gmail.com", refresh_token=RT, sub="108"):
    def fake_post(url, form):
        body = {"id_token": _jwt({"sub": sub, "email": email,
                                  "email_verified": True, "name": "Alice"})}
        if refresh_token:
            body["refresh_token"] = refresh_token
        return body
    return fake_post


class _google:
    """Patch google_auth.exchange_code's network leg for the duration of a block."""

    def __init__(self, **kw):
        self._post = _fake_google(**kw)

    def __enter__(self):
        self._orig = google_auth.exchange_code
        google_auth.exchange_code = lambda code, post=None: self._orig(code, post=self._post)

    def __exit__(self, *exc):
        google_auth.exchange_code = self._orig


def _begin_login(**params) -> str:
    """Drive the REAL login leg, so the client's cookie jar holds the pre-auth CSRF nonce.
    Returns the `state` Google would echo back to the callback."""
    client.cookies.clear()
    r = client.get("/auth/google/login", params=params)
    assert r.status_code == 302, r.status_code
    return dict(parse_qsl(urlsplit(r.headers["location"]).query))["state"]


def _set_session(cookie: str) -> None:
    client.cookies.set(user_auth.COOKIE, cookie)


def _session_cookies(r) -> list:
    return [h for h in r.headers.get_list("set-cookie") if h.startswith(user_auth.COOKIE + "=")]


def _assert_no_secret_leak(r) -> None:
    """LAW 1: the refresh token never reaches a cookie, a body, or a redirect URL."""
    blob = ("\n".join(r.headers.get_list("set-cookie")) + "\n"
            + r.headers.get("location", "") + "\n" + r.text)
    assert RT not in blob, f"LAW 1 BREACH: the refresh token appeared in the response: {blob!r}"


def test_login_redirects_to_google():
    r = client.get("/auth/google/login")
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "access_type=offline" in r.headers["location"]
    # the CSRF nonce rides an httpOnly, SameSite=Lax pre-auth cookie
    raw = [h for h in r.headers.get_list("set-cookie")
           if h.startswith(sp_connect.STATE_COOKIE + "=")]
    assert raw, "login must set the pre-auth CSRF nonce cookie"
    assert "httponly" in raw[0].lower() and "samesite=lax" in raw[0].lower(), raw[0]
    client.cookies.clear()


def test_login_rejects_an_unknown_channel():
    """A typo'd channel used to be dropped silently: the user consents to the BASE scopes
    only, a refresh token with NO data scopes is vaulted, the UI reports success - and every
    later query fails opaquely at Google, far from the typo."""
    r = client.get("/auth/google/login", params={"channels": "bigquery,bigqeury"})
    assert r.status_code == 400, r.status_code
    assert "bigqeury" in r.json()["detail"]
    ok = client.get("/auth/google/login", params={"channels": "bigquery,drive"})
    assert ok.status_code == 302, ok.status_code
    assert "drive.readonly" in ok.headers["location"]
    client.cookies.clear()


def test_callback_vaults_under_google_and_signs_in():
    user_auth.VAULT.drop("alice@gmail.com")
    state = _begin_login()
    with _google():
        r = client.get(f"/auth/google/callback?code=abc&state={state}")
    assert r.status_code == 302
    # identity = the verified email; credential vaulted under the GOOGLE idp only
    assert user_auth.VAULT.get("alice@gmail.com", idp="google") == RT
    assert user_auth.VAULT.linked("alice@gmail.com") == ["google"]
    # a signed-OUT user becomes their Google identity: the session cookie IS set
    assert _session_cookies(r), \
        "the signed-out path must sign the user in — no session cookie was set"
    sess = user_auth.read_session(client.cookies.get(user_auth.COOKIE, ""))
    assert sess and sess["oid"] == "alice@gmail.com", sess
    _assert_no_secret_leak(r)                      # LAW 1
    client.cookies.clear()


def test_linking_from_an_existing_session_keeps_that_oid():
    """Account linking: signed in as an Entra oid, connecting Google must vault the Google
    credential UNDER THAT SAME oid — not fork a second identity, and not silently re-mint the
    session as the Google email (which would switch the identity every later query runs as,
    while still passing an oid-only assertion)."""
    user_auth.VAULT.drop("entra-oid-1")
    user_auth.VAULT.put("entra-oid-1", "rt-entra")
    cookie = user_auth.sign_session({"oid": "entra-oid-1", "name": "M", "email": "m@x.com",
                                     "exp": 2 ** 31})
    state = _begin_login()                          # clears the jar, then sets the nonce
    _set_session(cookie)
    # #572 finding 1: a Google `sub` that another test already signed in standalone (the
    # default "108", claimed by alice@gmail.com above) is now a REAL conflict - ACCOUNTS
    # correctly refuses to move it. This test's own subject is about linking a Google
    # identity nobody has claimed yet, so it needs one this suite has not touched.
    with _google(email="entra-linked@gmail.com", sub="208"):
        r = client.get(f"/auth/google/callback?code=abc&state={state}")
    assert r.status_code == 302
    assert sorted(user_auth.VAULT.linked("entra-oid-1")) == ["entra", "google"]
    assert user_auth.VAULT.get("entra-oid-1", idp="google") == RT
    # the linking path must not touch the session: no new cookie, so no identity switch
    assert not _session_cookies(r), \
        "linking re-minted the session cookie — the signed-in identity would silently switch"
    _assert_no_secret_leak(r)                      # LAW 1
    client.cookies.clear()


def test_callback_without_a_refresh_token_is_an_error_not_a_lie():
    """Google can return an id_token with NO refresh token (e.g. a re-link without consent).
    Reporting `?linked=google` then is a lie the user only discovers when their first query
    fails: nothing was vaulted."""
    user_auth.VAULT.drop("nort@gmail.com")
    state = _begin_login()
    with _google(email="nort@gmail.com", refresh_token=""):
        r = client.get(f"/auth/google/callback?code=abc&state={state}")
    assert r.status_code == 302
    assert "linked=google" not in r.headers["location"], r.headers["location"]
    assert "login=error" in r.headers["location"], r.headers["location"]
    assert user_auth.VAULT.linked("nort@gmail.com") == []
    assert not _session_cookies(r), "nothing was vaulted, so nobody may be signed in"
    client.cookies.clear()


def test_callback_surfaces_a_declined_consent():
    """?error=access_denied (the user clicked Cancel) used to fall straight into the state
    check and return a raw 400 JSON page. Mirror the Entra leg: back to the canvas."""
    state = _begin_login()
    r = client.get(f"/auth/google/callback?error=access_denied&state={state}")
    assert r.status_code == 302, r.status_code
    assert "/canvas?login=error" in r.headers["location"], r.headers["location"]
    client.cookies.clear()


# ---- CSRF: the state must be a real per-browser token (each of these was a 302 before) ----
def test_callback_without_a_state_is_rejected():
    _begin_login()
    with _google():
        r = client.get("/auth/google/callback?code=abc")
    assert r.status_code == 400, r.status_code
    client.cookies.clear()


def test_callback_with_a_forged_state_is_rejected():
    """The old state was HMAC(expiry) keyed on SP_CONNECTOR_CLIENT_SECRET or the literal
    "dev-secret" — so a Google-only deployment (this one) signed every state with a public
    constant, and anyone could forge one offline."""
    _begin_login()
    with _google():
        r = client.get("/auth/google/callback?code=abc&state=1999999999.deadbeef")
    assert r.status_code == 400, r.status_code
    client.cookies.clear()


def test_callback_with_another_browsers_state_is_rejected():
    """THE attack. The state is minted in the ATTACKER's browser (they simply fetch the
    unauthenticated /auth/google/login and read it out of the Location header) and replayed in
    the VICTIM's browser together with the attacker's `code`. Without the nonce-cookie binding
    the callback vaults the attacker's Google refresh token under the victim's oid, and every
    later Google query the victim runs executes as the attacker."""
    attacker_state = _begin_login()                 # minted in the attacker's browser...
    victim = user_auth.sign_session({"oid": "victim-oid", "name": "V", "email": "v@x.com",
                                     "exp": 2 ** 31})
    client.cookies.clear()                          # ...the victim's browser holds no nonce
    _set_session(victim)
    user_auth.VAULT.drop("victim-oid")
    with _google():
        r = client.get(f"/auth/google/callback?code=attacker-code&state={attacker_state}")
    assert r.status_code == 400, r.status_code
    assert user_auth.VAULT.linked("victim-oid") == [], \
        "CSRF BREACH: the attacker's Google credential was vaulted under the victim's oid"
    client.cookies.clear()


def test_the_csrf_nonce_is_single_use():
    state = _begin_login()
    with _google():
        first = client.get(f"/auth/google/callback?code=abc&state={state}")
        assert first.status_code == 302, first.status_code
        replay = client.get(f"/auth/google/callback?code=abc&state={state}")
    assert replay.status_code == 400, "a spent state must not be replayable"
    client.cookies.clear()


def test_auth_me_reports_linked_clouds():
    user_auth.VAULT.drop("carol@gmail.com")
    user_auth.VAULT.put("carol@gmail.com", RT, idp="google")
    cookie = user_auth.sign_session({"oid": "carol@gmail.com", "name": "C",
                                     "email": "carol@gmail.com", "exp": 2 ** 31})
    client.cookies.clear()
    _set_session(cookie)
    try:
        r = client.get("/auth/me")
    finally:
        client.cookies.clear()
    body = r.json()
    assert body["signed_in"] is True
    assert body["google_enabled"] is True
    assert body["linked"] == ["google"]
    _assert_no_secret_leak(r)          # LAW 1: /auth/me names the cloud, never the token


def test_dev_header_is_fail_closed_when_google_login_is_configured():
    """#183 parity: with a real login configured, the X-DBSearch-User switcher must NEVER
    authenticate — else anyone could redeem a victim's vaulted refresh token as them. This
    test is about the GOOGLE clause, so it first asserts Entra login is NOT enabled: with an
    ambient SP_CONNECTOR_* the 401 could come from the old Entra branch and prove nothing."""
    client.cookies.clear()
    assert not user_auth.is_enabled(), "test setup: Entra login must be OFF here"
    assert google_auth.is_enabled(), "test setup: Google login must be ON here"
    r = client.get("/auth/me", headers={"X-DBSearch-User": "alice@gmail.com"})
    assert r.json()["signed_in"] is False
    r2 = client.post("/router/ask", json={"question": "x"},
                     headers={"X-DBSearch-User": "alice@gmail.com"})
    assert r2.status_code == 401, r2.status_code


def test_subject_provider_routes_by_idp():
    """Fail-closed per idp: dave has a GOOGLE credential only. Asking for entra must raise
    NotSignedIn(idp='entra') from the vault, not fall through to the env dev seam - that
    fallback only applies to a cloud with no real login configured at all, and here we
    configure entra real login too (AUTH_* env, mirroring selftest_devheader_login_exclusion)
    so `_subject_provider`'s per-idp `enabled` check actually consults the vault."""
    auth_vars = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET")
    saved = {k: os.environ.get(k) for k in auth_vars}
    os.environ["AUTH_TENANT_ID"] = "tid"
    os.environ["AUTH_CLIENT_ID"] = "cid"
    os.environ["AUTH_CLIENT_SECRET"] = "sec"
    try:
        user_auth.VAULT.drop("dave@gmail.com")
        user_auth.VAULT.put("dave@gmail.com", "rt-g", idp="google")
        assert app_mod._subject_provider("dave@gmail.com", "google") == "rt-g"
        try:
            app_mod._subject_provider("dave@gmail.com", "entra")
        except user_auth.NotSignedIn as e:
            assert e.idp == "entra"
        else:
            raise AssertionError("an unlinked cloud must fail closed, not fall back")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_env_dev_seam_is_never_substituted_for_another_cloud():
    """LAW 2 / cross-cloud credential leak: DBSEARCH_SUBJECT_TOKEN is an ENTRA assertion by
    construction. With Google login NOT configured, `_subject_provider(oid, "google")` used to
    fall through to it - so a `google_refresh` store (composable by ANY authenticated caller
    via POST /router/compose) would POST that Entra credential to oauth2.googleapis.com: one
    cloud's credential transmitted to another cloud. It must fail closed instead."""
    saved = {k: os.environ.get(k) for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")}
    os.environ.pop("GOOGLE_CLIENT_ID", None)
    os.environ.pop("GOOGLE_CLIENT_SECRET", None)
    os.environ["DBSEARCH_SUBJECT_TOKEN"] = "entra-dev-assertion"
    try:
        assert not google_auth.is_enabled(), "test setup: Google login must be OFF here"
        try:
            got = app_mod._subject_provider("eve@gmail.com", "google")
        except user_auth.NotSignedIn as e:
            assert e.idp == "google", e.idp
        else:
            raise AssertionError(
                f"CROSS-CLOUD LEAK: the entra dev seam was handed to google ({got!r})")
        # the seam still works for the cloud it actually belongs to (backward compat)
        assert app_mod._subject_provider("eve@gmail.com") == "entra-dev-assertion"
    finally:
        os.environ.pop("DBSEARCH_SUBJECT_TOKEN", None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


for fn in [test_login_redirects_to_google, test_login_rejects_an_unknown_channel,
           test_callback_vaults_under_google_and_signs_in,
           test_linking_from_an_existing_session_keeps_that_oid,
           test_callback_without_a_refresh_token_is_an_error_not_a_lie,
           test_callback_surfaces_a_declined_consent,
           test_callback_without_a_state_is_rejected,
           test_callback_with_a_forged_state_is_rejected,
           test_callback_with_another_browsers_state_is_rejected,
           test_the_csrf_nonce_is_single_use,
           test_auth_me_reports_linked_clouds,
           test_dev_header_is_fail_closed_when_google_login_is_configured,
           test_subject_provider_routes_by_idp,
           test_env_dev_seam_is_never_substituted_for_another_cloud]:
    fn()
