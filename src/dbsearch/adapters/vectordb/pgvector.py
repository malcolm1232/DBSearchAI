"""PgVectorIndex — IndexPort backed by Postgres + the pgvector extension.

Optional dep: pip install '.[vectordb]'  (psycopg + pgvector)

LAW 1: runs in the customer's own Postgres (in-tenant) — data never leaves.
LAW 2 + ADR 0012: retrieval carries TWO mandatory WHERE clauses:

    WHERE tenant_id = %s                        -- ADR 0012: tenant partition
      AND allowed_principals && %s::text[]      -- LAW 2: per-document ACL

A chunk is only returned if it belongs to the caller's tenant AND its
allowed-principals array overlaps the user's principal set. There is no query path
without both clauses — a leak needs an ACL collision AND a tenant-id collision.
"""
from __future__ import annotations

import json

from dbsearch.core.models import Chunk, CorpusStatus, DocACL
from dbsearch.ports.base import IndexPort, ObjectStorePort


def _vec_literal(vec: list[float]) -> str:
    # pgvector accepts a bracketed string literal cast to ::vector.
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _doorway_sql(scope) -> "tuple[str, list]":
    """The partition predicate (#582 / ADR 0019 D3), as SQL.

    An EMPTY doorway - the overwhelmingly common case - emits the ORIGINAL single
    predicate, so ordinary retrieval keeps exactly the query plan and the tenant index it
    has always used. A non-empty doorway adds an EXISTS over two parallel arrays, which
    keeps the parameter count fixed at three no matter how many documents were shared.

    This must stay semantically identical to `ReadScope.allows`;
    tests/selftest_582_doorway_parity.py pins the two against one truth table.
    """
    if not scope.doorway:
        return "tenant_id = %s", [scope.partition]
    pairs = sorted(scope.doorway)
    return ("""( tenant_id = %s
                 OR EXISTS (SELECT 1 FROM unnest(%s::text[], %s::text[]) AS d(t, x)
                            WHERE d.t = tenant_id AND d.x = doc_external_id) )""",
            [scope.partition, [t for t, _ in pairs], [x for _, x in pairs]])


def _pg_text(s: str) -> str:
    """#367: PostgreSQL text cannot contain NUL. The pipeline strips it from chunk text, but
    a doc TITLE or URI reaches this column straight from the connector — and one 0x00 aborts
    the whole crawl mid-index. Backstop at the constraint, so no producer can 500 an ingest."""
    return s.replace("\x00", " ") if s and "\x00" in s else s


