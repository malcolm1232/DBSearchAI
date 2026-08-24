"""#916 - a chunk's identity must include its tenant, in EVERY persistent index adapter.

THE DEFECT, measured on prod 260821: the owner uploaded "F&N Group Policy Guidelines
...pdf" (home partition). A brand-new local account (mex3woof) uploaded the SAME file.
Upload external_ids are content-addressed (upload-{slug}-{sha8}) and runner.py builds
chunk_id = "{external_id}#{n}", so both accounts produced IDENTICAL chunk_ids. The
pgvector table's PK was chunk_id ALONE, and upsert's ON CONFLICT (chunk_id) DO UPDATE
overwrote allowed_principals + owner_oid but NOT tenant_id. Net effect, verified row by
row on prod: the stranger's partition gained nothing (their /admin listed zero docs, the
reported symptom), and the owner's 20-chunk document was re-ACL'd to an acct: identity
that can never reach the home partition - a LAW 2 cross-account ACL/owner overwrite that
orphaned the document for everyone.

THE RULE (matches the InMemoryIndex contract, adapters/local: `(tenant_id, chunk_id)` is
the identity): the same chunk_id under two tenants is TWO chunks. pgvector enforces it
with a composite primary key (migrated in ensure_schema, detected lock-free by
schema_is_current); Azure AI Search derives its document key from tenant + chunk id.

One clause per test, so each regressing alone goes red:
  - cross-tenant same-chunk_id ingest keeps BOTH partitions intact (the exact prod
    defect: pre-fix, tenant B's upsert stole tenant A's rows);
  - same-tenant re-upsert still REPLACES in place (kills the drop-the-conflict-clause
    wrong fix, which would duplicate instead);
  - an ESTABLISHED single-column-PK table is migrated on ensure_schema, and
    schema_is_current says False for it first (kills the wrong fix of migrating only
    freshly created tables);
  - the AI Search document key differs across tenants for the same chunk_id (unit-level;
    the defect there is the same shape - key=chunk_id alone - and upload_documents
    replaces the whole doc, tenant field included).

Needs docker (pgvector/pgvector:pg16) + psycopg; SKIPS loudly when unavailable.
Run: python3 tests/selftest_916_chunk_identity_is_tenant_scoped.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.core.models import Chunk  # noqa: E402
from dbsearch.ports.base import ReadScope  # noqa: E402

CONTAINER = "dbsearch-selftest-916-pg"
PORT = 55434
DSN = f"postgresql://postgres:test@127.0.0.1:{PORT}/postgres"

TENANT_A = "selfhost"                 # the home partition, as on prod
TENANT_B = "acct:acct_stranger916"    # the stranger's private partition
OWNER_A, OWNER_B = "owner-a-oid", "acct_stranger916"
DOC = "upload-fnn-guidelines-f384c175"   # content-addressed: SAME id for both uploads


def _skip(reason: str) -> None:
    print(f"SKIP selftest_916_chunk_identity_is_tenant_scoped — {reason}")
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


class _Store:
    """Minimal ObjectStorePort double: text + embedding blobs by ref."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, ref: str, data: bytes) -> None:
        self._blobs[ref] = data

    def get(self, ref: str) -> bytes:
        return self._blobs[ref]


def _chunks(store: _Store, tenant: str, owner: str, n: int = 2) -> list[Chunk]:
    """The prod shape: same DOC external id and chunk ids regardless of tenant."""
    import json as _json
    out = []
    for i in range(n):
        text_ref = f"{tenant}/{DOC}/{i}.txt"
        emb_ref = f"{tenant}/{DOC}/{i}.emb"
        store.put(text_ref, f"guideline clause {i}".encode())
        store.put(emb_ref, _json.dumps([float(i)] * 4).encode())
        out.append(Chunk(
            tenant_id=tenant, doc_external_id=DOC, chunk_id=f"{DOC}#{i}",
            text_ref=text_ref, embedding_ref=emb_ref, title="F&N Guidelines",
            uri="upload://fnn.pdf", allowed_principals=[owner], locator={"page": i},
            owner_oid=owner, doc_bytes=1000,
        ))
    return out


def _acls(index, tenant: str, principals: list[str]):
    return {d.doc_external_id: sorted(d.allowed_principals)
            for d in index.list_doc_acls(ReadScope(partition=tenant))}


fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        fails.append(name)
    return cond


