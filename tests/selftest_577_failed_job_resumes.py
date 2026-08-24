"""#577 - a FAILED job is the one a checkpoint exists for, so it must be resumable.

Two things built in #454/#455 contradicted each other:

  - `run_ingestion` marks a job `failed` on exception. Deliberate: a job stuck at `running`
    would mislead `resumable()` forever.
  - `worker.submit` only resumes a NON-TERMINAL job, and `failed` is terminal.

Net effect in the production path: a crawl dies, the user hits sync again, `submit` finds
nothing resumable, creates a FRESH job with an empty checkpoint, and re-embeds everything.
Measured at full scale by the #564 acceptance run: run 1 died at doc 1456/4884 having banked
20,002 chunks; run 2 got a different job id, reported docs_skipped=0, and paid 59,927 chunks.
Zero saving, from machinery built precisely to save it.

THE RULE, and the reason it is `succeeded` rather than "terminal" that must be excluded:

  succeeded -> NEVER resumable. Its batch is complete; re-listing it would re-crawl a
               finished batch and, worse, skip the new changes behind it.
  failed    -> resumable. "Failed" means "died with work banked", which is the exact
               condition a checkpoint is for.
  running   -> resumable IN THIS PROCESS only. A job left running is not evidence that a
               worker is alive, it is evidence one died without finishing. The `_WORKER_ID`
               guard keeps that in-process, because resuming a dead process's job while the
               index is in-memory is silent data loss rather than a saving.

    PYTHONPATH=src python3 tests/selftest_577_failed_job_resumes.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("SELFHOST_BACKEND", "memory")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dbsearch.pipeline.jobs import InMemoryJobStore  # noqa: E402
from dbsearch.server.app import _edition  # noqa: E402
from selftest_569_edition_ingest_off_the_request import (SlowConnector, _FailsPartWay,  # noqa: E402
                                                         TENANT)

WS = "ws-577"
SOURCE = "sharepoint:577"


# ---- the store rule ---------------------------------------------------------------------

def test_a_failed_job_is_offered_for_resume():
    """THE FIX. Before #577 this returned None and the banked documents were abandoned."""
    store = InMemoryJobStore()
    job = store.create(tenant_id=WS, source_id=SOURCE, cursor="cursor-A")
    store.record_document(job.job_id, "offer-letter.pdf")
    store.update(job.job_id, status="failed", error="RuntimeError")
    got = store.resumable(WS, SOURCE)
    assert got is not None and got.job_id == job.job_id, (
        "a failed job is not offered for resume, so its banked documents are re-paid for")
    assert store.completed_documents(job.job_id) == {"offer-letter.pdf"}


def test_a_succeeded_job_is_never_offered_for_resume():
    """UNCHANGED, and the reason the fix is 'exclude succeeded' rather than 'allow terminal'.
    A finished batch re-listed would re-crawl what is done AND skip the new changes behind
    it - a worse failure than re-paying, because it loses documents rather than time."""
    store = InMemoryJobStore()
    job = store.create(tenant_id=WS, source_id=SOURCE, cursor="cursor-A")
    store.update(job.job_id, status="succeeded", next_cursor="cursor-A2")
    assert store.resumable(WS, SOURCE) is None


def test_a_resumed_failed_job_keeps_its_pinned_cursor():
    """The cursor stays where the dead batch started. Advancing it is the silent-loss trap
    ADR 0016 refuses: `list_changes` advances PER BATCH, so a newer cursor would skip every
    unrecorded item of the batch that was in flight."""
    store = InMemoryJobStore()
    job = store.create(tenant_id=WS, source_id=SOURCE, cursor="cursor-pinned")
    store.update(job.job_id, status="failed", error="RuntimeError")
    assert store.resumable(WS, SOURCE).cursor_in_flight == "cursor-pinned"


def test_a_failed_job_in_another_workspace_is_not_offered():
    """#565: the job store is one table shared by every workspace. A resume that crossed
    workspaces would skip documents into an index that never saw them."""
    store = InMemoryJobStore()
    mine = store.create(tenant_id=WS, source_id=SOURCE, cursor="c")
    store.update(mine.job_id, status="failed", error="RuntimeError")
    assert store.resumable("ws-someone-else", SOURCE) is None