class PgVectorIndex(IndexPort):
    # #440: AUTOCOMMIT, and it is load-bearing rather than a preference.
    #
    # psycopg opens a transaction on the first statement, including a plain SELECT, and holds
    # it until commit/rollback. Every read here (search, chunk_count_for, list_doc_acls,
    # list_doc_segments) returned without committing, so a connection that had served one
    # search sat in `idle in transaction` indefinitely.
    #
    # That is not merely untidy. On prod it took the whole database down: a second process
    # constructed a PgVectorIndex, ensure_schema()'s ALTER TABLE queued for an ACCESS
    # EXCLUSIVE lock behind that idle transaction, and once an ACCESS EXCLUSIVE request is
    # queued Postgres makes every LATER query wait behind it - including plain SELECTs on the
    # same table. All query traffic stalled until the backends were terminated.
    #
    # Autocommit means a read leaves the connection IDLE, holding nothing. Writes that must
    # stay atomic (upsert, delete, ensure_schema) declare that explicitly with
    # `self._conn.transaction()`, which starts a real transaction block even in autocommit
    # mode. NOTE: inside that block `commit()` RAISES ("Explicit commit() forbidden within a
    # Transaction context") - the context manager commits on clean exit. So no method here
    # calls commit() any more.
    def __init__(self, dsn: str, store: ObjectStorePort, table: str = "chunks", dim: int = 1536) -> None:
        import psycopg

        self._conn = psycopg.connect(dsn, autocommit=True)
        # Not paranoia, and not decoration. The write paths below rely on
        # `self._conn.transaction()` to commit them, and it only does that in AUTOCOMMIT
        # mode - with autocommit off it opens a nested block and leaves the OUTER
        # transaction uncommitted, so every upsert, delete and DDL would be silently
        # discarded on close. Since no method calls commit() any more, turning autocommit
        # off would lose data quietly. Fail loudly instead. (Found while writing the #440
        # regression test: with autocommit reverted, ensure_schema's CREATE TABLE never
        # became visible to a second connection.)
        assert self._conn.autocommit, (
            "PgVectorIndex requires an autocommit connection: its writes are committed by "
            "conn.transaction(), which only commits in autocommit mode (#440)")
        self._store = store
        self._table = table
        self._dim = dim

    # Columns ensure_schema() is responsible for. Kept beside the DDL so the check and the
    # thing it checks cannot drift: a column added below without being named here would be
    # created once on a fresh box and never backfilled on an existing one.
    _REQUIRED_COLUMNS = ("chunk_id", "tenant_id", "doc_external_id", "text_ref", "title",
                         "uri", "content", "locator", "allowed_principals", "embedding",
                         "owner_oid", "doc_bytes")

    def schema_is_current(self) -> bool:
        """Is the table already shaped correctly, asked WITHOUT taking a lock on it (#440).

        Reads pg_catalog only. `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` still acquires
        ACCESS EXCLUSIVE even when it turns out to be a no-op - Postgres takes the lock in
        order to find out - which is why a startup that changed nothing could still stall
        every reader. Asking the catalog first means the steady-state path takes no lock at
        all.

        #916: the PRIMARY KEY is part of "shaped correctly", not just the column set. An
        established deployment has every column, so a column-only check would skip
        ensure_schema forever and the single-column legacy PK - the cross-tenant collision
        - would never migrate."""
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT attname FROM pg_attribute
                    WHERE attrelid = to_regclass(%s) AND attnum > 0 AND NOT attisdropped""",
                (self._table,),
            )
            present = {r[0] for r in cur.fetchall()}
        return present.issuperset(self._REQUIRED_COLUMNS) \
            and self._primary_key() == ["tenant_id", "chunk_id"]

    def _primary_key(self) -> list[str]:
        """The table's PK columns in index order, from pg_catalog (lock-free)."""
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT a.attname
                     FROM pg_index i
                     JOIN pg_attribute a
                       ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                    WHERE i.indrelid = to_regclass(%s) AND i.indisprimary
                    ORDER BY array_position(i.indkey, a.attnum)""",
                (self._table,),
            )
            return [r[0] for r in cur.fetchall()]

    def ensure_schema(self, *, force: bool = False) -> None:
        """Create/patch the schema, but ONLY when it is actually out of shape (#440).

        The standing rule is that schema DDL does not belong on the startup path. Removing
        this call outright would break fresh dev rigs and self-host boxes, which legitimately
        rely on it to provision themselves, so instead the DDL is skipped whenever there is
        nothing to do - which on any established deployment is always. That removes the lock
        from the hot path while a first boot still provisions itself.

        An operator running migrations out of band can force the check off entirely with
        DBSEARCH_PG_SKIP_DDL=1."""
        import os

        if os.environ.get("DBSEARCH_PG_SKIP_DDL") == "1":
            return
        if not force and self.schema_is_current():
            return
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {self._table} (
                    chunk_id text NOT NULL,
                    tenant_id text NOT NULL,
                    doc_external_id text NOT NULL,
                    text_ref text, title text, uri text, content text,
                    locator jsonb,
                    allowed_principals text[] NOT NULL,
                    embedding vector({self._dim}),
                    PRIMARY KEY (tenant_id, chunk_id)
                )"""
            )
            # #916: a chunk's identity is (tenant_id, chunk_id) - the InMemoryIndex contract.
            # chunk_id alone collides across tenants: upload external_ids are content-
            # addressed, so two accounts uploading the same bytes minted identical chunk_ids
            # and the second upsert overwrote the first account's ACL/owner while its own
            # partition gained nothing (prod, 260821: the F&N guidelines doc). Migrate an
            # established table's legacy single-column PK in place; chunk_id was unique, so
            # no (tenant_id, chunk_id) duplicates can exist and the rebuild cannot fail.
            pk = self._primary_key()
            if pk and pk != ["tenant_id", "chunk_id"]:
                cur.execute(
                    """SELECT conname FROM pg_constraint
                        WHERE conrelid = to_regclass(%s) AND contype = 'p'""",
                    (self._table,),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(f"ALTER TABLE {self._table} DROP CONSTRAINT {row[0]}")
                cur.execute(
                    f"ALTER TABLE {self._table} ADD PRIMARY KEY (tenant_id, chunk_id)")
            cur.execute(f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS locator jsonb")
            # ADR 0012: attribution only (who ingested) — nullable, never gates retrieval
            cur.execute(f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS owner_oid text")
            # #775 / ADR 0027 rule 3: the raw size of an uploaded document, denormalized onto
            # every one of its chunks so usage is a grouped SUM here rather than a separate
            # ledger that drifts. Additive and nullable, so an existing deployment upgrades
            # with no migration step; NULL reads as 0, which is right for every row written
            # before this column existed (a connector crawl holds no bytes of ours anyway).
            cur.execute(f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS doc_bytes bigint")
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {self._table}_emb_idx "
                f"ON {self._table} USING hnsw (embedding vector_cosine_ops)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {self._table}_tenant_idx "
                f"ON {self._table} (tenant_id)"  # ADR 0012: keep the partition predicate cheap
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {self._table}_acl_idx "
                f"ON {self._table} USING gin (allowed_principals)"  # fast overlap filter
            )

    def upsert(self, chunks: list[Chunk]) -> None:
        # Explicit transaction: a batch of chunks lands all-or-nothing. Under autocommit each
        # INSERT would otherwise commit on its own, so a failure midway would leave a document
        # half-indexed and silently partial - worse than the failure itself.
        with self._conn.transaction(), self._conn.cursor() as cur:
            for c in chunks:
                content = self._store.get(c.text_ref).decode()
                embedding = c.vector(self._store)
                cur.execute(
                    f"""INSERT INTO {self._table}
                        (chunk_id, tenant_id, doc_external_id, text_ref, title, uri, content,
                         allowed_principals, embedding, locator, owner_oid, doc_bytes)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s)
                        ON CONFLICT (tenant_id, chunk_id) DO UPDATE SET
                          content=EXCLUDED.content,
                          allowed_principals=EXCLUDED.allowed_principals,
                          embedding=EXCLUDED.embedding,
                          locator=EXCLUDED.locator,
                          owner_oid=EXCLUDED.owner_oid,
                          doc_bytes=EXCLUDED.doc_bytes""",
                    (c.chunk_id, c.tenant_id, c.doc_external_id, c.text_ref,
                     _pg_text(c.title), _pg_text(c.uri), _pg_text(content),
                     c.allowed_principals, _vec_literal(embedding), json.dumps(c.locator),
                     c.owner_oid, int(c.doc_bytes or 0)),
                )

    def search(self, query_embedding: list[float], principals: list[str], top_k: int,
               scope) -> list[dict]:
        qv = _vec_literal(query_embedding)
        where, wparams = _doorway_sql(scope)
        with self._conn.cursor() as cur:
            cur.execute(
                f"""SELECT doc_external_id, chunk_id, text_ref, title, uri, locator,
                           1 - (embedding <=> %s::vector) AS score, owner_oid
                    FROM {self._table}
                    WHERE {where}                               -- MANDATORY partition (ADR 0012/0019)
                      AND allowed_principals && %s::text[]      -- MANDATORY trim (LAW 2)
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s""",
                (qv, *wparams, principals, qv, top_k),
            )
            rows = cur.fetchall()
        return [
            {"doc_external_id": r[0], "chunk_id": r[1], "text_ref": r[2],
             "title": r[3], "uri": r[4], "locator": r[5] or {}, "score": r[6],
             "allowed_principals": principals,
             "owner_oid": r[7]}   # #576 Finding 2: retrieval-based activity touch
            for r in rows
        ]

    def delete(self, tenant_id: str, doc_external_id: str) -> None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {self._table} WHERE tenant_id=%s AND doc_external_id=%s",
                (tenant_id, doc_external_id),
            )

    def chunk_count_for(self, tenant_id: str, doc_external_id: str) -> int:
        """#93: real per-doc chunk count, so /admin/upload stops reporting the
        doc_count=1 fallback on the pgvector backend."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM {self._table} WHERE tenant_id=%s AND doc_external_id=%s",
                (tenant_id, doc_external_id),
            )
            return int(cur.fetchone()[0])

    def corpus_status(self, scope, principals: list[str]) -> CorpusStatus:
        """#392: both numbers in ONE scan, via a FILTER aggregate, rather than two queries
        or a list_doc_acls() walk that would grow with the corpus. This runs on page load,
        so it has to stay O(index) in the database and O(1) in transferred rows.

        This used to call commit() explicitly, to avoid adding to #440's idle-in-transaction
        pile while that bug was still open. #440 is now fixed at the source - the connection
        is autocommit - so the read holds nothing and the manual commit is gone with it."""
        where, wparams = _doorway_sql(scope)
        with self._conn.cursor() as cur:
            cur.execute(
                f"""SELECT count(DISTINCT doc_external_id),
                           count(DISTINCT doc_external_id)
                               FILTER (WHERE allowed_principals && %s::text[])
                    FROM {self._table}
                    WHERE {where}""",   # MANDATORY partition (ADR 0012/0019)
                (principals, *wparams),
            )
            total, mine = cur.fetchone()
        return CorpusStatus(indexed=bool(total), authorized_docs=int(mine or 0))

    def add_doc_principals(self, tenant_id: str, doc_external_id: str,
                           principals: list[str]) -> int:
        """#538/ADR 0017: add principals to every chunk of one document, idempotently.

        Postgres does the set union in one statement rather than read-modify-write, so two
        concurrent grants on the same document cannot lose one another - a read-modify-write
        here would drop whichever grant lost the race, and it would look exactly like a share
        that was never made."""
        extra = [p for p in principals if p]
        if not extra:
            return 0
        with self._conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {self._table}
                    SET allowed_principals = (
                        SELECT array_agg(DISTINCT p)
                        FROM unnest(allowed_principals || %s::text[]) AS p)
                    WHERE tenant_id=%s AND doc_external_id=%s""",
                (extra, tenant_id, doc_external_id),
            )
            return cur.rowcount or 0

    def docs_owned_by(self, tenant_id: str, owner_oid: str) -> list[str]:
        """#576: doc external ids this account ingested into this partition - what the
        retention sweep deletes when the account has gone silent. `owner_oid` is
        attribution only (ADR 0012), never an ACL check."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"""SELECT DISTINCT doc_external_id FROM {self._table}
                    WHERE tenant_id=%s AND owner_oid=%s""",
                (tenant_id, owner_oid),
            )
            return [r[0] for r in cur.fetchall()]

    def usage_bytes(self, tenant_id: str, owner_oid: str,
                    exclude_uri: "str | None" = None,
                    exclude_doc_id: "str | None" = None) -> int:
        """#775: how many raw bytes this owner has entrusted to this partition.

        The inner GROUP BY is load-bearing. `doc_bytes` is denormalized onto every chunk, so
        `SUM(doc_bytes)` over the raw rows would multiply each file by its chunk count and
        bill a 300-page upload as if it were hundreds of copies. Group per document, take the
        max, then add those up. NULL (rows written before the column existed) reads as 0.
        """
        # #844: exclude the rows this write is about to REPLACE, so the quota compares
        # against what the account will hold once it lands. IS DISTINCT FROM, not <>, so a
        # NULL uri does not swallow the whole predicate.
        where, params = "tenant_id=%s AND owner_oid=%s", [tenant_id, owner_oid]
        if exclude_uri is not None:
            where += " AND uri IS DISTINCT FROM %s"
            params.append(exclude_uri)
        if exclude_doc_id is not None:
            where += " AND doc_external_id IS DISTINCT FROM %s"
            params.append(exclude_doc_id)
        with self._conn.cursor() as cur:
            cur.execute(
                f"""SELECT COALESCE(SUM(b), 0) FROM (
                        SELECT doc_external_id, MAX(COALESCE(doc_bytes, 0)) AS b
                        FROM {self._table}
                        WHERE {where}
                        GROUP BY doc_external_id) t""",
                tuple(params),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def list_doc_acls(self, scope) -> list[DocACL]:
        """Admin Permission-Tester surface + the #90 supersede-by-uri lookup: one row
        per document, principals = the UNION across its chunks. Metadata only (LAW 1)."""
        where, wparams = _doorway_sql(scope)
        with self._conn.cursor() as cur:
            cur.execute(
                f"""SELECT doc_external_id, max(title), max(uri),
                           array_agg(DISTINCT p), max(owner_oid)
                    FROM {self._table}, unnest(allowed_principals) AS p
                    WHERE {where}
                    GROUP BY doc_external_id
                    ORDER BY doc_external_id""",
                tuple(wparams),
            )
            rows = cur.fetchall()
        return [DocACL(doc_external_id=r[0], title=r[1] or "", uri=r[2] or "",
                       allowed_principals=list(r[3] or []), owner_oid=r[4]) for r in rows]

    def list_doc_segments(self, scope, doc_external_id: str, principals: list[str]) -> list[dict]:
        where, wparams = _doorway_sql(scope)
        with self._conn.cursor() as cur:
            cur.execute(
                f"""SELECT chunk_id, locator, left(content, 200)
                    FROM {self._table}
                    WHERE {where} AND doc_external_id=%s
                          AND allowed_principals && %s::text[]      -- MANDATORY trim (LAW 2)
                    ORDER BY chunk_id
                    LIMIT 1000""",
                (*wparams, doc_external_id, principals),
            )
            rows = cur.fetchall()
        return [{"chunk_id": r[0], "locator": r[1] or {}, "preview": r[2] or ""} for r in rows]