def main() -> int:
    if not _docker_available():
        _skip("docker unavailable")
    try:
        import psycopg  # noqa: F401
    except Exception:
        _skip("psycopg not installed")

    from dbsearch.adapters.vectordb.pgvector import PgVectorIndex

    _start_pg()
    try:
        _wait_ready()
        store = _Store()

        # ---- clause 3 first (needs a virgin table): an ESTABLISHED deployment with the
        # OLD single-column PK is detected and migrated. Build the OLD shape by hand -
        # from `git show 6633f04:src/.../pgvector.py`'s DDL - then boot the adapter.
        import psycopg
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                """CREATE TABLE chunks (
                       chunk_id text PRIMARY KEY,
                       tenant_id text NOT NULL,
                       doc_external_id text NOT NULL,
                       text_ref text, title text, uri text, content text,
                       locator jsonb,
                       allowed_principals text[] NOT NULL,
                       embedding vector(4),
                       owner_oid text, doc_bytes bigint
                   )""")
        legacy = PgVectorIndex(DSN, store, dim=4)
        check("schema_is_current flags the legacy single-column PK as out of shape",
              legacy.schema_is_current() is False,
              "an established table would skip ensure_schema and never migrate")
        legacy.ensure_schema()
        with psycopg.connect(DSN) as conn:
            pk = [r[0] for r in conn.execute(
                """SELECT a.attname FROM pg_index i
                       JOIN pg_attribute a ON a.attrelid = i.indrelid
                        AND a.attnum = ANY(i.indkey)
                    WHERE i.indrelid = 'chunks'::regclass AND i.indisprimary
                    ORDER BY a.attnum""").fetchall()]
        check("ensure_schema migrates the PK to (tenant_id, chunk_id)",
              sorted(pk) == ["chunk_id", "tenant_id"], f"pk={pk}")

        index = legacy   # continue on the migrated table - closest to prod's path

        # ---- clause 1: the exact prod defect. Tenant A ingests, then tenant B ingests
        # the SAME content-addressed chunk ids. Both partitions must hold their own copy
        # with their own ACL.
        index.upsert(_chunks(store, TENANT_A, OWNER_A))
        index.upsert(_chunks(store, TENANT_B, OWNER_B))
        a = _acls(index, TENANT_A, [OWNER_A])
        b = _acls(index, TENANT_B, [OWNER_B])
        check("tenant A keeps its document and its OWN ACL after B's colliding ingest",
              a.get(DOC) == [OWNER_A], f"A sees {a}")
        check("tenant B's partition holds its own copy",
              b.get(DOC) == [OWNER_B], f"B sees {b}")
        check("A's copy still counts its chunks", index.chunk_count_for(TENANT_A, DOC) == 2,
              f"count={index.chunk_count_for(TENANT_A, DOC)}")
        check("B's copy counts its chunks too", index.chunk_count_for(TENANT_B, DOC) == 2,
              f"count={index.chunk_count_for(TENANT_B, DOC)}")

        # ---- clause 2: same-tenant re-upsert REPLACES in place (LAW 3 idempotency) -
        # fails the wrong fix that drops the conflict clause and duplicates instead.
        index.upsert(_chunks(store, TENANT_A, OWNER_A))
        check("same-tenant re-upsert does not duplicate",
              index.chunk_count_for(TENANT_A, DOC) == 2,
              f"count={index.chunk_count_for(TENANT_A, DOC)}")

        # ---- clause 4: AI Search key derivation is tenant-scoped (unit-level: the SDK
        # is only imported inside methods, so the pure key function is importable).
        from dbsearch.adapters.azure import aisearch
        keyfn = getattr(aisearch, "_doc_key", None)
        if check("aisearch exposes a tenant-scoped _doc_key", callable(keyfn),
                 "upsert must derive the AI Search document key from (tenant, chunk_id)"):
            k_a, k_b = keyfn(TENANT_A, f"{DOC}#0"), keyfn(TENANT_B, f"{DOC}#0")
            check("aisearch keys differ across tenants for the same chunk_id",
                  k_a != k_b, f"{k_a} == {k_b}")
            import re
            check("aisearch keys stay in the allowed charset",
                  bool(re.fullmatch(r"[A-Za-z0-9_\-=]+", k_a + k_b)), f"{k_a} {k_b}")
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)

    print()
    if fails:
        print(f"selftest_916: FAILED ({len(fails)}): {fails}")
        return 1
    print("selftest_916: chunk identity is tenant-scoped in every persistent adapter ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
