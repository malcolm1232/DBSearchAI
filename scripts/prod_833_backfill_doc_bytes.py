"""#833 - backfill chunks.doc_bytes from the raw blobs, once, on prod.

The #775 metering added doc_bytes as a nullable column, which was right for boot safety and
wrong for history: every document ingested before the column existed reads as 0 bytes to
usage_bytes(), so /billing/status told the owner 0 while the store held 211MB - measured
live, 53/53 chunk rows NULL. New ingests set the column; nothing ever repairs the old rows
except a re-upload. This script is that repair.

Where the number comes from: for uploads the raw blob IS the uploaded bytes, written
verbatim (`raw/{tenant_id}/{doc_external_id}`, runner.py) from the same object whose len()
became doc_bytes (connectors/upload.py). So getsize(raw blob) == the value ingest would have
written. Connector docs are set to explicit 0 - what a re-ingest writes (models.py default) -
and their raw blob is deliberately never consulted: connector bytes are not ours to meter
(ADR 0027 rule 3). An upload whose raw blob is gone is SKIPPED and named, never written as 0,
because 0 is a metering claim and "unknown" must stay NULL.

Two safety properties:
  - PRECONDITION (the #791 rig lesson): refuses to run when no NULL doc_bytes rows exist,
    because a run that could never show the defect proves nothing about fixing it.
  - Idempotent: every UPDATE carries `AND doc_bytes IS NULL`; the dry-run is the default and
    --apply is the only writing path.

Run INSIDE the api container (it sees /data/objects and the DB):

    python3 prod_833_backfill_doc_bytes.py                 # dry-run, prints the plan
    python3 prod_833_backfill_doc_bytes.py --apply         # one transaction, then re-checks
"""
from __future__ import annotations

import argparse
import os
import sys

UPLOAD_PREFIX = "upload://"


