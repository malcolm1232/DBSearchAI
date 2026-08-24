"""#596: conversations survive a restart.

Documents are durable, grants are durable (#575), sessions are durable (signed cookie) -
the conversation was the one plane that died with the process, and sharing a conversation
(#600) is meaningless if the thing shared evaporates on the next deploy.

Pins: the restart story (a fresh store over the same Postgres returns the same turns);
cited_docs round-trips (the share operation in #600 is defined over it); drop_user reaches
the store (the #576 retention sweep must not leave a swept account's questions readable);
an unreachable Pg store raises ConversationStoreUnavailable and never a driver error
(data-free reason, grant_store.py's rule).
"""
import sys
sys.path.insert(0, "src")

from dbsearch.query.conversation import ConversationService, Turn
from dbsearch.query.service import QueryResult
from dbsearch.server.conversation_store import (
    ConversationStoreUnavailable, InMemoryConversationStore, PgConversationStore)


def _turn(q="what is the leave policy?", docs=("hr-policy-1",)):
    return Turn(question=q, standalone=q, answer="14 days.", cited_docs=list(docs))


def test_turn_carries_cited_docs_and_defaults_empty():
    assert _turn().cited_docs == ["hr-policy-1"]
    assert Turn(question="q", standalone="q", answer="a").cited_docs == []


def test_append_then_history_in_order():
    s = InMemoryConversationStore()
    s.append("c1", "bob", _turn("q1"))
    s.append("c1", "bob", _turn("q2"))
    assert [t.question for t in s.history("c1", "bob")] == ["q1", "q2"]


def test_histories_do_not_bleed_across_users_or_conversations():
    s = InMemoryConversationStore()
    s.append("c1", "bob", _turn())
    assert s.history("c1", "alice") == []
    assert s.history("c2", "bob") == []


def test_drop_user_removes_every_conversation_of_that_user_only():
    s = InMemoryConversationStore()
    s.append("c1", "bob", _turn())
    s.append("c2", "bob", _turn())
    s.append("c1", "alice", _turn())
    assert s.drop_user("bob") == 2
    assert s.history("c1", "bob") == [] and s.history("c1", "alice") != []


class _FakeDriverError(Exception):
    """Stands in for a psycopg error whose message quotes a stored value - e.g. the
    DETAIL line of a uniqueness violation, which echoes the row it collided with. Real
    driver errors carry `.sqlstate`; this fake carries one too, so it exercises the same
    branch of `_safe_reason` (SQLSTATE-bearing) a real query failure would hit, not the
    fallback branch a bare connection-refused error takes."""

    def __init__(self, message, sqlstate):
        super().__init__(message)
        self.sqlstate = sqlstate


