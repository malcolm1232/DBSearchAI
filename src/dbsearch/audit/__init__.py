"""Query + access audit log (#45) — per-query access-reason transparency on top of the LAW-2
trim. Records, IN-TENANT, who asked what and which docs they were authorized to see.

LAW 1 (metadata only): this stores the user's own question text and the authorized doc
*identities* (external_ids/titles) + counts — NEVER document CONTENT, and never crosses the
control-plane boundary. It is a data-plane governance artifact.

#623, LAW 6: this used to be an in-memory ring buffer and NOTHING ELSE, while the note here
said a Postgres adapter "can replace it later". It could not wait: the owner-facing panel
built on it (#593, "Questions you have asked") tells the user "Every answer is recorded
against the person who asked it", and an api restart emptied it - proven on dbsearch.ai
driving GOAL_ACCEPTANCE step 9, where conversations, shares and grants all came back and
this alone did not. `PgAuditLog` is that adapter, behind the same tiny interface, wired the
same way every other store in this repo is: durable when a DSN is configured, memory-only
otherwise (the self-host contract - a demo box keeps working, it just forgets on restart).
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field


class AuditLogUnavailable(Exception):
    """Configured but unreachable.

    The message is data-free - exception class name plus SQLSTATE, nothing more - and that
    rule (grant_store.py's) binds harder here than anywhere else it is applied: the values
    this store writes ARE the user's own question text, so a driver message that quoted the
    failing statement would put a question, verbatim, into a log line. Everything this
    module refuses to leak into an answer it must also refuse to leak into an error.
    """


def _safe_reason(exc: BaseException) -> str:
    sqlstate = getattr(exc, "sqlstate", None)
    return (f"{type(exc).__name__} (sqlstate {sqlstate})" if sqlstate
            else type(exc).__name__)


def _iso(value) -> str:
    """A `timestamptz` off a row, as the string shape `AuditEntry.ts` has always been.

    The wire contract predates the durable store: `to_dict()` feeds /me/questions and
    /admin/audit, and admin.js renders `ts` as text. Returning a datetime here because that
    is what the column holds would change the response shape of two routes as a side effect
    of where the rows are kept, which is exactly the kind of coupling the port is meant to
    prevent. Defensive about a driver that hands back a string for the same reason
    conversation_store._iso is: a log with an oddly-shaped time is worse than one that 500s.
    """
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


@dataclass
class AuditEntry:
    ts: str
    user: str
    question: str
    surface: str                 # "ask" | "chat" | "search" | ...
    authorized_docs: list[str] = field(default_factory=list)  # external_ids the user could see

    @property
    def n_authorized(self) -> int:
        return len(self.authorized_docs)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["n_authorized"] = self.n_authorized
        return d


class InMemoryAuditLog:
    """Bounded, newest-last in-memory audit trail. The unconfigured (no-DSN) path only.

    Named `InMemoryAuditLog` rather than `AuditLog` since #623 so that no call site can be
    ambiguous about which half it holds - the same reason conversation_store.py has no bare
    `ConversationStore`. A deployment that reaches for the short name and gets the forgetful
    one is the bug this card fixed.
    """
    def __init__(self, capacity: int = 1000) -> None:
        self._entries: list[AuditEntry] = []
        self._cap = capacity
        self._lock = threading.Lock()

    def record(self, user: str, question: str, surface: str,
               authorized_docs: list[str], ts: str) -> AuditEntry:
        entry = AuditEntry(ts=ts, user=user, question=question, surface=surface,
                           authorized_docs=list(authorized_docs))
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._cap:
                self._entries = self._entries[-self._cap:]
        return entry

    def recent(self, limit: int = 50, user: "str | None" = None) -> list[AuditEntry]:
        """Most-recent-first. With `user`, ONLY that user's entries.

        #593: filter, THEN limit - never the other way round. The browser used to fetch the
        newest 25 rows deployment-wide and filter them client-side, so on any box where a
        colleague was also asking questions the owner's own history rendered as "No questions
        yet." Windowing first makes the answer depend on other people's activity, which is
        both wrong and, since the caller can never widen it back, silently wrong.
        """
        with self._lock:
            rows = (self._entries if user is None
                    else [e for e in self._entries if e.user == user])
            return list(reversed(rows[-limit:]))

    def drop_user(self, user_oid: str) -> int:
        """#623: the retention sweep's reach, extended to this store because durability
        created the obligation. While the trail died with the process, #576 not covering it
        cost nothing - a swept account's questions were gone by the next deploy anyway. Rows
        that now outlive every deploy have to be swept explicitly or the fix for a
        durability bug quietly becomes a retention one."""
        with self._lock:
            keep = [e for e in self._entries if e.user != user_oid]
            dropped = len(self._entries) - len(keep)
            self._entries = keep
            return dropped

    def __len__(self) -> int:
        return len(self._entries)


class PgAuditLog:
    """The durable half (#623). Same four methods, same shapes, one SELECT per read.

    READS GO THROUGH TO POSTGRES rather than hydrating a cache at boot, for
    conversation_store.py's reason: this is not authorization and it is not on the
    per-request hot path (two owner-facing panels call it), so reading through means a
    restarted process simply continues the trail with no hydration step and nothing that can
    go stale. It is also what makes the durability testable - a second instance over the
    same DSN is what a restart looks like from the data's side.
    """

    def __init__(self, dsn: str, table: str = "query_audit",
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
                        id              bigserial PRIMARY KEY,
                        ts              timestamptz NOT NULL,
                        user_oid        text NOT NULL,
                        question        text NOT NULL,
                        surface         text NOT NULL,
                        authorized_docs text[] NOT NULL DEFAULT '{{}}'
                    )""")
                # The read is always "this user's newest N", so the index carries the filter
                # and the order together and the limit is satisfied by walking it. Without
                # it, #593's filter-then-limit becomes a full scan that grows with every
                # question anybody has ever asked - the read is correct either way, so this
                # would degrade silently rather than break.
                conn.execute(
                    f"""CREATE INDEX IF NOT EXISTS {self._table}_user_id_desc
                        ON {self._table} (user_oid, id DESC)""")
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
                "audit log %s unavailable: %s", self._table, reason)
        raise AuditLogUnavailable(reason)

    def record(self, user: str, question: str, surface: str,
               authorized_docs: list[str], ts: str) -> AuditEntry:
        """Write one row and hand back the entry the caller would have got from memory.

        `%s::timestamptz` rather than letting the driver adapt the value: `ts` arrives as an
        ISO-8601 STRING (it is `Edition._now()`, shared with the telemetry emit that has
        always taken a string), and a text column comparison or an implicit cast is a thing
        that works until a driver version changes its mind. The cast says what is meant.

        WHO CATCHES THE FAILURE IS THE CALLER'S DECISION AND IT IS NOT THE SAME EVERYWHERE:
        `Edition.record_query` deliberately does not let an outage here fail a question that
        has already been answered - see its docstring - while the two READ routes turn the
        same exception into a 503 rather than an empty list. This method's job is only to
        make the failure loud and typed instead of silent.
        """
        entry = AuditEntry(ts=ts, user=user, question=question, surface=surface,
                           authorized_docs=list(authorized_docs))

        def _insert(conn):
            conn.execute(
                f"""INSERT INTO {self._table}
                    (ts, user_oid, question, surface, authorized_docs)
                    VALUES (%s::timestamptz, %s, %s, %s, %s)""",
                (ts, user, question, surface, entry.authorized_docs))
            return entry
        return self._run(_insert)

    def recent(self, limit: int = 50, user: "str | None" = None) -> list[AuditEntry]:
        """Most-recent-first, and #593's filter-then-limit is the WHERE clause preceding the
        LIMIT - the property is structural in SQL in a way it has to be argued for in Python.

        ORDERED BY `id DESC`, NOT `ts DESC`. `ts` is supplied by the caller, so two rows can
        carry the identical timestamp (a fast pair of questions, a fixed clock in a test) and
        ordering by it alone lets two reads of the same table disagree about which came
        first. `id` is the insertion order Postgres itself assigned, which is what "recent"
        has always meant here. It never returns the rows themselves in a different order than
        the memory store would.
        """
        def _load(conn):
            if user is None:
                rows = conn.execute(
                    f"""SELECT ts, user_oid, question, surface, authorized_docs
                        FROM {self._table} ORDER BY id DESC LIMIT %s""",
                    (limit,)).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT ts, user_oid, question, surface, authorized_docs
                        FROM {self._table} WHERE user_oid = %s
                        ORDER BY id DESC LIMIT %s""",
                    (user, limit)).fetchall()
            return [AuditEntry(ts=_iso(ts), user=u, question=q, surface=s,
                               authorized_docs=list(docs or []))
                    for ts, u, q, s, docs in rows]
        return self._run(_load)

    def drop_user(self, user_oid: str) -> int:
        """The Postgres sibling of `InMemoryAuditLog.drop_user` - read that one for why the
        retention sweep had to grow a call for this store. Matched for EQUALITY: an audit row
        belongs to exactly the account that asked, and no prefix match has any meaning here.
        """
        def _drop(conn):
            row = conn.execute(
                f"SELECT COUNT(*) FROM {self._table} WHERE user_oid = %s",
                (user_oid,)).fetchone()
            conn.execute(f"DELETE FROM {self._table} WHERE user_oid = %s", (user_oid,))
            return int(row[0])
        return self._run(_drop)
