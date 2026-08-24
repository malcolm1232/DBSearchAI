"""Typed provenance — the "show your proof" contract (#165).

Every citation normalizes into ONE of three proof shapes; the contract is enforced
by selftest_router_contracts so future connectors (GCP/AWS) cannot drift. Rerun
tokens are HMAC-signed server-side and USER-BOUND: only server-issued SQL can
re-execute, and only for the user it was issued to (LAW 2). Stateless by design —
a restart invalidates tokens; the remedy is re-asking the question.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field

from dbsearch.router.evidence import Evidence

PROOF_SQL = "sql"
PROOF_DOCUMENT = "document"
PROOF_NATIVE = "native"

# env-pinned for multi-process deploys; per-process fallback keeps dev/tests zero-config
_KEY = os.environ.get("DBSEARCH_PROOF_KEY") or secrets.token_hex(32)


class ProvenanceError(ValueError):
    """Evidence whose provenance can't be classified — a contract violation."""


@dataclass
class SqlProof:
    sql: str
    table: str
    store_id: str
    row_ids: list = field(default_factory=list)
    rerun_token: str = ""            # stamped at the API boundary (user-bound)

    def to_dict(self) -> dict:
        return {"kind": PROOF_SQL, "sql": self.sql, "table": self.table,
                "store_id": self.store_id, "row_ids": self.row_ids,
                "rerun_token": self.rerun_token}


@dataclass
class DocProof:
    uri: str
    title: str
    doc: str
    store_id: str
    locator: str = ""

    def to_dict(self) -> dict:
        return {"kind": PROOF_DOCUMENT, "uri": self.uri, "title": self.title,
                "doc": self.doc, "store_id": self.store_id, "locator": self.locator}


@dataclass
class NativeProof:
    source: str
    store_id: str

    def to_dict(self) -> dict:
        return {"kind": PROOF_NATIVE, "source": self.source, "store_id": self.store_id}


def normalize_proof(ev: Evidence):
    """Classify by provenance content (total over today's stores; loud otherwise)."""
    prov = ev.provenance or {}
    if "sql" in prov:
        return SqlProof(sql=str(prov.get("sql", "")), table=str(prov.get("table", "")),
                        store_id=ev.store_id, row_ids=list(prov.get("row_ids") or []))
    if "doc" in prov or "uri" in prov or "title" in prov:
        return DocProof(uri=str(prov.get("uri", "")), title=str(prov.get("title", "")),
                        doc=str(prov.get("doc", "")), store_id=ev.store_id,
                        locator=str(prov.get("locator", "")))
    if "source" in prov:
        return NativeProof(source=str(prov.get("source", "")), store_id=ev.store_id)
    raise ProvenanceError(
        f"store {ev.store_id!r} kind {ev.kind!r}: unclassifiable provenance {sorted(prov)}")


def _msg(store_id: str, sql: str, user_oid: str) -> bytes:
    return f"{store_id}\n{user_oid}\n{sql}".encode()


def sign_rerun(store_id: str, sql: str, user_oid: str, key: str | None = None) -> str:
    k = (key or _KEY).encode()
    return hmac.new(k, _msg(store_id, sql, user_oid), hashlib.sha256).hexdigest()


def verify_rerun(store_id: str, sql: str, user_oid: str, token: str,
                 key: str | None = None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(sign_rerun(store_id, sql, user_oid, key), token)
