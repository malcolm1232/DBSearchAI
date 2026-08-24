"""Per-owner connector connection state (#431, gap 1 of #389).

Which SharePoint tenants has THIS user connected? That was a module-level dict keyed only by
tenant, so every signed-in identity saw every other identity's connections - proven live on prod
when a foreign-tenant stranger's node reported Malcolm's connection as "Connected" - and a restart
wiped it, so a genuine connection read as "Not connected yet" after every deploy.

This holds metadata (a tenant id and a timestamp), NOT a credential: the SharePoint tokens live in
the encrypted vault (#435) and nothing here grants access to anything. So it belongs in the same
Postgres as `user_manifests` rather than the secrets store, and it follows the shape and the
hard-won details of `PgManifestStore` deliberately - see its docstrings for why the DDL runs in its
own committed transaction under a lock.
"""
from __future__ import annotations

import threading

from dbsearch.server.manifest_store import _safe_reason


class InMemoryConnectionStore:
    """Hermetic adapter for tests and rigs without Postgres."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], int] = {}      # (owner, tenant) -> connected_at
        self._lock = threading.Lock()

    def put(self, owner: str, tenant: str, connected_at: int) -> None:
        with self._lock:
            self._rows[(owner, tenant)] = connected_at

    def list(self, owner: str) -> list[dict]:
        with self._lock:
            rows = [(t, ts) for (o, t), ts in self._rows.items() if o == owner]
        return [{"tenant": t, "connected_at": ts} for t, ts in sorted(rows)]

    def delete(self, owner: str, tenant: str) -> None:
        with self._lock:
            self._rows.pop((owner, tenant), None)


class PgConnectionStore:
    """`connector_connections` in the deployment's own compose-managed Postgres.

    Same connect-per-operation and first-touch-DDL idiom as PgManifestStore (which explains at
    length why the CREATE TABLE gets its own committed transaction and why `_schema_done` may only
    be set after it commits: setting it inside the operation's transaction poisoned the process
    for every later call).

    PRIMARY KEY (owner_oid, tenant_id) IS the isolation: a row cannot exist without an owner, so
    there is no shape of this table that can answer "who is connected" without being asked about
    somebody in particular. That is the property #431 was missing.
    """

    def __init__(self, dsn: str, table: str = "connector_connections",
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
        with self._schema_lock:
            if self._schema_done:
                return
            with self._conn() as conn:
                conn.execute(
                    f"""CREATE TABLE IF NOT EXISTS {self._table} (
                        owner_oid    text NOT NULL,
                        tenant_id    text NOT NULL,
                        connected_at bigint NOT NULL,
                        PRIMARY KEY (owner_oid, tenant_id)
                    )""")
            self._schema_done = True

    def _run(self, fn, default=None):
        """Sanitized-and-swallowed on failure, unlike the manifest store which raises.

        Connection state is DISPLAY state: the node's pill and the status endpoint. A Postgres
        blip must degrade to "not connected" (the user reconnects) rather than 500 a canvas load
        or, worse, break the consent callback that is mid-redirect from Microsoft. The reason is
        logged here, sanitized, so an operator still sees it."""
        try:
            self._ensure_schema()
            with self._conn() as conn:
                return fn(conn)
        except Exception as exc:
            import logging
            logging.getLogger("dbsearch").warning(
                "connector_connections store unavailable: %s", _safe_reason(exc))
            self._schema_done = False       # retry the DDL next call
            return default

    def put(self, owner: str, tenant: str, connected_at: int) -> None:
        self._run(lambda conn: conn.execute(
            f"""INSERT INTO {self._table} (owner_oid, tenant_id, connected_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (owner_oid, tenant_id)
                DO UPDATE SET connected_at = EXCLUDED.connected_at""",
            (owner, tenant, connected_at)))

    def list(self, owner: str) -> list[dict]:
        def _q(conn):
            cur = conn.execute(
                f"SELECT tenant_id, connected_at FROM {self._table} "
                f"WHERE owner_oid = %s ORDER BY tenant_id", (owner,))
            return [{"tenant": t, "connected_at": ts} for t, ts in cur.fetchall()]
        return self._run(_q, default=[]) or []

    def delete(self, owner: str, tenant: str) -> None:
        self._run(lambda conn: conn.execute(
            f"DELETE FROM {self._table} WHERE owner_oid = %s AND tenant_id = %s",
            (owner, tenant)))
