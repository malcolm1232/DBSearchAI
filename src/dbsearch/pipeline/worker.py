"""Ingest off the request thread (ADR 0016 §1, #454).

`router_api.sync_store` used to call `provider.sync(store_id)` inline, and
`ConnectorStoreProvider._build` called `self.sync(sid)` as the initial full crawl during
compose. So both re-sync AND first connect ran an entire crawl inside one HTTP request — a
LAW 4 violation with a measured consequence: #536's 40.2MB / 4884-document pack exceeded
`/router/compose`'s 3600s timeout and could not complete at all.

This module is the missing piece: a request SUBMITS a crawl and returns a handle, a worker
executes it, and the job record (`pipeline/jobs.py`) makes it observable and resumable.

**Why a thread pool and not an external queue.** ADR 0016 deliberately left the worker
mechanism open — it is a portability question (LAW 7) that should follow the deployment
target, and both satisfy the checkpoint contract. The contract lives in the job store, not
here, so swapping this for a Service Bus consumer changes no caller: they submit and poll a
job id either way. What matters is that the durable state is NOT in this process (LAW 6),
which is `PgJobStore`'s job.

**One run per source, enforced.** Two workers crawling one source would split a single batch
between them and neither would finish it — and because the cursor is pinned until the batch
completes, the source would sit permanently behind while both runs reported success. A
submit for a source that is already running returns the RUNNING job rather than starting a
second one.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from dbsearch.pipeline.jobs import RUNNING, TERMINAL, IngestJob, JobCheckpoint

log = logging.getLogger("dbsearch.ingest")


class IngestJobRunner:
    """Submits crawls to a small pool and tracks the one live run per source.

    `work` is a callable taking the `JobCheckpoint` for the run: everything source-specific
    (which connector, which cursor, what to record on the descriptor afterwards) stays with
    the caller, so this class never grows a connector import."""

    def __init__(self, store, max_workers: int = 2) -> None:
        self._store = store
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="dbsearch-ingest")
        self._lock = threading.Lock()
        self._futures: dict[str, Future] = {}
        self._active: dict[tuple[str, str], str] = {}      # (tenant, source) -> job_id

    # ------------------------------------------------------------------ submit

    def submit(self, *, tenant_id: str, source_id: str, cursor: str | None, work,
               resume: bool = True, force_new: bool = False) -> IngestJob:
        """Enqueue a crawl and return its job record immediately.

        `resume=True` (the default) picks up a non-terminal job for this source instead of
        starting a fresh one, which is what turns a killed process into a continuation rather
        than a restart (#327). The resumed run is listed with the job's PINNED
        `cursor_in_flight`, never with a newer cursor: the batch it was part-way through is
        the batch it must finish."""
        key = (tenant_id, source_id)
        with self._lock:
            live = self._active.get(key)
            if live is not None and not force_new:
                fut = self._futures.get(live)
                if fut is not None and not fut.done():
                    existing = self._store.get(live)
                    if existing is not None:
                        log.info("ingest already running for %s, returning job %s",
                                 source_id, live)
                        return existing

            # `force_new` is the REBUILD case, and it is not an optimisation opt-out. A store
            # that is composed again gets a brand new index and descriptor, so an in-flight
            # crawl over the previous ones is filling something that has been thrown away -
            # handing that job back would leave the new store marked syncing and permanently
            # empty, because no crawl was ever submitted for it. Caught by the conversational
            # setup flow (#116), which composes twice: once to validate, once to apply.
            prior = (self._store.resumable(tenant_id, source_id)
                     if resume and not force_new else None)
            if prior is not None:
                job = prior
                # The pinned cursor wins over whatever the caller passed. A resumed run that
                # re-listed with the source's newer cursor would skip every item of the
                # unfinished batch that has not been recorded — silent document loss, the one
                # failure ADR 0016 exists to refuse.
                use_cursor = prior.cursor_in_flight
                resuming = True
            else:
                job = self._store.create(tenant_id=tenant_id, source_id=source_id,
                                         cursor=cursor)
                use_cursor = cursor
                resuming = False

            self._store.update(job.job_id, status=RUNNING, error=None)
            fut = self._pool.submit(self._execute, job.job_id, use_cursor, work, resuming)
            self._futures[job.job_id] = fut
            self._active[key] = job.job_id

        refreshed = self._store.get(job.job_id)
        return refreshed if refreshed is not None else job

    def _execute(self, job_id: str, cursor: str | None, work, resuming: bool) -> None:
        checkpoint = JobCheckpoint(self._store, job_id, resume=resuming)
        try:
            work(checkpoint, cursor)
        except BaseException as exc:                    # noqa: BLE001 - see below
            # run_ingestion already marked the job failed through the checkpoint; this catch
            # exists so the exception cannot escape into the pool and vanish. A failure that
            # only exists as a dead Future is invisible to the user, who is watching a
            # progress endpoint that would simply stop moving — the "is it slow or is it
            # hung?" question this whole card exists to answer.
            checkpoint.failed(exc)
            log.warning("ingest job %s failed: %s", job_id, type(exc).__name__)

    # -------------------------------------------------------------------- read

    def wait(self, job_id: str, timeout: float | None = None) -> "IngestJob | None":
        """Block until this job leaves the pool. For rigs, tests and the CLI — never for a
        request handler, which is the thing this module exists to get crawls out of."""
        with self._lock:
            fut = self._futures.get(job_id)
        if fut is not None:
            try:
                fut.result(timeout=timeout)
            except BaseException:                       # noqa: BLE001
                pass                                    # the job record carries the verdict
        return self._store.get(job_id)

    def is_running(self, tenant_id: str, source_id: str) -> bool:
        with self._lock:
            job_id = self._active.get((tenant_id, source_id))
            fut = self._futures.get(job_id) if job_id else None
        return bool(fut is not None and not fut.done())

    def job(self, job_id: str) -> "IngestJob | None":
        return self._store.get(job_id)

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait)


__all__ = ["IngestJobRunner", "TERMINAL"]
