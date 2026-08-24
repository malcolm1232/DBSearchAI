"""Ingest as a durable, resumable, observable job (ADR 0016, #454 + #455).

One record per crawl. It carries the three things the ADR says must not be renegotiated:

  * the **pinned cursor** the batch was listed with (`cursor_in_flight`),
  * the **per-document checkpoint** (which documents are already indexed), and
  * enough **progress** to tell a slow job from a hung one.

A job also records the PROCESS that created it, and only that process may resume it (see
`_WORKER_ID`): a checkpoint is a promise about what is in the index, and a durable job record
outliving a non-durable index turns that promise into silent data loss.

The record is deliberately NOT the same shape as the SourceDescriptor. A descriptor answers
"where is this source up to" (one cursor, one doc_count) and is overwritten on every sync; a
job answers "what is this particular run doing, and what has it already paid for", which is
the state a resume needs and a descriptor structurally cannot hold.

**Why completed documents are appended, never rewritten.** The obvious encoding is a list on
the job row. At the #536 acceptance size that is 4884 ids rewritten on every single document
— quadratic write amplification against the exact corpus this design exists to survive, and
the row itself becomes the bottleneck. So completed ids are their own append-only rows,
keyed (job_id, external_id), and the job row is written only when phase/status changes.

LAW 1: nothing here holds document CONTENT. An external_id is a source-side identifier and
stays in the data plane with everything else; ADR 0012's tenant partition applies unchanged.
LAW 6: the point of the module is that this state is NOT held in compute — the in-memory
store is for tests and single-process rigs, and `PgJobStore` is the same interface backed by
the deployment's own Postgres, exactly as `manifest_store.py` does it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# Terminal and non-terminal states. "syncing" is the value SourceDescriptor.status already
# reserved for this ("reserved for async slice") — the state machine was anticipated and
# never built.
QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"
TERMINAL = (SUCCEEDED, FAILED)

# #577: what makes a job UNRESUMABLE is NOT the same set as what makes it terminal, and
# conflating the two is what made #455's checkpoint never pay off in the one case it exists
# for. Only a SUCCEEDED job must never be picked back up.
#
#   succeeded -> never resumable. Its batch is COMPLETE. Re-listing it would re-crawl
#                finished work and, worse, skip the new changes sitting behind it - losing
#                documents rather than merely time.
#   failed    -> RESUMABLE. "Failed" means "died with work banked", which is the exact
#                condition a checkpoint exists for. Measured cost of getting this wrong
#                (#564, 4884 docs): run 1 died at 1456 having banked 20,002 chunks; run 2
#                started fresh and paid 59,927. Zero saving.
#   running   -> resumable, in THIS process only (see _WORKER_ID). A job left running is not
#                evidence a worker is alive; it is evidence one died without finishing.
UNRESUMABLE = (SUCCEEDED,)

# Identity of THIS process, stamped on every job it creates.
#
# A checkpoint is only as trustworthy as the index it points at, and those are two different
# stores with two different lifetimes. The connector rail indexes into an InMemoryIndex, which
# dies with the process; `PgJobStore` does not. Without this, the sequence is:
#
#   crawl 700 of 4884 documents -> process restarts -> the Pg row still says `running`
#   -> the next sync finds it resumable -> the resume SKIPS those 700 -> their chunks went
#   with the old process, so they are never indexed and never will be.
#
# Silent, permanent loss, produced by the mechanism built to prevent it. So a job is only
# resumable by the process that created it. A durable job store still buys durable HISTORY
# and observability across restarts; what it cannot buy alone is restart-resume, which needs
# a durable INDEX too (#327). When the index becomes durable (pgvector), this filter is the
# one line that lifts.
_WORKER_ID = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class IngestJob:
    """One crawl. `cursor_in_flight` is the cursor `list_changes` was called with and stays
    put for the whole run; `next_cursor` is set ONLY when the batch completed, which is what
    makes it safe for the caller to persist onto the source."""

    job_id: str
    tenant_id: str
    source_id: str
    status: str = QUEUED
    phase: str = ""                     # discovering | fetching | extracting | embedding | indexing | done
    docs_done: int = 0
    docs_total: int = 0
    docs_skipped: int = 0               # already-indexed documents a resume did not redo
    cursor_in_flight: str | None = None
    next_cursor: str | None = None
    error: str | None = None
    owner: str = ""                     # the process that created it - see _WORKER_ID
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "IngestJob":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class JobStoreUnavailable(Exception):
    """The job store is configured but cannot be reached. Mirrors ManifestStoreUnavailable:
    the message is a data-free exception class name plus a SQLSTATE where the driver gives
    one, never `str(driver_error)`."""


class InMemoryJobStore:
    """Hermetic adapter for tests and single-process rigs.

    Honest about what it is: this satisfies the *checkpoint* contract (a resume inside one
    process skips completed documents) but NOT restart-resume (#327), because a process
    restart takes it with it. That is exactly the LAW 6 line, and it is why PgJobStore below
    is the same interface rather than a different design.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, IngestJob] = {}
        self._docs: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def create(self, *, tenant_id: str, source_id: str, cursor: str | None,
               job_id: str | None = None) -> IngestJob:
        job = IngestJob(job_id=job_id or f"job-{uuid.uuid4().hex[:12]}", tenant_id=tenant_id,
                        source_id=source_id, cursor_in_flight=cursor, owner=_WORKER_ID)
        with self._lock:
            self._jobs[job.job_id] = job
            self._docs.setdefault(job.job_id, set())
        return job

    def get(self, job_id: str) -> "IngestJob | None":
        with self._lock:
            job = self._jobs.get(job_id)
            return IngestJob.from_dict(job.to_dict()) if job else None

    def update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated_at = _now()

    def record_document(self, job_id: str, external_id: str) -> None:
        with self._lock:
            self._docs.setdefault(job_id, set()).add(external_id)

    def completed_documents(self, job_id: str) -> set[str]:
        with self._lock:
            return set(self._docs.get(job_id, ()))

    def resumable(self, tenant_id: str, source_id: str) -> "IngestJob | None":
        """The most recent resumable run of this source BY THIS PROCESS.

        A job left `running` is not evidence that a worker is alive; it is evidence that one
        died without finishing. Within a process that is exactly what should be picked back
        up. Across processes it must NOT be, while the index is in-memory — see `_WORKER_ID`,
        where resuming a dead process's job is silent data loss rather than a saving.

        #577: the filter is `UNRESUMABLE`, not `TERMINAL`. A FAILED job is precisely the one
        a checkpoint was built for - see UNRESUMABLE for why succeeded is the only exclusion."""
        with self._lock:
            candidates = [j for j in self._jobs.values()
                          if j.tenant_id == tenant_id and j.source_id == source_id
                          and j.status not in UNRESUMABLE and j.owner == _WORKER_ID]
            if not candidates:
                return None
            newest = max(candidates, key=lambda j: j.created_at)
            return IngestJob.from_dict(newest.to_dict())

    def list_jobs(self, tenant_id: str, source_id: str | None = None) -> list[IngestJob]:
        with self._lock:
            rows = [j for j in self._jobs.values()
                    if j.tenant_id == tenant_id
                    and (source_id is None or j.source_id == source_id)]
        return sorted((IngestJob.from_dict(j.to_dict()) for j in rows),
                      key=lambda j: j.created_at, reverse=True)