class _DummyConn:
    """A `with self._conn() as conn:` stand-in that neither connects nor fails, so `_run`
    reaches `fn(conn)` instead of dying on the connection attempt first. Lets the test drive
    a failure INSIDE the query - the shape a uniqueness violation or a rejected value
    actually takes - rather than the connection-refused shape, which never carries a
    conv_id/oid regardless of what `_run` does with the exception."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, *args, **kwargs):
        pass


def test_run_never_lets_a_driver_message_leak_through():
    """Pins the actual guarantee: `_run` (not `_safe_reason` in isolation) must build
    `ConversationStoreUnavailable`'s message from `_safe_reason(exc)`, never `str(exc)`,
    even when the driver error's own text quotes the conv_id, the oid, and the question.

    Exercises `_run` itself - `_ensure_schema` is skipped via `_schema_done = True` and
    `_conn` is replaced with `_DummyConn` so the call reaches `fn(conn)` and raises there,
    the same place a real uniqueness violation or rejected value would raise. A
    connection-refused DSN (see test below) cannot pin this: that failure never reaches a
    query, so mutating `_run` from `_safe_reason(exc)` to `str(exc)` would leave a
    connection-refused-only test passing while still leaking a driver message on every
    failure that actually carries one."""
    s = PgConversationStore("dbname=nope host=127.0.0.1 port=1 connect_timeout=1")
    s._schema_done = True
    s._conn = _DummyConn

    def _boom(conn):
        raise _FakeDriverError(
            'duplicate key value violates unique constraint "conversation_turns_pkey" '
            "DETAIL: Key (conv_id, user_oid, seq)=(c1, bob, 0) already exists, question "
            '"what is the leave policy?".', sqlstate="23505")

    try:
        s._run(_boom)
        assert False, "expected ConversationStoreUnavailable"
    except ConversationStoreUnavailable as e:
        assert "bob" not in str(e) and "c1" not in str(e)
        assert "leave policy" not in str(e)
        assert str(e) == "_FakeDriverError (sqlstate 23505)"


def test_pg_store_down_raises_unavailable_on_connection_refused():
    """Coverage for the actual shape an unreachable DSN produces: a connection error
    raised inside `_ensure_schema()`, before conv_id/user_oid ever reach the driver. This
    does NOT pin the data-free guarantee - a connection-refused message never contains a
    conv_id or oid no matter how `_run` builds its reason string. That guarantee is pinned
    by test_safe_reason_strips_sensitive_values_even_when_the_driver_quotes_them, above."""
    s = PgConversationStore("dbname=nope host=127.0.0.1 port=1 connect_timeout=1")
    try:
        s.history("c1", "bob")
        assert False, "expected ConversationStoreUnavailable"
    except ConversationStoreUnavailable:
        pass


class _FakeQueryService:
    """Stands in for QueryService.answer/answer_stream - always returns
    retrieved_docs=["hr-policy-1"] so cited_docs round-tripping is checkable."""

    def answer(self, user_oid, question, llm=None, tenant_id=None):
        return QueryResult(answer=f"answer to: {question}", citations=[],
                           retrieved_docs=["hr-policy-1"])

    def answer_stream(self, user_oid, question, llm=None, tenant_id=None):
        yield {"type": "token", "text": "answer"}
        yield {"type": "done", "answer": "answer to: " + question, "citations": [],
               "retrieved_docs": ["hr-policy-1"]}


class _FakeLlm:
    """condense_question is a no-op that echoes its input, and tracks whether it was
    called at all - the thing test_condense_runs_on_the_restarted_processes_history
    needs to pin (history must be non-empty across a fresh service instance)."""

    def __init__(self):
        self.condense_called = False

    def condense_question(self, question, history):
        self.condense_called = True
        return question

    def answer(self, question, context_chunks):
        return {"answer": question, "citations": []}


class _DownStore:
    """A store that is configured but unreachable on every call - the shape
    PgConversationStore takes when Postgres itself is down, not merely slow."""

    def history(self, conv_id, user_oid):
        raise ConversationStoreUnavailable("_DownStore (sqlstate None)")

    def append(self, conv_id, user_oid, turn):
        raise ConversationStoreUnavailable("_DownStore (sqlstate None)")

    def drop_user(self, user_oid):
        raise ConversationStoreUnavailable("_DownStore (sqlstate None)")


def _make_service(store=None):
    return ConversationService(_FakeQueryService(), _FakeLlm(),
                               store=store if store is not None else InMemoryConversationStore())


def _make_service_capturing_condense(store):
    llm = _FakeLlm()
    return ConversationService(_FakeQueryService(), llm, store=store), llm


def test_service_records_cited_docs_from_query_result():
    svc = _make_service()                      # fake qs returns retrieved_docs=["hr-policy-1"]
    svc.ask("bob", "c1", "leave policy?")
    assert svc.history("bob", "c1")[0].cited_docs == ["hr-policy-1"]


def test_a_second_service_over_the_same_store_continues_the_thread():
    store = InMemoryConversationStore()        # stands in for the same Postgres across a deploy
    _make_service(store).ask("bob", "c1", "q1")
    svc2 = _make_service(store)
    svc2.ask("bob", "c1", "q2")
    assert [t.question for t in svc2.history("bob", "c1")] == ["q1", "q2"]


def test_condense_runs_on_the_restarted_processes_history():
    store = InMemoryConversationStore()
    _make_service(store).ask("bob", "c1", "q1")
    svc2, llm = _make_service_capturing_condense(store)
    svc2.ask("bob", "c1", "and how do I apply?")
    assert llm.condense_called      # history was non-empty across the "restart"


def test_ask_degrades_to_no_history_when_store_read_fails_but_history_raises():
    svc = _make_service(_DownStore())          # history() raises ConversationStoreUnavailable
    r = svc.ask("bob", "c1", "q1")             # must still answer (store outage != outage)
    assert r.answer
    try:
        svc.history("bob", "c1")
        assert False, "history() must stay honest and raise"
    except ConversationStoreUnavailable:
        pass


# ---- ask_stream: the streaming twin, mirroring the four tests above (#596 review
# finding 1) - `/chat/stream` in server/app.py drives THIS method, not `ask`, so a
# mutation to its guarded read, guarded append, or cited_docs extraction went undetected
# until these were added. `_drain` exhausts the generator the way a real SSE loop does,
# since `ask_stream`'s history-recording append only runs after the last event is pulled.

def _drain(gen):
    return list(gen)


def test_ask_stream_records_cited_docs_from_done_event():
    svc = _make_service()                      # fake qs yields a done event with retrieved_docs
    _drain(svc.ask_stream("bob", "c1", "leave policy?"))
    assert svc.history("bob", "c1")[0].cited_docs == ["hr-policy-1"]


def test_a_second_service_over_the_same_store_continues_the_thread_streamed():
    store = InMemoryConversationStore()        # stands in for the same Postgres across a deploy
    _drain(_make_service(store).ask_stream("bob", "c1", "q1"))
    svc2 = _make_service(store)
    _drain(svc2.ask_stream("bob", "c1", "q2"))
    assert [t.question for t in svc2.history("bob", "c1")] == ["q1", "q2"]


def test_condense_runs_on_the_restarted_processes_history_streamed():
    store = InMemoryConversationStore()
    _drain(_make_service(store).ask_stream("bob", "c1", "q1"))
    svc2, llm = _make_service_capturing_condense(store)
    _drain(svc2.ask_stream("bob", "c1", "and how do I apply?"))
    assert llm.condense_called      # history was non-empty across the "restart"


def test_ask_stream_degrades_to_no_history_when_store_read_fails_but_history_raises():
    svc = _make_service(_DownStore())          # history() raises ConversationStoreUnavailable
    events = _drain(svc.ask_stream("bob", "c1", "q1"))  # must still stream (store outage != outage)
    done = [e for e in events if e.get("type") == "done"]
    assert done and done[0]["answer"]
    try:
        svc.history("bob", "c1")
        assert False, "history() must stay honest and raise"
    except ConversationStoreUnavailable:
        pass


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
