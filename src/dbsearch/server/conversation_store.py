"""#596: conversations survive a restart.

Unlike grant_store.py, this store IS the read path: history is read per ask. Grants kept
their in-memory dict as the only read surface because principal expansion runs on every
request and is authorization; conversation history is neither - it is one SELECT per
question, against a latency budget dominated by retrieval + LLM generation, and reading
through means a restarted process simply continues the thread with no hydration step and
no cache to go stale. Unconfigured (no DSN) runs memory-only, same as manifest_store.py.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from dbsearch.query.conversation import Turn


class ConversationStoreUnavailable(Exception):
    """Configured but unreachable. Message is data-free (class name + SQLSTATE only) -
    a driver message can quote a stored value, and question/answer TEXT lives here, which
    is worse than the oids grant_store.py protects."""


def _safe_reason(exc: BaseException) -> str:
    sqlstate = getattr(exc, "sqlstate", None)
    return (f"{type(exc).__name__} (sqlstate {sqlstate})" if sqlstate
            else type(exc).__name__)


#: #611. The owner-facing question log needs a time for every question, and `Turn` carries
#: none - deliberately, and it is not being given one here: `conversation_transcript` in
#: app.py reasons explicitly from "Turn carries no timestamp" when it argues that a shared
#: transcript's two halves are consecutive rather than interleaved, and several surfaces
#: serialize turns. The TIME OF AN APPEND is a fact the STORE owns, exactly as
#: `conversation_turns.asked_at` (which has existed since #596) already is on the Postgres
#: side. So the memory store stamps its own rows and both stores answer `questions_for` with
#: the same (question, iso-8601-or-None) shape, which is the whole of what the log reads.
def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value) -> "str | None":
    """A `timestamptz` off a row, as the wire shape. Defensive about a driver that hands back
    a string (or nothing) rather than a datetime - an owner-facing log must not 500 because a
    timestamp arrived in an unexpected form; a missing time is a worse log, not a broken one."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class InMemoryConversationStore:
    def __init__(self) -> None:
        # (turn, asked_at) pairs in ONE structure rather than a Turn list beside a parallel
        # dict of times: two structures keyed the same way are two things every future
        # `drop_user`-shaped method has to remember to clear, and the one that gets forgotten
        # is the one holding a swept account's questions.
        self._turns: dict[tuple[str, str], list[tuple[Turn, str]]] = {}
        self._lock = threading.Lock()

    def append(self, conv_id: str, user_oid: str, turn: Turn) -> None:
        with self._lock:
            self._turns.setdefault((conv_id, user_oid), []).append((turn, _stamp()))

    def history(self, conv_id: str, user_oid: str) -> list[Turn]:
        with self._lock:
            return [t for t, _ in self._turns.get((conv_id, user_oid), [])]

    def drop_user(self, user_oid: str) -> int:
        with self._lock:
            dead = [k for k in self._turns if k[1] == user_oid]
            for k in dead:
                del self._turns[k]
            return len(dead)

    def users_for_conv(self, conv_id: str, oid_prefix: str) -> list[str]:
        """WHO has turns in this conversation under keys starting `oid_prefix`, in FIRST-SEEN
        order. #611's enumeration: the link question log passes `link:<share_id>:` and gets
        that share's visitor forks, in the order they first asked, which is what makes the
        owner-facing ordinals (visitor 1, 2, 3) mean anything.

        A BLANK PREFIX IS REFUSED rather than treated as "everyone". `""` matches the
        GRANTOR's own key too, and every other account that happens to share this
        client-chosen conv_id, so a caller that forwarded an empty value would silently widen
        an owner-scoped enumeration into a deployment-wide one. There is no caller that wants
        every user in a conversation; if one ever exists it can ask for that in words.

        Dict insertion order IS first-seen order here - a key is created by its owner's first
        append and never re-created - which is the same fact `PgConversationStore` gets from
        `MIN(asked_at)`."""
        if not oid_prefix:
            raise ValueError("users_for_conv needs a prefix - a blank one matches everybody")
        with self._lock:
            return [oid for (c, oid) in self._turns if c == conv_id
                    and oid.startswith(oid_prefix)]

    def conversations_for(self, user_oid: str) -> "list[dict]":
        """#602: THIS user's own threads, newest first, so the owner has a door back into them.

        `user_oid` IS MATCHED FOR EQUALITY, and that is the whole security property of this
        method. Its sibling `users_for_conv` directly above matches by PREFIX because
        enumerating a link share's visitor forks is what it is for, and the obvious way to write
        this method is to copy that shape - which would be wrong in two directions at once. A
        link visitor's turns are stored under `link:<share_id>:<visitor_id>` INSIDE THE OWNER'S
        OWN conv_id (ADR 0021), so a loose match would put a stranger's typed question in the
        owner's list; and account ids are not guaranteed to be prefix-free, so a loose match
        would also hand one account another account's threads. Equality has neither failure
        mode, and it is also why a blank `user_oid` needs no explicit refusal here (unlike
        `users_for_conv`): no key is the empty string, so `""` matches nothing rather than
        everybody.

        NEWEST FIRST by the last question asked. The thread the owner wants back after a reload
        is nearly always the one she was just in, and `last_asked_at` is the only fact that can
        say which - `seq` is dense per thread and says nothing across threads. `conv_id` breaks
        a tie deterministically, so two reads of the same data never reorder and read as
        activity that did not happen.

        Content, deliberately and narrowly: `first_question` is the caller's OWN opening
        question returned to the caller alone, which is what makes a row nameable at all. The
        ANSWER is not here, and the route truncates the question for display."""
        with self._lock:
            rows = [{"conv_id": conv,
                     "first_question": turns[0][0].question,
                     "turns": len(turns),
                     "last_asked_at": turns[-1][1]}
                    for (conv, oid), turns in self._turns.items()
                    if oid == user_oid and turns]
        rows.sort(key=lambda r: (r["last_asked_at"] or "", r["conv_id"]), reverse=True)
        return rows

    def questions_for(self, conv_id: str, user_oid: str) -> "list[tuple[str, str | None]]":
        """What this key ASKED, and when - never what it was answered.

        The projection is the point, not a formality: #611's log exists so an owner can see
        what strangers ask, and it must not become a second channel for the synthesized answer
        text (which is document content, reproduced into a new place, on a surface whose whole
        justification is the questions). `PgConversationStore.questions_for` makes the same
        statement in SQL - `SELECT question, asked_at`, so the answer column never leaves the
        database at all - and this one must stay its equal. DO NOT ADD THE ANSWER HERE."""
        with self._lock:
            return [(t.question, at)
                    for t, at in self._turns.get((conv_id, user_oid), [])]