def decide(rows, blob_size_of):
    """The pure core. `rows` is one dict per document: tenant_id, doc_external_id, uri,
    doc_bytes. `blob_size_of(tenant_id, doc_external_id)` returns the raw blob's size in
    bytes, or None when the blob is missing.

    Returns (updates, skips): updates = [(tenant_id, doc_external_id, value)],
    skips = [(tenant_id, doc_external_id, reason)]. Rows whose doc_bytes is already set are
    untouched by construction, so the caller's SQL filter is a convenience, not a guard."""
    updates, skips = [], []
    for row in rows:
        if row["doc_bytes"] is not None:
            continue
        tenant, doc = row["tenant_id"], row["doc_external_id"]
        uri = row.get("uri") or ""
        if uri.startswith(UPLOAD_PREFIX):
            size = blob_size_of(tenant, doc)
            if size is None:
                skips.append((tenant, doc, "raw blob missing - leaving NULL, never 0"))
            else:
                updates.append((tenant, doc, int(size)))
        elif not uri:
            # #840: an ABSENT uri is not evidence of a connector - it is the absence of
            # evidence. This classifier reads "not upload://" as "connector, write 0", and 0
            # is irreversible: the row leaves the NULL set and no later run reconsiders it.
            # Every ingest path in this tree sets a uri, but the repo is a one-commit squash
            # of an older archive, so a row written by pre-snapshot code cannot be ruled out.
            # Same discipline as the missing-blob branch above: when we do not know, leave it
            # NULL and SAY so, rather than writing a number that looks like knowledge.
            skips.append((tenant, doc, "no uri - cannot tell upload from connector, "
                                       "leaving NULL rather than guessing 0"))
        else:
            # Connector doc: explicit 0, the value a re-ingest would write. Its raw blob
            # exists but is deliberately not consulted (ADR 0027 rule 3).
            updates.append((tenant, doc, 0))
    return updates, skips


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n}B"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the updates (default is a dry-run that only prints the plan)")
    ap.add_argument("--dsn", default=os.environ.get("PGVECTOR_DSN", ""),
                    help="Postgres DSN (default: PGVECTOR_DSN from the environment)")
    ap.add_argument("--objects", default=os.environ.get("OBJECT_STORE_DIR", "/data/objects"),
                    help="object store root (default: OBJECT_STORE_DIR or /data/objects)")
    args = ap.parse_args()

    if not args.dsn:
        print("FATAL: no DSN. Set PGVECTOR_DSN or pass --dsn.")
        return 2

    import psycopg  # the api image's driver (pgvector adapter uses psycopg 3)

    conn = psycopg.connect(args.dsn)  # autocommit False: the apply is one transaction
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks WHERE doc_bytes IS NULL")
        null_chunks = cur.fetchone()[0]
        if null_chunks == 0:
            print("PRECONDITION FAILED: no chunk has doc_bytes NULL - there is nothing this "
                  "run could show or fix. Refusing to continue (a rig that cannot show the "
                  "bug proves nothing).")
            return 3
        print(f"precondition: {null_chunks} chunk rows carry doc_bytes NULL")

        cur.execute(
            """SELECT DISTINCT tenant_id, doc_external_id, uri, doc_bytes
               FROM chunks WHERE doc_bytes IS NULL
               ORDER BY tenant_id, doc_external_id""")
        rows = [{"tenant_id": t, "doc_external_id": d, "uri": u, "doc_bytes": b}
                for (t, d, u, b) in cur.fetchall()]
    print(f"{len(rows)} documents to consider")

    def blob_size_of(tenant_id, doc_external_id):
        path = os.path.join(args.objects, "raw", tenant_id, doc_external_id)
        try:
            return os.path.getsize(path)
        except OSError:
            return None

    updates, skips = decide(rows, blob_size_of)

    total = sum(v for (_, _, v) in updates)
    print("\nPLAN:")
    for tenant, doc, value in updates:
        print(f"  SET {_human(value):>10}  {tenant}/{doc}")
    for tenant, doc, reason in skips:
        print(f"  SKIP           {tenant}/{doc}  ({reason})")
    print(f"\n{len(updates)} updates totalling {_human(total)}, {len(skips)} skipped")

    if not args.apply:
        print("\nDRY-RUN ONLY. Re-run with --apply to write.")
        conn.close()
        return 0

    with conn.cursor() as cur:
        for tenant, doc, value in updates:
            cur.execute(
                "UPDATE chunks SET doc_bytes=%s "
                "WHERE tenant_id=%s AND doc_external_id=%s AND doc_bytes IS NULL",
                (value, tenant, doc))
        conn.commit()

        # Post-assertion: no upload doc may still be NULL except the named skips.
        #
        # #840: counted per (tenant_id, doc_external_id), NOT by doc id alone. Upload ids are
        # CONTENT-addressed (`upload-{slug}-{sha8}`), so the same file uploaded by two
        # accounts shares one external_id while `skips` is per (tenant, doc). Counting bare
        # ids made the two sides of this comparison different things: two tenants each
        # holding that doc with a missing blob is 2 skips against a count of 1, which is a
        # spurious POST-ASSERTION FAILED *after* a correct commit - and symmetrically it
        # could mask a real leftover NULL that happened to share an id with a skip. The data
        # was never at risk; only the verdict was, which is worse in a way, because a
        # verdict is the only thing anyone reads.
        cur.execute(
            "SELECT count(*) FROM ("
            "  SELECT DISTINCT tenant_id, doc_external_id FROM chunks "
            "  WHERE doc_bytes IS NULL AND uri LIKE %s) t", (UPLOAD_PREFIX + "%",))
        upload_nulls = cur.fetchone()[0]
    conn.close()

    expected_nulls = len(skips)
    if upload_nulls != expected_nulls:
        print(f"POST-ASSERTION FAILED: {upload_nulls} upload docs still NULL, "
              f"expected {expected_nulls} (the skips). Investigate before trusting the meter.")
        return 4
    print(f"\nAPPLIED. Upload docs still NULL: {upload_nulls} (all named skips). "
          "Now confirm /billing/status shows the real number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
