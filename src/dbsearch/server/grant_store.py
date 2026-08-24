"""#575 (second half of ADR 0017): durable grants, so a share survives a deploy.

ADR 0017 s4 named this the deferred piece - the grant registry lived in memory only, the
same position `ApiKeyRegistry` held, because durability is a storage decision that
should not be taken while the sharing model is new. The model has settled; this closes it.

The store is write-through/delete-through PLUS a boot-time hydration, never a read path:
`GrantRegistry._by_id` (grants.py) stays the ONLY thing per-request principal expansion
touches, so sharing does not trade "instantaneous revocation, no sweeper" for a database
round trip on every question. This file only has to keep that dict truthful across a
restart - the enforcement story in grants.py's own module docstring is unchanged.

Unconfigured (no DSN) runs memory-only, the same contract manifest_store.py and
accounts.py already use. Configured-but-broken is NOT uniformly best-effort - `GrantRegistry`
(grants.py) makes the call per operation, not per store, and the two directions disagree on
purpose (#575 review, Finding B corrects an earlier version of this paragraph that claimed
otherwise). `load`/`save` are TokenVault's stance: a store that is down must not turn a
working share into a 500, so a failed hydration or a failed `create`-time `save` is swallowed
and logged - a grant born while the store is unreachable simply does not outlive THIS
process. `delete` is NOT best-effort when it backs a `revoke`: a swallowed delete failure
would leave a "revoked" grant's row alive in Postgres, ready to be handed back out as a live,
expandable principal by the next restart's hydration - the opposite of what revoking it was
for. `GrantRegistry.revoke` calls `store.delete` unguarded and lets it raise; only
`GrantRegistry.discard_unconfirmed` (cleanup for a grant whose share never took effect, not a
reversal of one that did) treats a failed `delete` as best-effort, same as `save`.

#600 adds `conv_id` (nullable, ALTER TABLE ADD COLUMN IF NOT EXISTS so this is idempotent
against the table that already exists on every deployed box) and `GrantRegistry.
drop_for_conversation`, which joins `revoke` in the unguarded-delete camp for the same
reason: it reverses a live authorization decision, not an orphan cleanup.
"""
from __future__ import annotations

import threading
from datetime import timezone

from dbsearch.api.grants import Grant


class GrantStoreUnavailable(Exception):
    """Configured but unreachable. Message is data-free (class name + SQLSTATE only) -
    manifest_store.py's `ManifestStoreUnavailable` docstring explains why never `str(exc)`:
    a driver message can quote a stored value, and `grantee_oid`/`granted_by` live here."""


def _safe_reason(exc: BaseException) -> str:
    sqlstate = getattr(exc, "sqlstate", None)
    return (f"{type(exc).__name__} (sqlstate {sqlstate})" if sqlstate
            else type(exc).__name__)


