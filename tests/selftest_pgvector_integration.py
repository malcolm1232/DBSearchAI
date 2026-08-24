"""PgVectorIndex integration test against a REAL dockerized Postgres+pgvector.

Covers the #93/#90 adapter surface for the pgvector backend: chunk_count_for,
list_doc_acls (union of chunk principals, uri for supersede-by-uri), the mandatory
search trim, and delete. SKIPS (exit 0, loud message) when docker or psycopg is
unavailable, so the suite stays runnable anywhere.

Run: python3 tests/selftest_pgvector_integration.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.ports.base import ReadScope  # noqa: E402

CONTAINER = "dbsearch-selftest-pg"
PORT = 55433
DSN = f"postgresql://postgres:test@127.0.0.1:{PORT}/postgres"


def _skip(reason: str) -> None:
    print(f"SKIP selftest_pgvector_integration — {reason}")
    sys.exit(0)


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "ps"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def _start_pg() -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    r = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", CONTAINER,
         "-e", "POSTGRES_PASSWORD=test", "-p", f"{PORT}:5432",
         "pgvector/pgvector:pg16"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        _skip(f"could not start pgvector container: {r.stderr.strip()[:200]}")


def _wait_ready(timeout_s: float = 60.0) -> None:
    import psycopg
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            psycopg.connect(DSN, connect_timeout=2).close()
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("postgres did not become ready")


def _chunk(store, tenant, doc, n, text, acl, title, uri):
    from dbsearch.core.models import Chunk
    text_ref = store.put(f"chunk/{tenant}/{doc}/{n}", text.encode())
    emb = [1.0 if i < 3 else 0.0 for i in range(8)] if "alpha" in text else \
          [0.0 if i < 5 else 1.0 for i in range(8)]
    emb_ref = store.put(f"emb/{tenant}/{doc}/{n}", json.dumps(emb).encode())
    return Chunk(tenant_id=tenant, doc_external_id=doc, chunk_id=f"{doc}#{n}",
                 text_ref=text_ref, allowed_principals=acl, embedding_ref=emb_ref,
                 title=title, uri=uri, locator={"page": n})


def main():
    try:
        import psycopg  # noqa: F401
    except ImportError:
        _skip("psycopg not installed (pip install 'psycopg[binary]')")
    if not _docker_available():
        _skip("docker unavailable")

    _start_pg()
    try:
        _wait_ready()
        from dbsearch.adapters.local import InMemoryObjectStore
        from dbsearch.adapters.vectordb import PgVectorIndex

        store = InMemoryObjectStore()
        idx = PgVectorIndex(DSN, store, dim=8)
        idx.ensure_schema()

        t = "acme"
        idx.upsert([
            _chunk(store, t, "doc-a", 0, "alpha first", ["all-staff"], "Doc A", "upload://a.txt"),
            _chunk(store, t, "doc-a", 1, "alpha second", ["deal-team"], "Doc A", "upload://a.txt"),
            _chunk(store, t, "doc-b", 0, "omega only", ["deal-team"], "Doc B", "upload://b.txt"),
            # ADR 0012: a FOREIGN tenant's doc with a deliberately WIDE acl - the pre-#389
            # leak shape. It must never surface in an `acme` search, however wide its ACL.
            _chunk(store, "beta", "doc-z", 0, "omega leaked", ["all-staff", "deal-team"],
                   "Doc Z", "upload://z.txt"),
        ])

        # #93: real chunk count, not the doc_count fallback
        assert idx.chunk_count_for(t, "doc-a") == 2, idx.chunk_count_for(t, "doc-a")
        assert idx.chunk_count_for(t, "doc-b") == 1

        # #90 surface: one row per doc, principals = UNION across chunks, uri present
        acls = {d.doc_external_id: d for d in idx.list_doc_acls(ReadScope(t))}
        assert set(acls) == {"doc-a", "doc-b"}, acls
        assert sorted(acls["doc-a"].allowed_principals) == ["all-staff", "deal-team"], acls
        assert acls["doc-a"].uri == "upload://a.txt", acls

        # LAW 2: the search trim is a mandatory WHERE — bob (all-staff) never sees
        # deal-team chunks even with a perfectly matching vector
        qv = [0.0 if i < 5 else 1.0 for i in range(8)]           # matches "omega only"
        hits = idx.search(qv, ["all-staff"], top_k=10, scope=ReadScope(t))
        assert all(h["doc_external_id"] != "doc-b" for h in hits), hits

        # ADR 0012: the tenant trim is a SECOND mandatory WHERE. beta's doc-z carries an
        # acl that matches BOTH principal sets — only the tenant predicate keeps it out.
        hits = idx.search(qv, ["all-staff", "deal-team"], top_k=10, scope=ReadScope(t))
        assert hits and all(h["doc_external_id"] != "doc-z" for h in hits), \
            f"cross-tenant wide-ACL leak: {hits}"
        hits_beta = idx.search(qv, ["deal-team"], top_k=10, scope=ReadScope("beta"))
        assert [h["doc_external_id"] for h in hits_beta] == ["doc-z"], hits_beta
        # and tenant_id cannot be omitted — required, not optional-with-default
        try:
            idx.search(qv, ["all-staff"], top_k=10)
            raise AssertionError("search() accepted a call WITHOUT tenant_id")
        except TypeError:
            pass

        # #392: corpus_status - both numbers in one scan, and the ACL predicate is the same
        # mandatory trim as search(). doc-a is all-staff+deal-team, doc-b is deal-team only.
        cs = idx.corpus_status(ReadScope(t), ["all-staff"])
        assert cs.indexed is True and cs.authorized_docs == 1, cs
        cs = idx.corpus_status(ReadScope(t), ["deal-team"])
        assert cs.indexed is True and cs.authorized_docs == 2, cs
        # Documents exist but admit nobody with this principal: indexed stays True while the
        # authorized count is 0. Ask relies on exactly that split to tell "nothing indexed"
        # apart from "nothing you may see", which is the whole point of #392.
        cs = idx.corpus_status(ReadScope(t), ["nobody"])
        assert cs.indexed is True and cs.authorized_docs == 0, cs
        # ADR 0012: the tenant predicate applies here too - a tenant with nothing is empty
        # however wide the caller's principals are.
        cs = idx.corpus_status(ReadScope("tenant-with-nothing"), ["all-staff", "deal-team"])
        assert cs.indexed is False and cs.authorized_docs == 0, cs

        # ---- #440: reads must not leave the connection IDLE IN TRANSACTION ----
        # This is the bug that took prod's database down. psycopg opens a transaction on the
        # first statement including a plain SELECT, and every read here used to return without
        # committing. A second process then queued for ACCESS EXCLUSIVE behind that idle
        # transaction, and once such a request is queued Postgres makes every LATER query wait
        # behind it - so all query traffic stalled, not just the deploy.
        import psycopg
        from psycopg.pq import TransactionStatus

        for label, call in (
            ("search", lambda: idx.search(qv, ["all-staff"], top_k=5, scope=ReadScope(t))),
            ("chunk_count_for", lambda: idx.chunk_count_for(t, "doc-b")),
            ("list_doc_acls", lambda: idx.list_doc_acls(ReadScope(t))),
            ("corpus_status", lambda: idx.corpus_status(ReadScope(t), ["all-staff"])),
        ):
            call()
            assert idx._conn.pgconn.transaction_status == TransactionStatus.IDLE, (
                f"#440 REGRESSION: {label}() left the connection in "
                f"{idx._conn.pgconn.transaction_status!r} - a DDL lock will queue behind it "
                "and stall every later query on this table")

        # The outage itself, reproduced: with a reader connection sitting idle after a search,
        # a SECOND connection must still be able to take ACCESS EXCLUSIVE. lock_timeout turns
        # the old hang into a fast, legible failure instead of wedging the test run.
        idx.search(qv, ["all-staff"], top_k=5, scope=ReadScope(t))      # leave the reader "used"
        other = psycopg.connect(DSN, autocommit=True)
        try:
            with other.cursor() as cur:
                cur.execute("SET lock_timeout = '5s'")
                cur.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS _t440_probe text")
                cur.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS _t440_probe")
        except psycopg.errors.LockNotAvailable as exc:
            raise AssertionError(
                "#440 REGRESSION: ALTER TABLE could not get its lock while a reader sat idle "
                f"in transaction - this is exactly the prod outage: {exc}") from None
        finally:
            other.close()

        # ensure_schema() is idempotent AND takes no lock in steady state: on an already-correct
        # schema it must short-circuit on the catalog check rather than issue ADD COLUMN, whose
        # ACCESS EXCLUSIVE is acquired even when the column already exists.
        assert idx.schema_is_current() is True, "schema_is_current() false right after ensure_schema()"
        idx.ensure_schema()                       # must be a no-op, and must not raise
        assert idx.schema_is_current() is True

        idx.delete(t, "doc-a")
        assert idx.chunk_count_for(t, "doc-a") == 0
        assert set(d.doc_external_id for d in idx.list_doc_acls(ReadScope(t))) == {"doc-b"}

        print("PASS selftest_pgvector_integration (real Postgres+pgvector in docker)")
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


if __name__ == "__main__":
    main()
