"""Real per-user Google sign-in / account linking (#193).

Confidential authorization-code flow, the sibling of user_auth.py's Entra leg: the code is
exchanged server-side at Google's token endpoint (TLS + client secret), so the returned
id_token is *transport-trusted* - we read the verified `sub`/`email` from its payload without
a separate JWKS signature check (the token came directly from Google, not from the browser).

Session identity for a Google sign-in is the VERIFIED EMAIL (spec §3.2): BigQuery IAM,
row-access policies (SESSION_USER()), Cloud SQL IAM users and Drive ACLs all key on the email,
so the session oid, the store ACL and the source-side policy all read the same string.
`email_verified: false` is refused outright - an unverified claim must never become an identity.

Least privilege is INCREMENTAL (ADR 0006 addendum): Google does not scope refresh redemption
per resource, so the refresh token carries whatever was consented. We therefore request only
the channels being connected, and re-consent with include_granted_scopes=true to add more.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse

from dbsearch.server.sp_connect import http_post_form

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

BASE_SCOPES = ["openid", "email", "profile"]

# channel -> the ONE delegated scope that channel's queries need (spec §3.3)
CHANNEL_SCOPES = {
    # Running a QUERY is a jobs.insert, which bigquery.readonly does NOT authorize (403 "insufficient
    # authentication scopes"). The full `bigquery` scope is required to create jobs; read-only is
    # then enforced by IAM (dataViewer, no dataEditor) + row-access policies, not by the OAuth scope.
    "bigquery": "https://www.googleapis.com/auth/bigquery",
    "cloudsql": "https://www.googleapis.com/auth/sqlservice.login",
    "firestore": "https://www.googleapis.com/auth/datastore",
    "drive": "https://www.googleapis.com/auth/drive.readonly",
    "gcs": "https://www.googleapis.com/auth/devstorage.read_only",
}

# channels linked by default when the canvas has not said otherwise
DEFAULT_CHANNELS = ["bigquery"]


def _cfg(name: str) -> str:
    return os.environ.get(name, "")


def client_id() -> str:
    return _cfg("GOOGLE_CLIENT_ID")


def client_secret() -> str:
    return _cfg("GOOGLE_CLIENT_SECRET")


def redirect_uri() -> str:
    return _cfg("GOOGLE_REDIRECT_URI") or "http://localhost:8080/auth/google/callback"


def is_enabled() -> bool:
    return bool(client_id() and client_secret())


def scopes_for(channels: "list[str] | None" = None) -> str:
    """Raise on an unknown channel - never silently drop it. A dropped channel consents to
    the BASE scopes only, so the link "succeeds", a refresh token with no data scopes is
    vaulted, and every later query fails opaquely at Google with no trace of the typo."""
    wanted = list(channels or DEFAULT_CHANNELS)
    unknown = [c for c in wanted if c not in CHANNEL_SCOPES]
    if unknown:
        raise ValueError(f"unknown google channel(s): {', '.join(unknown)} "
                         f"(known: {', '.join(sorted(CHANNEL_SCOPES))})")
    return " ".join(BASE_SCOPES + [CHANNEL_SCOPES[c] for c in wanted])


def login_url(state: str, *, channels: "list[str] | None" = None,
              incremental: bool = False) -> str:
    q = {
        "client_id": client_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "scope": scopes_for(channels),
        "state": state,
        "access_type": "offline",   # without this Google returns NO refresh token
        "prompt": "consent",        # without this a re-link returns no refresh token
    }
    if incremental:
        q["include_granted_scopes"] = "true"   # keep scopes granted on earlier links
    return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"


def _jwt_payload(jwt: str) -> dict:
    part = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def exchange_code(code: str, post=http_post_form) -> dict:
    """Exchange the auth code for tokens; return {sub, email, name, refresh_token}."""
    r = post(TOKEN_URL, {
        "client_id": client_id(),
        "client_secret": client_secret(),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
    })
    idt = r.get("id_token")
    if not idt:
        raise RuntimeError(r.get("error_description") or r.get("error")
                           or "google token exchange failed")
    c = _jwt_payload(idt)
    if not c.get("email_verified"):
        raise RuntimeError("google account has no verified email - refusing to make it "
                           "a session identity")
    email = (c.get("email") or "").strip()
    if not email:
        # email_verified with no email at all: the identity string IS the session oid, the
        # vault key and the ACL subject, so an empty one would vault a credential under ""
        # and mint a cookie for the empty user. Refuse (LAW 2).
        raise RuntimeError("google id_token carries no email - refusing to make it "
                           "a session identity")
    return {
        "sub": c.get("sub", ""),
        "email": email,
        "name": c.get("name") or email,
        "refresh_token": r.get("refresh_token", ""),
    }


__all__ = ["is_enabled", "client_id", "client_secret", "redirect_uri", "scopes_for",
           "login_url", "exchange_code", "CHANNEL_SCOPES", "DEFAULT_CHANNELS"]
