"""#623: "Questions you have asked" survives a restart.

The panel's own subtitle says "Every answer is recorded against the person who asked it".
Until this card that sentence was false the moment the api container restarted: the audit
trail was a bounded in-memory list, so a `docker compose restart api` on prod emptied it
while conversations, shares and grants - all in Postgres - came back untouched. Found by
driving GOAL_ACCEPTANCE step 9 against dbsearch.ai on 260811, not by a test, because no
test could see it: every existing audit test builds one process and asks it what it holds.

That is the shape this file is written around. `test_pg_rows_outlive_the_process_that_wrote
_them` builds a SECOND store over the same DSN and reads what the first one wrote, which is
the closest a test gets to a restart, and it is the only test here that would have failed
before the fix. The in-memory tests below it are a regression guard on semantics that
already worked (#593's filter-then-limit especially) and would have passed either way.

The Postgres tests need a real Postgres, named by DBSEARCH_TEST_DSN. They SKIP without one
rather than silently passing on the memory path - a skipped durability test is honest; a
green one that never touched Postgres is the failure mode this card exists to fix.
"""
import os
import sys
import uuid

sys.path.insert(0, "src")

import pytest

from dbsearch.audit import AuditLogUnavailable, InMemoryAuditLog, PgAuditLog

TEST_DSN = os.environ.get("DBSEARCH_TEST_DSN", "")
needs_pg = pytest.mark.skipif(not TEST_DSN, reason="set DBSEARCH_TEST_DSN to a live Postgres")

# Unreachable in one connect attempt: port 1 on loopback refuses immediately.
DEAD_DSN = "dbname=nope host=127.0.0.1 port=1 connect_timeout=1"


def _table() -> str:
    """A fresh table per test, so two runs (or two tests) never read each other's rows."""
    return "audit_test_" + uuid.uuid4().hex[:12]


def _pg(table: str) -> PgAuditLog:
    return PgAuditLog(TEST_DSN, table=table)


def _drop(table: str) -> None:
    import psycopg
    with psycopg.connect(TEST_DSN, connect_timeout=5) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


# --------------------------------------------------------------------------------------
# The card: durability
# --------------------------------------------------------------------------------------

@needs_pg
def test_pg_rows_outlive_the_process_that_wrote_them():
    """THE #623 TEST. A second store over the same DSN is what a restart looks like from
    the data's point of view: no shared object, no shared cache, nothing carried across
    but the table itself. Before the fix there was no table to carry anything."""
    table = _table()
    try:
        _pg(table).record("bob", "how much leave?", "ask", ["hr-1"], "2026-08-11T00:00:00+00:00")
        restarted = _pg(table)                      # the "new process"
        rows = restarted.recent(50, user="bob")
        assert [r.question for r in rows] == ["how much leave?"]
        assert rows[0].authorized_docs == ["hr-1"]
        assert rows[0].n_authorized == 1
        assert rows[0].surface == "ask"
    finally:
        _drop(table)


@needs_pg
def test_pg_newest_first_and_filter_then_limit():
    """#593's property, on the durable path: filter by user FIRST, then window. A colleague
    asking questions must never push the owner's own history out of the answer."""
    table = _table()
    try:
        log = _pg(table)
        for i in range(3):
            log.record("bob", f"bob-{i}", "ask", [], "2026-08-11T00:00:00+00:00")
        for i in range(5):
            log.record("alice", f"alice-{i}", "ask", [], "2026-08-11T00:00:00+00:00")
        assert [r.question for r in log.recent(2, user="bob")] == ["bob-2", "bob-1"]
        # Deployment-wide read still sees everyone, newest first.
        assert log.recent(1)[0].question == "alice-4"
    finally:
        _drop(table)


@needs_pg
def test_pg_drop_user_removes_that_users_rows_only():
    """Durability created this obligation. While the log died with the process, the #576
    retention sweep not covering it was harmless - the rows were gone within a deploy. A
    swept account whose questions survive in a table forever is a new hole, so the sweep
    gained a call and both stores gained this method in the same change."""
    table = _table()
    try:
        log = _pg(table)
        log.record("bob", "q", "ask", [], "2026-08-11T00:00:00+00:00")
        log.record("alice", "q", "ask", [], "2026-08-11T00:00:00+00:00")
        assert log.drop_user("bob") == 1
        assert log.recent(50, user="bob") == []
        assert len(log.recent(50, user="alice")) == 1
    finally:
        _drop(table)


@needs_pg
def test_pg_ts_round_trips_as_the_string_the_wire_expects():
    """`AuditEntry.ts` is a string on the wire and admin.js renders it. A timestamptz column
    that came back as a datetime would change the response shape for every reader."""
    table = _table()
    try:
        log = _pg(table)
        log.record("bob", "q", "ask", [], "2026-08-11T09:30:00+00:00")
        ts = log.recent(1, user="bob")[0].ts
        assert isinstance(ts, str) and ts.startswith("2026-08-11T09:30:00")
    finally:
        _drop(table)