def test_the_two_stores_exclude_the_same_states():
    """There is no Postgres in this rig (selftest_565 skips the SQL path without a DSN), so
    the Pg query cannot be executed here. What CAN be pinned is that it is built from the
    same `UNRESUMABLE` tuple the in-memory store filters on - so the two cannot drift into
    different resume semantics without this failing.

    The failure that would otherwise be invisible: the in-memory store resumes a failed job,
    prod's Postgres store does not, and the saving silently exists only on the laptop.
    """
    import inspect

    from dbsearch.pipeline import jobs

    assert jobs.UNRESUMABLE == (jobs.SUCCEEDED,), (
        f"UNRESUMABLE is {jobs.UNRESUMABLE} - only SUCCEEDED may be excluded (#577)")
    assert jobs.FAILED not in jobs.UNRESUMABLE, "a failed job must stay resumable"

    # CODE only - no docstring, no comments. Both explain the #577 rule in prose and both
    # therefore contain the word 'failed', which made the first version of this assertion
    # fail against a correct implementation. The mirror image of the trap that made two #594
    # UI tests pass against deleted behaviour: never assert on source you have not stripped.
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(jobs.PgJobStore.resumable)))
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]                      # drop the docstring
    code = ast.unparse(fn)                         # unparse drops comments for us

    assert "UNRESUMABLE" in code, (
        "the Pg query no longer derives its exclusion list from UNRESUMABLE, so the two "
        "stores can drift apart silently")
    assert "'failed'" not in code and '"failed"' not in code, (
        "the Pg query hardcodes 'failed' as an exclusion again - that is the #577 bug")


# ---- the payoff, on the rail #569 just wired --------------------------------------------

def test_a_died_crawl_resumes_the_same_job_and_skips_what_it_banked():
    """What #455 was FOR, and what #564 measured as absent: the retry reuses the SAME job
    record, skips the documents already indexed, and still covers the ones the dead run
    never reached.

    This is the test #569 pinned as a known gap and told the next card to FLIP.
    """
    conn = _FailsPartWay(once=True)                 # dies at doc 3 of 5, once
    out = _edition.connect_sharepoint("tenant-577-a", "drive-577", connector=conn,
                                      tenant_id=TENANT, owner_oid="alice")
    _edition.await_ingest(out["job_id"], timeout=20)
    banked = set(conn.fetched)
    assert banked == {"doc-569-0", "doc-569-1", "doc-569-2"}, sorted(banked)

    conn.fetched.clear()                            # measure ONLY what the retry pays for
    out2 = _edition.resync_source(out["source_id"])
    final = _edition.await_ingest(out2["job_id"], timeout=20)
    assert final.status == "succeeded", f"the retry did not succeed: {final.error}"

    assert out2["job_id"] == out["job_id"], (
        f"the retry created a FRESH job ({out2['job_id']}) instead of resuming "
        f"{out['job_id']} - the checkpoint is abandoned and everything is re-paid for")
    refetched = banked & set(conn.fetched)
    assert not refetched, f"the resume re-fetched banked documents: {sorted(refetched)}"
    assert final.docs_skipped == 3, (
        f"the job reports docs_skipped={final.docs_skipped}, so the saving is invisible "
        "to anyone watching - which is how #564 found this at all")
    covered = banked | set(conn.fetched)
    assert covered == {f"doc-569-{i}" for i in range(5)}, (
        f"documents went missing across the resume - silent loss: {sorted(covered)}")


def test_a_clean_crawl_still_starts_fresh_and_skips_nothing():
    """The fix must not make every crawl look like a resume."""
    conn = SlowConnector(docs=3, per_doc=0.01)
    out = _edition.connect_sharepoint("tenant-577-b", "drive-577b", connector=conn,
                                      tenant_id=TENANT, owner_oid="alice")
    job = _edition.await_ingest(out["job_id"], timeout=20)
    assert job.status == "succeeded" and job.docs_skipped == 0, job
    assert job.docs_done == 3, job


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
