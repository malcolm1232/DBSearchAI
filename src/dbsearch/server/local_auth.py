"""#574 - local email/password sign-in, REMOVABLE by design (unset DBSEARCH_LOCAL_AUTH).

The email is a LOGIN HANDLE only. It is unverified, so it must never become a LAW 2
principal, never key a workspace, and never be compared against any source-system ACL.
The session identity for a local account is the opaque acct_* id (ADR 0013 decision 2:
derived from nothing mutable).

scrypt (stdlib, memory-hard) rather than argon2id: the core package has zero
dependencies and keeps them. Parameters follow OWASP's scrypt line (N=2^15, r=8, p=1).

v1 limitations, stated in the spec: no password reset, no verification email (no email
infra yet). Signup discloses "account exists" on a duplicate email - an enumeration
oracle accepted at this scale, revisit with email infra.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets

_N, _R, _P, _DKLEN = 2**15, 8, 1, 32
MIN_PASSWORD_LEN = 8

# Logged at most once per process (see is_enabled()) - a misconfiguration check that
# fires on every request would spam the log without adding information after the first.
_warned_no_signing_key = False


def is_enabled() -> bool:
    """DBSEARCH_LOCAL_AUTH=1 is necessary but NOT sufficient (#574 code review, CRITICAL).

    Sessions are signed with user_auth._key(), which falls back to the public literal
    "dev-secret" when no real secret is configured. Every OTHER session-minting path
    (Entra, Google) is reachable only when a real secret already exists, so that
    fallback was safe by construction until this one: local auth can mint a session
    from a bare password POST with nothing upstream requiring a secret at all. Refuse
    to turn on rather than hand out forgeable sessions - a deployment that believes it
    enabled local login must not silently get one anyone can forge."""
    if os.environ.get("DBSEARCH_LOCAL_AUTH", "") != "1":
        return False
    from dbsearch.server import user_auth      # lazy: avoid a hard import-time cycle

    if user_auth.has_real_signing_key():
        return True
    global _warned_no_signing_key
    if not _warned_no_signing_key:
        logging.getLogger("dbsearch").error(
            "DBSEARCH_LOCAL_AUTH=1 but no real session-signing key is configured "
            "(set DBSEARCH_SESSION_KEY, or configure AUTH_CLIENT_SECRET / "
            "SP_CONNECTOR_CLIENT_SECRET / GOOGLE_CLIENT_SECRET) - local auth is "
            "DISABLED rather than mint forgeable sessions")
        _warned_no_signing_key = True
    return False


def hash_password(password: str, salt: "bytes | None" = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P,
                       maxmem=64 * 1024 * 1024, dklen=_DKLEN)
    return salt.hex(), h.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        _, h = hash_password(password, bytes.fromhex(salt_hex))
    except Exception:
        return False
    return hmac.compare_digest(h, hash_hex)


def valid_email(email: str) -> bool:
    """Deliberately minimal (#574 code review, Finding 5): not an RFC 5322 validator,
    just enough to reject the shapes that would otherwise slip through - any whitespace
    (not just the space character, which let a bare "\\n" through) and anything but
    exactly one "@" with a non-empty part on each side."""
    e = email.strip().lower()
    if not e or len(e) > 254 or any(c.isspace() for c in e):
        return False
    parts = e.split("@")
    return len(parts) == 2 and bool(parts[0]) and bool(parts[1])


def normalize_email(email: str) -> str:
    return email.strip().lower()


__all__ = ["is_enabled", "hash_password", "verify_password", "valid_email",
           "normalize_email", "MIN_PASSWORD_LEN"]