class JobCheckpoint:
    """What `run_ingestion` is handed: the per-document checkpoint for ONE job.

    Deliberately narrow. The runner does not decide cursors, does not decide job lifecycle,
    and cannot see other jobs — it records documents as it indexes them and asks which ones
    are already done. Everything else is the caller's (ADR 0016 §2 is a pipeline concern;
    §1 and §3 are the router's).

    `resume=True` is what makes a second run skip completed work. It is opt-in rather than
    inferred, because "this job already has completed documents" is ALSO true of a job that
    is mid-flight in another worker, and silently treating that as a resume would have two
    workers splitting one batch and neither one finishing it.
    """

    def __init__(self, store, job_id: str, *, resume: bool = False) -> None:
        self._store = store
        self.job_id = job_id
        self._already_done: set[str] = store.completed_documents(job_id) if resume else set()
        # Set by the caller (see ConnectorStoreProvider._crawl). Runs BEFORE the job is
        # marked succeeded, and the ordering is load-bearing: a client polls the job and
        # then reads the store, so a job that flips to "succeeded" while the source
        # descriptor still says "syncing" hands out a verdict its own product contradicts.
        # Caught by e2e_router_api on a one-file folder, where the crawl finished inside the
        # compose request and the window was the entire test.
        self.on_commit = None

    @property
    def already_done(self) -> set[str]:
        return set(self._already_done)

    def record(self, external_id: str) -> None:
        self._store.record_document(self.job_id, external_id)

    def progress(self, phase: str, done: int, total: int, skipped: int = 0) -> None:
        self._store.update(self.job_id, phase=phase, docs_done=done, docs_total=total,
                           docs_skipped=skipped, status=RUNNING)

    def started(self, cursor: str | None) -> None:
        self._store.update(self.job_id, status=RUNNING, cursor_in_flight=cursor, error=None)

    def complete(self, result) -> None:
        """Commit the caller's own end-of-crawl bookkeeping, THEN publish success."""
        if self.on_commit is not None:
            self.on_commit(result)
        self.succeeded(result.cursor)

    def succeeded(self, next_cursor: str | None) -> None:
        self._store.update(self.job_id, status=SUCCEEDED, phase="done", error=None,
                           next_cursor=next_cursor)

    def failed(self, exc: BaseException) -> None:
        # The class name, not str(exc): a driver/connector error can quote the offending
        # value, and this field is rendered back to a user (manifest_store.py, same rule).
        self._store.update(self.job_id, status=FAILED, error=type(exc).__name__)