def _aware(dt):
    """Defend against a naive datetime coming back off a row. `Grant.is_live()` (grants.py)
    compares `expires_at` against `datetime.now(timezone.utc)`, an AWARE value - comparing
    aware to naive raises TypeError, which would turn a live grant's next request into a
    500 rather than a read. A real psycopg connection against a `timestamptz` column already
    returns aware datetimes; this is a defensive floor under that assumption, not a
    correction of a known bug, and it is what makes `_row_to_grant` safe to feed anything
    that merely LOOKS like a timestamptz row (including a test double's naive datetime)."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def _row_to_grant(row) -> Grant:
    (grant_id, doc_external_id, tenant_id, grantee_oid, granted_by,
     expires_at, created_at, conv_id) = row
    return Grant(grant_id=grant_id, doc_external_id=doc_external_id, tenant_id=tenant_id,
                 grantee_oid=grantee_oid, granted_by=granted_by,
                 expires_at=_aware(expires_at), created_at=_aware(created_at),
                 conv_id=conv_id)


class InMemoryGrantStore:
    """Hermetic adapter for tests and rigs without Postgres - also the shape `GrantRegistry`
    defaults to needing none of, since `store=None` is its own no-store path (grants.py).
    This class exists so a test can exercise the write-through/hydrate contract without a
    live Postgres: a dict keyed by grant_id, holding the `Grant` dataclasses themselves
    (frozen, so no copy-on-write is needed the way manifest_store.py needs one for a mutable
    dict payload)."""

    def __init__(self) -> None:
        self._rows: dict[str, Grant] = {}
        self._lock = threading.Lock()

    def load_all(self) -> list[Grant]:
        with self._lock:
            return list(self._rows.values())

    def save(self, g: Grant) -> None:
        with self._lock:
            self._rows[g.grant_id] = g

    def delete(self, grant_id: str) -> None:
        with self._lock:
            self._rows.pop(grant_id, None)


class PgGrantStore:
    """doc_grants in the deployment's own compose-managed Postgres (PGVECTOR_DSN).

    Connects per operation - manifest_store.py's `PgManifestStore` docstring explains why
    (a held connection is one more thing that dies with the DB and has to be liveness
    checked). DDL is CREATE TABLE IF NOT EXISTS on first touch, `_schema_done` set only
    after that transaction commits - manifest_store.py's `_ensure_schema` docstring is the
    full account of why each half of that matters; this is the same pattern verbatim.
    """

    def __init__(self, dsn: str, table: str = "doc_grants", connect_timeout: int = 5) -> None:
        self._dsn = dsn
        self._table = table
        self._timeout = connect_timeout
        self._schema_done = False
        self._schema_lock = threading.Lock()

    def _conn(self):
        import psycopg
        return psycopg.connect(self._dsn, connect_timeout=self._timeout)

    def _ensure_schema(self) -> None:
        with self._schema_lock:
            if self._schema_done:
                return
            with self._conn() as conn:
                conn.execute(
                    f"""CREATE TABLE IF NOT EXISTS {self._table} (
                        grant_id        text PRIMARY KEY,
                        doc_external_id text NOT NULL,
                        tenant_id       text NOT NULL,
                        grantee_oid     text NOT NULL,
                        granted_by      text NOT NULL,
                        expires_at      timestamptz,
                        created_at      timestamptz NOT NULL,
                        conv_id         text
                    )""")
                # #600: the table already exists on every deployed box, so the CREATE above
                # is a no-op there - this is what actually adds the column. IF NOT EXISTS
                # makes it idempotent on a fresh DB too (the CREATE just made the column, so
                # this finds it already present and does nothing), which is the same
                # both-directions idempotence manifest_store.py's _ensure_schema documents.
                conn.execute(
                    f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS conv_id text")
            self._schema_done = True

    def _run(self, fn):
        try:
            self._ensure_schema()
            with self._conn() as conn:
                return fn(conn)
        except Exception as exc:
            reason = _safe_reason(exc)
            import logging
            logging.getLogger("dbsearch").error(
                "grant store %s unavailable: %s", self._table, reason)
        # Raised outside the except block - manifest_store.py's `_run` docstring explains why:
        # it keeps this exception's __cause__/__context__ clear of the driver error, so a
        # future traceback render of THIS exception cannot echo a grantee oid back out.
        raise GrantStoreUnavailable(reason)

    def load_all(self) -> list[Grant]:
        def _load(conn):
            rows = conn.execute(
                f"""SELECT grant_id, doc_external_id, tenant_id, grantee_oid, granted_by,
                           expires_at, created_at, conv_id FROM {self._table}""").fetchall()
            return [_row_to_grant(r) for r in rows]
        return self._run(_load)

    def save(self, g: Grant) -> None:
        def _save(conn):
            conn.execute(
                f"""INSERT INTO {self._table}
                    (grant_id, doc_external_id, tenant_id, grantee_oid, granted_by,
                     expires_at, created_at, conv_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (grant_id) DO UPDATE SET
                      doc_external_id = EXCLUDED.doc_external_id,
                      tenant_id = EXCLUDED.tenant_id,
                      grantee_oid = EXCLUDED.grantee_oid,
                      granted_by = EXCLUDED.granted_by,
                      expires_at = EXCLUDED.expires_at,
                      created_at = EXCLUDED.created_at,
                      conv_id = EXCLUDED.conv_id""",
                (g.grant_id, g.doc_external_id, g.tenant_id, g.grantee_oid, g.granted_by,
                 g.expires_at, g.created_at, g.conv_id))
        self._run(_save)

    def delete(self, grant_id: str) -> None:
        self._run(lambda conn: conn.execute(
            f"DELETE FROM {self._table} WHERE grant_id = %s", (grant_id,)))
