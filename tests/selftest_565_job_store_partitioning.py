"""#565 — a durable job store is ONE table shared by every workspace, so the partition key
has to be right.

#454 gave each provider its own in-memory job store, where cross-workspace confusion was
impossible by construction. Wiring `PgJobStore` (ADR 0016 §3) removes that accident: every
workspace now writes into `ingest_jobs`, and `resumable(tenant_id, source_id)` looks a job up
by that pair.

THE FAILURE THAT SHAPE INVITES: two people in the same Entra tenant each connect a folder
and each call it `hr-docs`. If the partition key were the tenant id, a resume for one would
find the OTHER's job record, skip every document that job had recorded, and write nothing for
them into an index that never saw those documents. Not a leak — no content crosses — but
silent, permanent data loss, produced by the exact mechanism built to prevent it. So the key
is the WORKSPACE, which is what `job_partition` carries.

This runs the same contract against both stores. `InMemoryJobStore` always; `PgJobStore` when
a DSN is offered, because the SQL is the half that cannot be proven by reading it:

    DBSEARCH_JOBSTORE_TEST_DSN=postgresql://postgres:test@127.0.0.1:55433/jobs \\
      python3 tests/selftest_565_job_store_partitioning.py
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.pipeline.jobs import InMemoryJobStore, PgJobStore  # noqa: E402

SOURCE = "hr-docs"          # the SAME store id in both workspaces — the whole point


def contract(store, tag: str) -> None:
    """Everything the resume path depends on, against whichever store."""
    ws_a = f"alice-ws-{uuid.uuid4().hex[:8]}"
    ws_b = f"bob-ws-{uuid.uuid4().hex[:8]}"

    job_a = store.create(tenant_id=ws_a, source_id=SOURCE, cursor="cursor-A")
    job_b = store.create(tenant_id=ws_b, source_id=SOURCE, cursor="cursor-B")
    assert job_a.job_id != job_b.job_id

    store.record_document(job_a.job_id, "offer-letter.pdf")
    store.record_document(job_a.job_id, "leave-policy.pdf")
    store.record_document(job_b.job_id, "bobs-own.pdf")

    # --- the partition itself
    r_a = store.resumable(ws_a, SOURCE)
    r_b = store.resumable(ws_b, SOURCE)
    assert r_a is not None and r_b is not None, f"{tag}: both workspaces have a live job"
    assert r_a.job_id == job_a.job_id, (
        f"{tag}: workspace A's resume found {r_a.job_id}, not its own job. A resume skips "
        "every document the found job recorded — into an index that never saw them.")
    assert r_b.job_id == job_b.job_id, f"{tag}: workspace B resumed the wrong job"
    assert r_a.cursor_in_flight == "cursor-A", (
        f"{tag}: the PINNED cursor must survive the round trip, got "
        f"{r_a.cursor_in_flight!r} — resuming with the wrong cursor lists the wrong batch")

    # --- the checkpoint is per JOB, and does not bleed between them
    done_a = store.completed_documents(job_a.job_id)
    done_b = store.completed_documents(job_b.job_id)
    assert done_a == {"offer-letter.pdf", "leave-policy.pdf"}, f"{tag}: {done_a}"
    assert done_b == {"bobs-own.pdf"}, f"{tag}: {done_b}"
    assert not (done_a & done_b), f"{tag}: checkpoints bled across jobs"

    # --- re-recording is a retry, not an error
    store.record_document(job_a.job_id, "offer-letter.pdf")
    assert store.completed_documents(job_a.job_id) == done_a, f"{tag}: duplicate broke it"

    # --- update merges rather than replaces, and a terminal job stops being resumable
    store.update(job_a.job_id, phase="embedding", docs_done=2, docs_total=9)
    mid = store.get(job_a.job_id)
    assert (mid.phase, mid.docs_done, mid.docs_total) == ("embedding", 2, 9), mid
    assert mid.cursor_in_flight == "cursor-A", (
        f"{tag}: a progress tick overwrote the pinned cursor — {mid.cursor_in_flight!r}")
    assert mid.source_id == SOURCE and mid.tenant_id == ws_a, mid

    store.update(job_a.job_id, status="succeeded", next_cursor="cursor-A2")
    assert store.resumable(ws_a, SOURCE) is None, (
        f"{tag}: a FINISHED job is still offered for resume — the next crawl would re-list "
        "a batch that is already complete and skip the new changes behind it")
    assert store.resumable(ws_b, SOURCE).job_id == job_b.job_id, (
        f"{tag}: finishing A's job disturbed B's")

    done = store.get(job_a.job_id)
    assert done.status == "succeeded" and done.next_cursor == "cursor-A2", done

    # --- a FAILED job IS resumable, and this assertion is FLIPPED by #577.
    #
    # It used to read "a failed job was offered for resume" as the failure, on the reasoning
    # that failed is terminal like succeeded. That reasoning was wrong and the test was
    # protecting the bug: `run_ingestion` marks a dead job failed, so "failed" is the normal
    # end state of exactly the crash a checkpoint exists for. Excluding it meant #455's
    # machinery never paid off in production - measured at 4884 docs, a crawl that died at
    # 1456 having banked 20,002 chunks re-paid 59,927 on the retry.
    #
    # SUCCEEDED remains excluded (asserted above), and that asymmetry is the point: a
    # complete batch re-listed would skip the new changes behind it.
    job_c = store.create(tenant_id=ws_a, source_id=SOURCE, cursor="cursor-C")
    store.update(job_c.job_id, status="failed", error="RuntimeError")
    resumed = store.resumable(ws_a, SOURCE)
    assert resumed is not None and resumed.job_id == job_c.job_id, (
        f"{tag}: a FAILED job is not offered for resume, so its banked documents are "
        "abandoned and the next crawl pays for them again (#577)")
    assert resumed.cursor_in_flight == "cursor-C", (
        f"{tag}: the resumed job did not keep its pinned cursor - a newer one would skip "
        "every unrecorded item of the batch that was in flight")
    assert store.get(job_c.job_id).error == "RuntimeError"

    # --- a job from ANOTHER process is never resumed while the index is in-memory
    import dbsearch.pipeline.jobs as J
    ws_d = f"ghost-ws-{uuid.uuid4().hex[:8]}"
    mine, J._WORKER_ID = J._WORKER_ID, "9999:deadbeef"      # pretend to be a previous process
    try:
        ghost = store.create(tenant_id=ws_d, source_id=SOURCE, cursor="cursor-D")
        store.record_document(ghost.job_id, "vanished.pdf")
    finally:
        J._WORKER_ID = mine
    assert store.get(ghost.job_id) is not None, f"{tag}: the ghost row should still EXIST"
    assert store.resumable(ws_d, SOURCE) is None, (
        f"{tag}: a job left `running` by a DEAD process was offered for resume. Its documents "
        "are recorded as done but its chunks went with that process (the connector rail "
        "indexes in memory), so resuming skips documents that will now never be indexed.")

    jobs = store.list_jobs(ws_a, SOURCE)
    assert {j.job_id for j in jobs} == {job_a.job_id, job_c.job_id}, (
        f"{tag}: list_jobs leaked across workspaces or lost one: {[j.job_id for j in jobs]}")
    assert all(j.tenant_id == ws_a for j in jobs), f"{tag}: foreign row in list_jobs"

    print(f"  PASS  {tag}")


def main() -> int:
    print("#565 job-store contract:")
    contract(InMemoryJobStore(), "InMemoryJobStore")

    dsn = os.environ.get("DBSEARCH_JOBSTORE_TEST_DSN", "")
    if dsn:
        table = f"ingest_jobs_t{uuid.uuid4().hex[:8]}"
        store = PgJobStore(dsn, table=table)
        try:
            contract(store, f"PgJobStore ({table})")
        finally:
            import psycopg
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                conn.execute(f"DROP TABLE IF EXISTS {table}_documents")
                conn.execute(f"DROP TABLE IF EXISTS {table}")
    else:
        # Said out loud rather than skipped quietly: PgJobStore is the store that makes
        # restart-resume real, and an unrun SQL path is an unproven one.
        print("  SKIP  PgJobStore — set DBSEARCH_JOBSTORE_TEST_DSN to exercise the SQL")

    print("#565 PASSED — a job belongs to a workspace; a SUCCEEDED job is never resumed, a failed one is (#577)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