# --------------------------------------------------------------------------------------
# Failure is loud and data-free
# --------------------------------------------------------------------------------------

def test_unreachable_store_raises_and_never_leaks_the_question():
    """grant_store.py's rule, and it matters MORE here: the value being written is the user's
    own question text, so a driver message quoting the statement would put a question into a
    log line. The reason is the exception class name plus a SQLSTATE, nothing else."""
    log = PgAuditLog(DEAD_DSN, table="audit_never_created")
    with pytest.raises(AuditLogUnavailable) as caught:
        log.record("bob", "the secret question", "ask", ["hr-1"], "2026-08-11T00:00:00+00:00")
    assert "secret question" not in str(caught.value)
    assert "bob" not in str(caught.value)

    with pytest.raises(AuditLogUnavailable):
        log.recent(25, user="bob")


def test_read_path_refuses_rather_than_answering_empty():
    """The read must never degrade to []. "No questions yet" is a claim about the user's
    history; a store outage is a claim about the store, and rendering the second as the first
    is how a durability bug hides. This is the guard for the route's 503 - see
    selftest_623_route_reports_outage below for the route itself."""
    log = PgAuditLog(DEAD_DSN, table="audit_never_created")
    with pytest.raises(AuditLogUnavailable):
        log.recent(25)


# --------------------------------------------------------------------------------------
# In-memory semantics, unchanged by the rename (regression guard only)
# --------------------------------------------------------------------------------------

def test_memory_newest_first_and_filter_then_limit():
    log = InMemoryAuditLog()
    for i in range(3):
        log.record("bob", f"bob-{i}", "ask", [], "2026-08-11T00:00:00+00:00")
    for i in range(5):
        log.record("alice", f"alice-{i}", "ask", [], "2026-08-11T00:00:00+00:00")
    assert [r.question for r in log.recent(2, user="bob")] == ["bob-2", "bob-1"]
    assert log.recent(1)[0].question == "alice-4"


def test_memory_drop_user_removes_that_users_rows_only():
    log = InMemoryAuditLog()
    log.record("bob", "q", "ask", [], "2026-08-11T00:00:00+00:00")
    log.record("alice", "q", "ask", [], "2026-08-11T00:00:00+00:00")
    assert log.drop_user("bob") == 1
    assert log.recent(50, user="bob") == []
    assert len(log) == 1


def test_memory_stays_bounded():
    log = InMemoryAuditLog(capacity=3)
    for i in range(10):
        log.record("bob", f"q{i}", "ask", [], "2026-08-11T00:00:00+00:00")
    assert len(log) == 3
    assert [r.question for r in log.recent(50)] == ["q9", "q8", "q7"]


def test_entry_dict_shape_is_what_admin_js_reads():
    log = InMemoryAuditLog()
    d = log.record("bob", "q", "ask", ["a", "b"], "2026-08-11T00:00:00+00:00").to_dict()
    assert d["n_authorized"] == 2
    assert set(d) == {"ts", "user", "question", "surface", "authorized_docs", "n_authorized"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------------------
# The route: an outage is a 503, never "No questions yet"
# --------------------------------------------------------------------------------------

def test_route_reports_the_outage_instead_of_an_empty_history():
    """The end of the chain, asserted through HTTP rather than on the store.

    This is the assertion that pins what a user is TOLD, and telling them the wrong thing is
    how #623 stayed invisible on prod: after the restart the panel said "No questions yet",
    which is what an empty 200 renders as, and that reads as a fact about the user rather
    than about the server. A test on `recent()` alone cannot see this - the store did exactly
    what it was asked - so it is asserted here, on the response.
    """
    import os as _os
    import time as _time

    _os.environ.setdefault("SELFHOST_BACKEND", "memory")
    from fastapi.testclient import TestClient
    from dbsearch.server import app as app_mod
    from dbsearch.server import user_auth

    client = TestClient(app_mod.app)
    cookie = {user_auth.COOKIE: user_auth.sign_session(
        {"oid": "11111111-1111-1111-1111-111111111111", "tid": "tid-1",
         "exp": int(_time.time()) + 3600})}

    original = app_mod._edition.audit_log
    app_mod._edition.audit_log = PgAuditLog(DEAD_DSN, table="audit_never_created")
    try:
        r = client.get("/me/questions", cookies=cookie)
        assert r.status_code == 503, (
            f"a store outage must not render as an empty history: {r.status_code} {r.text[:200]}")
        detail = r.json().get("detail", "")
        assert "stored, not lost" in detail, detail
        # And it must not have degraded to the shape the panel reads as "nothing here".
        assert r.json() != []
    finally:
        app_mod._edition.audit_log = original
