"""#368: per-owner manifest persistence - the server, not the canvas, is the system of
record for what a user has composed.

Stores the RAW manifest (the exact dict the canvas POSTs to /router/compose), which by
construction carries only the three legal value forms of ADR 0010 s2 - a `secret://`
handle at rest here is safe (it is not a capability; resolution re-checks the owner), and
the literal-in-secret-field guard (secret_fields.py) refuses the fourth, illegal form
before anything reaches this table.

Unconfigured (no DSN) is not an error - the pool just runs memory-only, today's
lifecycle. Configured-but-broken IS an error (ManifestStoreUnavailable), which the
router maps to a 503: an empty workspace served in place of a failing lookup would read
as "your stores are gone" (the #200 lesson).
"""
from __future__ import annotations

import json
import logging
import threading


class ManifestStoreUnavailable(Exception):
    """The manifest store is configured but cannot be reached or queried.

    Its message is deliberately DATA-FREE - an exception class name plus, where the driver
    offers one, the five-character SQLSTATE. It used to be `str(psycopg_error)`, and a
    psycopg message can quote the offending value: a jsonb write rejected for an embedded
    NUL byte, or a check/size violation, echoes back a fragment of the manifest - which is
    exactly where a `delegation.client_secret` lives. Nothing reads that message today, but
    it was one `logging.exception` away from printing a credential (LAW 1), and "safe only
    because no caller logs it" is not an invariant. The full driver error is not silently
    lost: `_run` logs the same sanitized reason itself, which is what an operator needs
    (which operation, which class of failure) without the payload.
    """


def _safe_reason(exc: BaseException) -> str:
    """A data-free description of `exc`: its class, plus a SQLSTATE when psycopg supplies
    one. Never `str(exc)` - see ManifestStoreUnavailable. Only exception TYPES and codes
    cross this boundary, so no manifest value can ride out on it regardless of which driver
    error fired or how a future caller renders it."""
    sqlstate = getattr(exc, "sqlstate", None)
    return (f"{type(exc).__name__} (sqlstate {sqlstate})" if sqlstate
            else type(exc).__name__)


class InMemoryManifestStore:
    """Hermetic adapter for tests and rigs without Postgres."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def get(self, owner: str) -> "dict | None":
        row = self._rows.get(owner)
        return json.loads(json.dumps(row)) if row is not None else None

    def put(self, owner: str, manifest: dict) -> None:
        self._rows[owner] = json.loads(json.dumps(manifest))

    def delete(self, owner: str) -> None:
        self._rows.pop(owner, None)


class PgManifestStore:
    """user_manifests in the deployment's own compose-managed Postgres (PGVECTOR_DSN).

    Connects per operation: manifest reads/writes are rare (compose, first request after
    a restart), and a held connection is one more thing that dies with the DB and has to
    be liveness-checked (#359's exact failure shape on QM). DDL is CREATE TABLE IF NOT
    EXISTS on first touch, the repo idiom (pgvector.py does the same).
    """

    def __init__(self, dsn: str, table: str = "user_manifests",
                 connect_timeout: int = 5) -> None:
        self._dsn = dsn
        self._table = table
        self._timeout = connect_timeout
        self._schema_done = False
        self._schema_lock = threading.Lock()

    def _conn(self):
        import psycopg
        return psycopg.connect(self._dsn, connect_timeout=self._timeout)

    def _ensure_schema(self) -> None:
        """CREATE TABLE in its OWN committed transaction, before the operation runs.

        `_schema_done` is set only AFTER that transaction has committed. It used to be set
        INSIDE the operation's transaction, which made one failed write poison the whole
        process: psycopg's `with conn` rolls back on any exception and PostgreSQL DDL is
        transactional, so a first operation that raised took the CREATE TABLE down with it
        while the flag stayed True. Every later call skipped the DDL and died on
        `relation "user_manifests" does not exist` - forever, for every user, since app.py
        holds ONE module-level store and the router maps the failure to a 503. Reachable
        from ordinary user data: a NUL byte or lone surrogate anywhere in the manifest
        (jsonb rejects \\u0000, and #367 puts one there), a statement timeout, or a
        connection dropped mid-transaction.

        Under the lock so two threads cannot race the DDL: concurrent
        CREATE TABLE IF NOT EXISTS is not safe in PostgreSQL (one side can still raise
        duplicate_table), and that raise would surface as a spurious 503.
        """
        with self._schema_lock:
            if self._schema_done:
                return
            with self._conn() as conn:
                conn.execute(
                    f"""CREATE TABLE IF NOT EXISTS {self._table} (
                        owner_oid   text PRIMARY KEY,
                        manifest    jsonb NOT NULL,
                        updated_at  timestamptz NOT NULL DEFAULT now()
                    )""")
            # Leaving the `with` above committed and closed the connection: only now is the
            # table durable, so only now may the flag be set. Anything raised before this
            # line leaves it False and the next call retries the DDL.
            self._schema_done = True

    def _run(self, fn):
        try:
            self._ensure_schema()
            with self._conn() as conn:
                return fn(conn)
        except Exception as exc:
            reason = _safe_reason(exc)
            # Logged HERE, sanitized, so an operator still sees that the store failed and
            # roughly how - without any caller having to decide whether the driver's own
            # message is safe to print (it is not: verified against the live Postgres, a
            # rejected jsonb write comes back as `... "config": {"description": "trailing
            # NUL ...` - a fragment of the manifest, and a manifest is where
            # `delegation.client_secret` lives).
            logging.getLogger("dbsearch").error(
                "manifest store %s unavailable: %s", self._table, reason)
        # Raised OUTSIDE the except block on purpose. `raise X from exc` keeps the driver
        # error as __cause__ and `raise X from None` still keeps it as __context__; either
        # way a traceback render of THIS exception can reach the driver's message. Raising
        # here, after the handler has exited (so sys.exc_info() is cleared), leaves both
        # __cause__ and __context__ None - there is no path from this exception back to the
        # payload, whatever a future caller does with it.
        raise ManifestStoreUnavailable(reason)

    def get(self, owner: str) -> "dict | None":
        def _get(conn):
            row = conn.execute(
                f"SELECT manifest FROM {self._table} WHERE owner_oid = %s",
                (owner,)).fetchone()
            return row[0] if row else None
        return self._run(_get)

    def put(self, owner: str, manifest: dict) -> None:
        def _put(conn):
            conn.execute(
                f"""INSERT INTO {self._table} (owner_oid, manifest, updated_at)
                    VALUES (%s, %s::jsonb, now())
                    ON CONFLICT (owner_oid) DO UPDATE SET
                      manifest = EXCLUDED.manifest, updated_at = now()""",
                (owner, json.dumps(manifest)))
        self._run(_put)

    def delete(self, owner: str) -> None:
        self._run(lambda conn: conn.execute(
            f"DELETE FROM {self._table} WHERE owner_oid = %s", (owner,)))