# --------------------------------------------------------------------------- durable

def _safe_reason(exc: BaseException) -> str:
    sqlstate = getattr(exc, "sqlstate", None)
    return (f"{type(exc).__name__} (sqlstate {sqlstate})" if sqlstate
            else type(exc).__name__)


class PgJobStore:
    """The same interface in the deployment's own compose-managed Postgres (PGVECTOR_DSN) —
    no new infrastructure, the `manifest_store.PgManifestStore` idiom throughout: connect per
    operation, `CREATE TABLE IF NOT EXISTS` on first touch in its OWN committed transaction,
    sanitized failures.

    Two tables, for the write-amplification reason in the module docstring: `ingest_jobs`
    holds one small row per run, `ingest_job_documents` is append-only, one row per completed
    document, and `completed_documents` is a single indexed read.
    """

    def __init__(self, dsn: str, table: str = "ingest_jobs", connect_timeout: int = 5) -> None:
        self._dsn = dsn
        self._table = table
        self._doc_table = f"{table}_documents"
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
                        job_id      text PRIMARY KEY,
                        tenant_id   text NOT NULL,
                        source_id   text NOT NULL,
                        record      jsonb NOT NULL,
                        created_at  timestamptz NOT NULL DEFAULT now(),
                        updated_at  timestamptz NOT NULL DEFAULT now()
                    )""")
                conn.execute(
                    f"""CREATE TABLE IF NOT EXISTS {self._doc_table} (
                        job_id       text NOT NULL,
                        external_id  text NOT NULL,
                        done_at      timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (job_id, external_id)
                    )""")
                # The resume read is (tenant, source, non-terminal) on every restart, and the
                # #327 case is a table that has accumulated every past run of every source.
                conn.execute(
                    f"""CREATE INDEX IF NOT EXISTS {self._table}_tenant_source_idx
                        ON {self._table} (tenant_id, source_id, created_at DESC)""")
            # Only now is the schema durable — see PgManifestStore._ensure_schema for why the
            # flag must not be set inside the operation's own transaction.
            self._schema_done = True

    def _run(self, fn):
        try:
            self._ensure_schema()
            with self._conn() as conn:
                return fn(conn)
        except Exception as exc:
            reason = _safe_reason(exc)
            logging.getLogger("dbsearch").error("job store %s unavailable: %s",
                                                self._table, reason)
        # Raised outside the handler so neither __cause__ nor __context__ can carry the
        # driver's message (which can quote a row value) into a traceback render.
        raise JobStoreUnavailable(reason)

    def create(self, *, tenant_id: str, source_id: str, cursor: str | None,
               job_id: str | None = None) -> IngestJob:
        job = IngestJob(job_id=job_id or f"job-{uuid.uuid4().hex[:12]}", tenant_id=tenant_id,
                        source_id=source_id, cursor_in_flight=cursor, owner=_WORKER_ID)
        self._run(lambda conn: conn.execute(
            f"""INSERT INTO {self._table} (job_id, tenant_id, source_id, record)
                VALUES (%s, %s, %s, %s::jsonb)""",
            (job.job_id, tenant_id, source_id, json.dumps(job.to_dict()))))
        return job

    def get(self, job_id: str) -> "IngestJob | None":
        def _get(conn):
            row = conn.execute(f"SELECT record FROM {self._table} WHERE job_id = %s",
                               (job_id,)).fetchone()
            return IngestJob.from_dict(row[0]) if row else None
        return self._run(_get)

    def update(self, job_id: str, **fields) -> None:
        """Merged server-side (`record || patch`) rather than read-modify-write, so a
        progress tick from the worker cannot clobber a status written concurrently."""
        fields = dict(fields, updated_at=_now())
        self._run(lambda conn: conn.execute(
            f"""UPDATE {self._table}
                SET record = record || %s::jsonb, updated_at = now()
                WHERE job_id = %s""",
            (json.dumps(fields), job_id)))

    def record_document(self, job_id: str, external_id: str) -> None:
        # ON CONFLICT DO NOTHING: re-recording a document is the normal shape of a retry,
        # not an error.
        self._run(lambda conn: conn.execute(
            f"""INSERT INTO {self._doc_table} (job_id, external_id) VALUES (%s, %s)
                ON CONFLICT DO NOTHING""",
            (job_id, external_id)))

    def completed_documents(self, job_id: str) -> set[str]:
        def _read(conn):
            rows = conn.execute(
                f"SELECT external_id FROM {self._doc_table} WHERE job_id = %s",
                (job_id,)).fetchall()
            return {r[0] for r in rows}
        return self._run(_read)

    def resumable(self, tenant_id: str, source_id: str) -> "IngestJob | None":
        """#577: `NOT IN ('succeeded')`, not `NOT IN ('succeeded','failed')`. A failed job is
        the one the checkpoint exists for. Kept in lockstep with the in-memory store through
        the shared `UNRESUMABLE` tuple, so the two cannot drift into different resume
        semantics - which is the whole reason both are exercised by one parametrised test
        (selftest_565_job_store_partitioning.py)."""
        # Built from UNRESUMABLE so the two stores cannot drift apart, but kept in the SAME
        # parameterised `NOT IN (...)` shape the pre-#577 query used - only the CONTENTS of
        # the list change. There is no Postgres in the test rig (selftest_565 skips the SQL
        # path without a DSN), so this deliberately avoids inventing a new construct that
        # nothing here can exercise; `test_the_two_stores_exclude_the_same_states` pins the
        # correspondence instead.
        placeholders = ", ".join(["%s"] * len(UNRESUMABLE))

        def _read(conn):
            row = conn.execute(
                f"""SELECT record FROM {self._table}
                    WHERE tenant_id = %s AND source_id = %s
                      AND record->>'status' NOT IN ({placeholders})
                      AND record->>'owner' = %s
                    ORDER BY created_at DESC LIMIT 1""",
                (tenant_id, source_id, *UNRESUMABLE, _WORKER_ID)).fetchone()
            return IngestJob.from_dict(row[0]) if row else None
        return self._run(_read)

    def list_jobs(self, tenant_id: str, source_id: str | None = None) -> list[IngestJob]:
        def _read(conn):
            if source_id is None:
                rows = conn.execute(
                    f"""SELECT record FROM {self._table} WHERE tenant_id = %s
                        ORDER BY created_at DESC LIMIT 50""", (tenant_id,)).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT record FROM {self._table}
                        WHERE tenant_id = %s AND source_id = %s
                        ORDER BY created_at DESC LIMIT 50""",
                    (tenant_id, source_id)).fetchall()
            return [IngestJob.from_dict(r[0]) for r in rows]
        return self._run(_read)
