"""ApiKeyRegistry — self-serve API keys bound to a user identity.

Concrete in-memory adapter (mirrors connectors/registry.py:SourceRegistry). A key's secret is
stored ONLY as sha256(secret) (LAW 1/6/7) — the full token is returned once at create. resolve()
is the hot auth path: O(1) by id, constant-time secret compare. A Postgres adapter can replace
this class behind the same method set later.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from dbsearch.api.auth import AuthError


@dataclass
class ApiKeyRecord:
    id: str                       # public, e.g. dbk_live_a1b2c3d4
    bound_user: str               # oid the key runs as
    label: str
    created_at: str               # iso
    last_used_at: str | None = None
    request_count: int = 0
    revoked: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class ApiKeyRegistry:
    def __init__(self) -> None:
        self._records: dict[str, ApiKeyRecord] = {}   # id -> record
        self._hashes: dict[str, str] = {}             # id -> sha256(secret)

    def create(self, bound_user: str, label: str) -> tuple[ApiKeyRecord, str]:
        key_id = "dbk_live_" + secrets.token_hex(4)
        secret = secrets.token_urlsafe(32)
        rec = ApiKeyRecord(id=key_id, bound_user=bound_user, label=label, created_at=_now())
        self._records[key_id] = rec
        self._hashes[key_id] = _sha(secret)
        return rec, f"{key_id}.{secret}"

    def resolve(self, token: str) -> str:
        key_id, _, secret = token.partition(".")
        rec = self._records.get(key_id)
        stored = self._hashes.get(key_id)
        if rec is None or stored is None or rec.revoked:
            raise AuthError("invalid api key")
        if not hmac.compare_digest(stored, _sha(secret)):
            raise AuthError("invalid api key")
        rec.last_used_at = _now()
        rec.request_count += 1
        return rec.bound_user

    def list_for(self, user: str) -> list[ApiKeyRecord]:
        return [r for r in self._records.values() if r.bound_user == user]

    def revoke(self, key_id: str, requesting_user: str) -> None:
        rec = self._records.get(key_id)
        if rec is None or rec.bound_user != requesting_user:
            raise KeyError(key_id)   # missing OR not owned -> 404 (don't leak existence)
        rec.revoked = True
