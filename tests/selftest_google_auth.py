"""#193 - Google authorization-code leg: scope selection (incremental auth), login URL
shape (offline + consent so a refresh token is actually returned), and code exchange
reading the transport-trusted id_token.

Run: PYTHONPATH=src python3 tests/selftest_google_auth.py
"""
import base64
import json
import os
import sys
from pathlib import Path
from urllib import parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.server import google_auth  # noqa: E402


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "hdr." + body + ".sig"


def test_disabled_without_config():
    os.environ.pop("GOOGLE_CLIENT_ID", None)
    os.environ.pop("GOOGLE_CLIENT_SECRET", None)
    assert google_auth.is_enabled() is False


def test_scopes_are_incremental_per_channel():
    bq = google_auth.scopes_for(["bigquery"])
    # the FULL bigquery scope, not bigquery.readonly: running a query is a jobs.insert that
    # readonly cannot authorize (403). Read-only stays enforced by IAM, not the scope.
    assert "https://www.googleapis.com/auth/bigquery" in bq
    assert "bigquery.readonly" not in bq
    assert "openid" in bq and "email" in bq
    assert "drive.readonly" not in bq          # least privilege: not requested until Drive
    both = google_auth.scopes_for(["bigquery", "drive"])
    assert "drive.readonly" in both and "https://www.googleapis.com/auth/bigquery" in both


def test_login_url_asks_for_a_refresh_token():
    os.environ["GOOGLE_CLIENT_ID"] = "cid"
    os.environ["GOOGLE_CLIENT_SECRET"] = "csec"
    os.environ["GOOGLE_REDIRECT_URI"] = "http://localhost:8080/auth/google/callback"
    assert google_auth.is_enabled() is True
    q = dict(parse.parse_qsl(parse.urlsplit(google_auth.login_url("st8")).query))
    assert q["access_type"] == "offline"       # without this: no refresh token
    assert q["prompt"] == "consent"            # without this: no refresh token on re-link
    assert q["client_id"] == "cid"
    assert q["state"] == "st8"
    assert q["redirect_uri"] == "http://localhost:8080/auth/google/callback"
    assert "include_granted_scopes" not in q   # first link: not incremental

    inc = dict(parse.parse_qsl(parse.urlsplit(
        google_auth.login_url("st8", channels=["drive"], incremental=True)).query))
    assert inc["include_granted_scopes"] == "true"   # keeps previously granted scopes


def test_exchange_code_reads_verified_identity_and_refresh_token():
    os.environ["GOOGLE_CLIENT_ID"] = "cid"
    os.environ["GOOGLE_CLIENT_SECRET"] = "csec"
    seen = {}

    def fake_post(url, form):
        seen["url"] = url
        seen["form"] = form
        return {"id_token": _jwt({"sub": "108", "email": "alice@gmail.com",
                                  "email_verified": True, "name": "Alice"}),
                "refresh_token": "rt-google", "access_token": "at"}

    u = google_auth.exchange_code("code-123", post=fake_post)
    assert seen["url"] == "https://oauth2.googleapis.com/token"
    assert seen["form"]["grant_type"] == "authorization_code"
    assert seen["form"]["code"] == "code-123"
    assert u == {"sub": "108", "email": "alice@gmail.com", "name": "Alice",
                 "refresh_token": "rt-google"}


def test_exchange_code_rejects_unverified_email():
    def fake_post(url, form):
        return {"id_token": _jwt({"sub": "1", "email": "spoof@gmail.com",
                                  "email_verified": False, "name": "S"}),
                "refresh_token": "rt"}
    try:
        google_auth.exchange_code("c", post=fake_post)
    except RuntimeError as e:
        assert "verified" in str(e)
    else:
        raise AssertionError("unverified email must not become a session identity")


def test_exchange_code_surfaces_google_error():
    def fake_post(url, form):
        return {"error": "invalid_grant", "error_description": "bad code"}
    try:
        google_auth.exchange_code("c", post=fake_post)
    except RuntimeError as e:
        assert "bad code" in str(e)
    else:
        raise AssertionError("token-endpoint error must raise")


for fn in [test_disabled_without_config, test_scopes_are_incremental_per_channel,
           test_login_url_asks_for_a_refresh_token,
           test_exchange_code_reads_verified_identity_and_refresh_token,
           test_exchange_code_rejects_unverified_email,
           test_exchange_code_surfaces_google_error]:
    fn()
