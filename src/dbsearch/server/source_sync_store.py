"""#883: durable per-source sync-state, so a deploy stops wiping the source registry.

`SourceRegistry` held cursor/last_sync_at/doc_count/status in compute. A restart erased it,
so the canvas connector node read "never-synced / 0 docs" after every single deploy while
pgvector still answered questions from the very corpus the node claimed did not exist. The
owner re-ran a full crawl once because of that zero. LAW 6: durable state belongs in a
managed service, and jobs, grants, shares, manifests and conversations are already here.

Two things live in a row, and they are different in kind:

  * the six mutable sync fields (cursor, last_sync_at, doc_count, unreadable, status,
    job_tenant) - what the last completed crawl found;
  * `config`, the connector BUILD recipe (az_tenant_id, drive_id, folder_path, owner_oid).

The config half is not an optimisation. `sharepoint:<tid>` is registered only inside
`connect_sharepoint`, and its drive_id/folder arrive on that one HTTP request and were
stored nowhere - so after a restart the source did not exist at all and there was nothing to
rebuild a connector from. Persisting sync fields alone would rehydrate a descriptor with a
hole where its connector goes, which `resync_source` would then trip over.

NO CREDENTIAL IS PERSISTED HERE. Client id/secret come from the deployment env at rebuild
time, exactly as they do on the connect path; this table holds identifiers, not capability.

Failure policy: SWALLOW and log, the `PgConnectionStore` stance rather than the manifest
store's raise. Sync-state is display state plus a resume optimisation - a lost row degrades
to today's behaviour (a "0" and a full re-crawl, which is wasteful but never wrong), while a
raise would 500 a canvas load over a cosmetic count. The one caller that must not be
degraded quietly is the crawl commit, and it logs.

Cursor timing is NOT this module's decision (ADR 0016): the store writes whatever the
registry hands it, and the registry only records a cursor from `record_sync`, which runs
inside the ingest job's `_commit`. Persisting a cursor any earlier silently skips every
unprocessed item in the batch.
"""
from __future__ import annotations

import json
import threading

from dbsearch.server.manifest_store import _safe_reason

# The columns a row carries, in the order the SQL writes them. `scope` and `source_id` are
# the key and are passed separately, so they are not in here.
_FIELDS = ("kind", "display_name", "cursor", "last_sync_at", "doc_count", "unreadable",
           "status", "job_tenant", "config")