class PgConversationStore:
    """conversation_turns in the deployment's compose-managed Postgres (PGVECTOR_DSN).
    Connects per operation; DDL is CREATE TABLE IF NOT EXISTS on first touch with
    `_schema_done` set only after commit - manifest_store.py's `_ensure_schema` docstring
    is the full account; this is the same pattern verbatim."""

    def __init__(self, dsn: str, table: str = "conversation_turns",
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
                        conv_id    text NOT NULL,
                        user_oid   text NOT NULL,
                        seq        int  NOT NULL,
                        question   text NOT NULL,
                        standalone text NOT NULL,
                        answer     text NOT NULL,
                        cited_docs text[] NOT NULL DEFAULT '{{}}',
                        asked_at   timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (conv_id, user_oid, seq)
                    )""")
                # #633, as a separate idempotent ALTER rather than a column in the CREATE:
                # every deployment that already has this table skips the CREATE entirely
                # (IF NOT EXISTS), so a column added only there would exist on new boxes and
                # nowhere else - and the failure would surface as a reopened thread quietly
                # losing its quotes on exactly the deployments that have history worth
                # reopening. Turns written before this column has a `citations` of [], which
                # `conversation_transcript` reads as "derive the rows from cited_docs".
                conn.execute(
                    f"""ALTER TABLE {self._table}
                        ADD COLUMN IF NOT EXISTS citations jsonb NOT NULL DEFAULT '[]'::jsonb""")
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
                "conversation store %s unavailable: %s", self._table, reason)
        raise ConversationStoreUnavailable(reason)

    def append(self, conv_id: str, user_oid: str, turn: Turn) -> None:
        """`seq` is derived from the table's current state (`MAX(seq) + 1`), not a
        client-generated key the way `grant_id` is in grant_store.py - that is what makes
        this different from every other store in the repo. At default READ COMMITTED, two
        concurrent appends to the SAME (conv_id, user_oid) - a double-submit, a client retry
        after a timed-out first request, the same conversation open in two tabs - can both
        run the `SELECT MAX(seq) + 1` before either has committed its INSERT, both compute
        the identical next seq, and the second's INSERT then violates the
        (conv_id, user_oid, seq) PRIMARY KEY. `_run` catches that as a generic `Exception`
        and turns it into `ConversationStoreUnavailable` - the second question the user
        actually asked is silently dropped, not merely delayed, and looks to them like the
        store failed rather than like a race.

        `pg_advisory_xact_lock` over the (conv_id, user_oid) pair serializes the two
        transactions instead: the second acquirer blocks until the first commits (the lock
        is released automatically at transaction end, which is where `_conn()`'s `with`
        block commits or rolls back), so it reads a `MAX(seq)` that already reflects the
        first turn and allocates the next one instead of colliding with it. `hashtext` maps
        the text keys onto the int4 pair the two-argument form of the lock takes; a hash
        collision between two different (conv_id, user_oid) pairs only costs unnecessary
        serialization between unrelated conversations, never an incorrect seq, so it does
        not need to be collision-free."""
        # Lazily, like `_conn`'s psycopg import: an unconfigured deployment runs memory-only
        # and must not need the driver installed to import this module.
        from psycopg.types.json import Jsonb

        def _append(conn):
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (conv_id, user_oid))
            conn.execute(
                f"""INSERT INTO {self._table}
                    (conv_id, user_oid, seq, question, standalone, answer, cited_docs,
                     citations)
                    SELECT %s, %s, COALESCE(MAX(seq) + 1, 0), %s, %s, %s, %s, %s
                    FROM {self._table} WHERE conv_id = %s AND user_oid = %s""",
                (conv_id, user_oid, turn.question, turn.standalone, turn.answer,
                 turn.cited_docs, Jsonb(turn.citations or []), conv_id, user_oid))
        self._run(_append)

    def history(self, conv_id: str, user_oid: str) -> list[Turn]:
        def _load(conn):
            rows = conn.execute(
                f"""SELECT question, standalone, answer, cited_docs, citations
                    FROM {self._table}
                    WHERE conv_id = %s AND user_oid = %s ORDER BY seq""",
                (conv_id, user_oid)).fetchall()
            return [Turn(question=q, standalone=s, answer=a, cited_docs=list(c or []),
                         citations=list(cit or []))
                    for q, s, a, c, cit in rows]
        return self._run(_load)

    def users_for_conv(self, conv_id: str, oid_prefix: str) -> list[str]:
        """The Postgres sibling of `InMemoryConversationStore.users_for_conv` - same contract,
        same blank-prefix refusal, same first-seen order.

        `left(user_oid, %s) = %s` RATHER THAN `LIKE %s || '%'`. The values in play today are
        `link:<share_id>:` with a uuid hex share_id, so no wildcard can appear in one - but a
        LIKE pattern gives `%` and `_` meaning, and the day a prefix carries either (a
        different id scheme, a caller that builds one from a request value) the match widens
        SILENTLY, and what it widens is an owner-scoped enumeration of other people's
        conversation keys. `left()` has no pattern language to escape, so there is nothing to
        get wrong and nothing for a later caller to have to remember.

        ORDERED BY `MIN(asked_at)`, which is the only column that can express "first seen":
        `seq` is dense PER (conv_id, user_oid), so every fork's first turn is seq 0 and
        ordering by it would say nothing. `user_oid` breaks a tie deterministically - two forks
        whose first turns share a timestamp must not swap ordinals between two reads of the
        same log."""
        if not oid_prefix:
            raise ValueError("users_for_conv needs a prefix - a blank one matches everybody")

        def _load(conn):
            rows = conn.execute(
                f"""SELECT user_oid FROM {self._table}
                    WHERE conv_id = %s AND left(user_oid, %s) = %s
                    GROUP BY user_oid ORDER BY MIN(asked_at), user_oid""",
                (conv_id, len(oid_prefix), oid_prefix)).fetchall()
            return [r[0] for r in rows]
        return self._run(_load)

    def conversations_for(self, user_oid: str) -> "list[dict]":
        """The Postgres sibling of `InMemoryConversationStore.conversations_for` - same
        contract, same equality scoping, same newest-first order. Read its docstring for why
        the scoping is what it is; the SQL below is that argument made in one predicate.

        `user_oid = %s` AND NOT `left(user_oid, %s) = %s`. The method directly above uses
        `left()` on purpose - a prefix match is its contract - and this one must not, in either
        spelling. A prefix here would pull in this conversation's `link:<share_id>:<visitor_id>`
        forks (a stranger's typed question, on the owner's screen, named as her thread) and any
        account whose oid happens to extend this one. Equality is the only match that answers
        "whose thread is this?".

        THE ROW IS BUILT INSIDE THE GROUP, not by a second query per conversation:
        `(array_agg(question ORDER BY seq))[1]` is the MIN(seq) turn's question - the thread's
        opening one - `COUNT(*)` is that key's own turn count, and `MAX(asked_at)` is both the
        row's timestamp and its sort key. A per-conversation follow-up read would be N+1 round
        trips and, worse, could disagree with the grouping it came from if a turn landed in
        between."""
        def _load(conn):
            rows = conn.execute(
                f"""SELECT conv_id, (array_agg(question ORDER BY seq))[1],
                           COUNT(*), MAX(asked_at)
                    FROM {self._table} WHERE user_oid = %s
                    GROUP BY conv_id ORDER BY MAX(asked_at) DESC, conv_id DESC""",
                (user_oid,)).fetchall()
            return [{"conv_id": conv, "first_question": question, "turns": int(n),
                     "last_asked_at": _iso(at)} for conv, question, n, at in rows]
        return self._run(_load)

    def questions_for(self, conv_id: str, user_oid: str) -> "list[tuple[str, str | None]]":
        """What this key asked, and when. `SELECT question, asked_at` - THE ANSWER COLUMN IS
        NOT IN THIS QUERY AND MUST NOT BE ADDED.

        That is a structural guarantee rather than a comment: #611's owner-facing log reads
        this method, so the synthesized answer text never leaves Postgres for that surface at
        all - a caller cannot leak a field it was never handed. Adding `answer` here to save a
        query for some future reader would quietly reopen the content channel; give that
        reader its own method."""
        def _load(conn):
            rows = conn.execute(
                f"""SELECT question, asked_at FROM {self._table}
                    WHERE conv_id = %s AND user_oid = %s ORDER BY seq""",
                (conv_id, user_oid)).fetchall()
            return [(q, _iso(at)) for q, at in rows]
        return self._run(_load)

    def drop_user(self, user_oid: str) -> int:
        def _drop(conn):
            rows = conn.execute(
                f"""SELECT COUNT(DISTINCT conv_id) FROM {self._table}
                    WHERE user_oid = %s""", (user_oid,)).fetchone()
            conn.execute(f"DELETE FROM {self._table} WHERE user_oid = %s", (user_oid,))
            return int(rows[0])
        return self._run(_drop)
