"""secret:// handles - the third legal manifest value form (ADR 0010 s2/s5).

    secret://<tenant_id>/<owner_oid>/<store_id>/<field>

The handle is NOT a capability: possessing it grants nothing. Resolution compares the
handle's tenant and owner against the REQUESTING context and refuses on any mismatch,
before touching the secret store at all. So a manifest that leaks into another tenant
carries dead references rather than credentials (LAW 5, LAW 2 default-deny).
"""
from __future__ import annotations

import re

SECRET_PREFIX = "secret://"

# This module IS the security boundary (ADR 0010 s5): a handle is scoped by literal
# string comparison of its segments, so "no '/'" alone is too permissive. Unicode
# confusable separators (e.g. U+FF0F fullwidth solidus) are not ASCII '/' as sent, but a
# downstream NFKC normalization (a web framework, a JSON/YAML loader, a logging pipeline)
# can turn one into a real '/' and reparse into a DIFFERENTLY-SCOPED 4-part handle. This
# module must not depend on "no caller ever normalizes" - so every segment is restricted
# to a conservative ASCII-only safe charset that has no normalization-form collisions with
# '/' or with each other, control characters and whitespace are excluded outright, and
# percent-encoding is excluded so a segment can never smuggle an escaped separator.
#
# The charset covers every real value this system uses: tenant ids ("acme", "acme-demo"),
# Entra object ids (UUIDs, e.g. "82d85111-cacc-46fa-b02d-465b437aa224"), store ids
# ("sales-db", "hr-wiki", "support-tickets"), and field names ("password", "note").
_SAFE_SEGMENT = re.compile(r"\A[A-Za-z0-9._-]+\Z")

# Comfortably above a UUID (36 chars) with room for realistic compound ids
# (e.g. "<uuid>.<suffix>") while still being small enough that an oversized segment
# cannot be used to smuggle bulk/binary content into a security-sensitive handle.
_MAX_SEGMENT_LEN = 128


def _is_safe_segment(part) -> bool:
    return (
        isinstance(part, str)
        and 0 < len(part) <= _MAX_SEGMENT_LEN
        and _SAFE_SEGMENT.match(part) is not None
    )


def format_handle(tenant: str, owner: str, store: str, field: str) -> str:
    for part in (tenant, owner, store, field):
        if not _is_safe_segment(part):
            raise ValueError(
                f"handle part must match [A-Za-z0-9._-]+ and be at most "
                f"{_MAX_SEGMENT_LEN} chars: {part!r}")
    return f"{SECRET_PREFIX}{tenant}/{owner}/{store}/{field}"


def is_handle(value) -> bool:
    return isinstance(value, str) and value.startswith(SECRET_PREFIX)


def parse_handle(value) -> "dict | None":
    """Strict: exactly four safe-charset parts, or None. A partial parse is worse than no
    parse - it would let a short handle match a broader scope than it names."""
    if not is_handle(value):
        return None
    parts = value[len(SECRET_PREFIX):].split("/")
    if len(parts) != 4 or not all(_is_safe_segment(p) for p in parts):
        return None
    return {"tenant": parts[0], "owner": parts[1], "store": parts[2], "field": parts[3]}


class ScopedSecretResolver:
    """Resolves handles for ONE (tenant, owner) context. Default-deny."""

    def __init__(self, secrets, tenant: str, owner: str) -> None:
        self._secrets = secrets
        self._tenant = tenant
        self._owner = owner

    def resolve(self, handle: str) -> str:
        parsed = parse_handle(handle)
        if parsed is None:
            raise ValueError(f"malformed secret handle: {handle!r}")
        if parsed["tenant"] != self._tenant or parsed["owner"] != self._owner:
            # Deliberately NOT naming the expected scope - the refusal must not teach a
            # prober whose handle it is.
            raise PermissionError("secret handle does not belong to this caller")
        name = f"{parsed['tenant']}/{parsed['owner']}/{parsed['store']}/{parsed['field']}"
        value = self._secrets.get_secret(name)
        if not value:
            raise KeyError(
                f"no stored credential for {parsed['store']}.{parsed['field']} - "
                "store it first, then compose")
        return value