class InMemorySourceSyncStore:
    """Hermetic adapter for tests and rigs without Postgres.

    Deep-copies through json like `InMemoryManifestStore` does, because `config` is a mutable
    dict: handing the caller the stored object would let a later mutation of a descriptor
    rewrite history that is supposed to be a snapshot of what was persisted.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()

    def get(self, scope: str, source_id: str) -> "dict | None":
        with self._lock:
            row = self._rows.get((scope, source_id))
            return json.loads(json.dumps(row)) if row is not None else None

    def put(self, scope: str, source_id: str, row: dict) -> None:
        with self._lock:
            self._rows[(scope, source_id)] = json.loads(json.dumps(row))

    def list(self, scope: str) -> list[dict]:
        with self._lock:
            rows = [(sid, r) for (sc, sid), r in self._rows.items() if sc == scope]
        return [dict(json.loads(json.dumps(r)), source_id=sid) for sid, r in sorted(rows)]

    def delete(self, scope: str, source_id: str) -> None:
        with self._lock:
            self._rows.pop((scope, source_id), None)


class PgSourceSyncStore:
    """`source_sync_state` in the deployment's own compose-managed Postgres.

    Same connect-per-operation and first-touch-DDL idiom as PgManifestStore, whose docstrings
    explain at length why the CREATE TABLE gets its OWN committed transaction and why
    `_schema_done` may only be set after it commits (setting it inside the operation's
    transaction poisoned the process for every later call).

    PRIMARY KEY (scope, source_id), not source_id alone. The edition rail has one registry per
    deployment so a bare source_id would work today, but #565 is the same table shape learned
    the hard way on ingest_jobs: two workspaces naming a store `hr-docs` were handed each
    other's job, and a resume SKIPS the documents that job recorded. A key that cannot express
    the partition is a hole waiting for the second writer.
    """

    def __init__(self, dsn: str, table: str = "source_sync_state",
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
        """CREATE TABLE in its own committed transaction, before the operation runs.

        `_schema_done` is set only AFTER that transaction commits - see PgManifestStore's
        `_ensure_schema` for the failure this prevents. Under the lock because concurrent
        CREATE TABLE IF NOT EXISTS is not safe in PostgreSQL (one side can still raise
        duplicate_table), and the ingest worker threads and request threads both land here.

        Later columns must arrive as ALTER TABLE ... ADD COLUMN IF NOT EXISTS in this same
        transaction: CREATE TABLE IF NOT EXISTS is a no-op against a deployed box that already
        has the table, so a column added to the CREATE alone would never exist in prod.
        """
        with self._schema_lock:
            if self._schema_done:
                return
            with self._conn() as conn:
                conn.execute(
                    f"""CREATE TABLE IF NOT EXISTS {self._table} (
                        scope         text NOT NULL,
                        source_id     text NOT NULL,
                        kind          text NOT NULL,
                        display_name  text NOT NULL,
                        cursor        text,
                        last_sync_at  text,
                        doc_count     integer NOT NULL DEFAULT 0,
                        unreadable    integer NOT NULL DEFAULT 0,
                        status        text NOT NULL DEFAULT 'idle',
                        job_tenant    text NOT NULL DEFAULT '',
                        config        jsonb,
                        updated_at    timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (scope, source_id)
                    )""")
            # The `with` above committed and closed: only now is the table durable, so only
            # now may the flag be set. Anything raised before this line leaves it False and
            # the next call retries the DDL.
            self._schema_done = True

    def _run(self, fn, default=None):
        """Sanitized-and-swallowed on failure, unlike the manifest store which raises.

        See the module docstring: sync-state is display state plus a resume optimisation, so a
        Postgres blip must degrade to the pre-#883 behaviour rather than 500 a canvas load or
        take down the crawl commit it is called from. The reason is logged, sanitized, so an
        operator still sees which operation failed and how."""
        try:
            self._ensure_schema()
            with self._conn() as conn:
                return fn(conn)
        except Exception as exc:
            import logging
            logging.getLogger("dbsearch").warning(
                "source_sync_state store unavailable: %s", _safe_reason(exc))
            self._schema_done = False       # retry the DDL next call
            return default

    def get(self, scope: str, source_id: str) -> "dict | None":
        def _q(conn):
            cur = conn.execute(
                f"SELECT {', '.join(_FIELDS)} FROM {self._table} "
                f"WHERE scope = %s AND source_id = %s", (scope, source_id))
            row = cur.fetchone()
            return dict(zip(_FIELDS, row)) if row is not None else None
        return self._run(_q)

    def put(self, scope: str, source_id: str, row: dict) -> None:
        from psycopg.types.json import Jsonb

        values = [row.get(f) for f in _FIELDS]
        values[_FIELDS.index("config")] = (
            Jsonb(row["config"]) if row.get("config") is not None else None)
        cols = ", ".join(_FIELDS)
        placeholders = ", ".join(["%s"] * len(_FIELDS))
        updates = ", ".join(f"{f} = EXCLUDED.{f}" for f in _FIELDS)
        self._run(lambda conn: conn.execute(
            f"""INSERT INTO {self._table} (scope, source_id, {cols}, updated_at)
                VALUES (%s, %s, {placeholders}, now())
                ON CONFLICT (scope, source_id)
                DO UPDATE SET {updates}, updated_at = now()""",
            (scope, source_id, *values)))

    def list(self, scope: str) -> list[dict]:
        def _q(conn):
            cur = conn.execute(
                f"SELECT source_id, {', '.join(_FIELDS)} FROM {self._table} "
                f"WHERE scope = %s ORDER BY source_id", (scope,))
            return [dict(zip(("source_id", *_FIELDS), r)) for r in cur.fetchall()]
        return self._run(_q, default=[]) or []

    def delete(self, scope: str, source_id: str) -> None:
        """Unused by the product today, and deliberately present.

        Nothing sweeps this table: retention.py touches no store tables and the router's
        delete_store never reaches a per-source row (#907). This is the seam that card needs,
        and a store whose only writer is an upsert is a store that can only ever grow."""
        self._run(lambda conn: conn.execute(
            f"DELETE FROM {self._table} WHERE scope = %s AND source_id = %s",
            (scope, source_id)))
