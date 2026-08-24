"""#833 - the doc_bytes backfill decides correctly BEFORE it ever touches prod.

Prod ground truth (measured 260818): every one of the 53 chunk rows has doc_bytes NULL, so
usage_bytes() sums to 0 for an account holding 211MB and /billing/status lies to the one
surface that now sells storage. The backfill recovers the number from the raw blob, because
for uploads the raw blob IS the uploaded bytes written verbatim (runner.py stores
`raw/{tenant}/{doc_external_id}` from the same object whose len() produced doc_bytes).

The script is a pure decision core plus an I/O shell, and THIS file tests the core with the
blob reader injected, so every branch is exercised without a database:

  - an upload doc (uri upload://...) with a raw blob     -> UPDATE to the blob's size
  - a connector doc (any other uri) with NULL doc_bytes  -> UPDATE to explicit 0, which is
    byte-for-byte what a re-ingest would write (models.py doc_bytes default 0), so NULL keeps
    meaning exactly "unknown"
  - an upload doc whose raw blob is GONE                 -> SKIP and say so; writing 0 would
    turn "unknown" into a metering claim, the empty-success lesson in one line
  - a doc that already has doc_bytes                     -> untouched, so the script is
    idempotent even if the SQL filter ever widens

And the rig proves it could show the bug: usage computed the way usage_bytes() computes it
(per-doc MAX(COALESCE(doc_bytes, 0)), then SUM) reads 0 before the decisions are applied and
the real total after.

    PYTHONPATH=src python3 tests/selftest_833_doc_bytes_backfill.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from prod_833_backfill_doc_bytes import decide  # noqa: E402

TENANT = "selfhost"


def _rows():
    """One row per document, the shape the script's SQL hands the core."""
    return [
        {"tenant_id": TENANT, "doc_external_id": "upload-report-abcd1234",
         "uri": "upload://Report.pdf", "doc_bytes": None},
        {"tenant_id": TENANT, "doc_external_id": "upload-notes-99ffee00",
         "uri": "upload://notes.txt", "doc_bytes": None},
        {"tenant_id": TENANT, "doc_external_id": "sp-hr-handbook",
         "uri": "https://contoso.sharepoint.com/hr/handbook.docx", "doc_bytes": None},
        {"tenant_id": TENANT, "doc_external_id": "upload-ghost-00000000",
         "uri": "upload://ghost.pdf", "doc_bytes": None},          # raw blob missing
        {"tenant_id": TENANT, "doc_external_id": "upload-fresh-11112222",
         "uri": "upload://fresh.csv", "doc_bytes": 777},           # already metered
    ]


BLOB_SIZES = {
    (TENANT, "upload-report-abcd1234"): 150_000_000,
    (TENANT, "upload-notes-99ffee00"): 61_000_000,
    # sp-hr-handbook HAS a raw blob too - connector ingests also write raw/ - and the
    # decision must never read it: connector bytes are not ours to meter (ADR 0027 rule 3).
    (TENANT, "sp-hr-handbook"): 999_999_999,
    # upload-ghost deliberately absent.
}


def _blob_size_of(tenant_id, doc_external_id):
    return BLOB_SIZES.get((tenant_id, doc_external_id))


def _usage_bytes_the_pgvector_way(chunks, tenant_id, owner_oid):
    """Mirror of pgvector.usage_bytes: per-doc MAX(COALESCE(doc_bytes,0)), then SUM."""
    per_doc = {}
    for c in chunks:
        if c["tenant_id"] != tenant_id or c["owner_oid"] != owner_oid:
            continue
        per_doc[c["doc_external_id"]] = max(
            per_doc.get(c["doc_external_id"], 0), int(c["doc_bytes"] or 0))
    return sum(per_doc.values())


def test_uploads_get_their_raw_blob_size():
    updates, _ = decide(_rows(), _blob_size_of)
    got = {d: v for (_, d, v) in updates}
    assert got.get("upload-report-abcd1234") == 150_000_000, got
    assert got.get("upload-notes-99ffee00") == 61_000_000, got


def test_connector_docs_get_explicit_zero_never_their_blob_size():
    """The blob reader KNOWS a size for the SharePoint doc; the decision must not ask."""
    updates, _ = decide(_rows(), _blob_size_of)
    got = {d: v for (_, d, v) in updates}
    assert got.get("sp-hr-handbook") == 0, got


def test_a_doc_with_no_uri_is_skipped_rather_than_guessed_as_a_connector():
    """#840: the classifier reads "not upload://" as "connector, write 0", and 0 is
    IRREVERSIBLE - the row leaves the NULL set and no later run reconsiders it. An absent uri
    is not evidence of a connector, it is the absence of evidence, so it gets the same
    treatment as a missing blob: left NULL and named."""
    rows = _rows() + [{"tenant_id": "t1", "doc_external_id": "legacy-no-uri",
                       "uri": "", "doc_bytes": None}]
    updates, skips = decide(rows, _blob_size_of)
    written = {d: v for (_, d, v) in updates}
    assert "legacy-no-uri" not in written, (
        f"a doc with no uri was written as {written.get('legacy-no-uri')!r} - guessing 0 for "
        f"an unknown row is permanent, because it leaves the NULL set")
    skipped = {d: reason for (_, d, reason) in skips}
    assert "legacy-no-uri" in skipped, skips
    assert "uri" in skipped["legacy-no-uri"], skipped["legacy-no-uri"]


def test_a_missing_raw_blob_is_skipped_and_named_never_written_as_zero():
    updates, skips = decide(_rows(), _blob_size_of)
    updated = {d for (_, d, _v) in updates}
    assert "upload-ghost-00000000" not in updated, updates
    skipped = {d: reason for (_, d, reason) in skips}
    assert "upload-ghost-00000000" in skipped, skips
    assert skipped["upload-ghost-00000000"], "a skip must carry its reason"


def test_an_already_metered_doc_is_untouched():
    updates, skips = decide(_rows(), _blob_size_of)
    touched = {d for (_, d, _v) in updates} | {d for (_, d, _r) in skips}
    assert "upload-fresh-11112222" not in touched, (updates, skips)


def test_the_rig_could_show_the_bug_usage_goes_zero_to_real():
    """Chunk-level mirror of prod: several chunks per doc, all NULL. Before the decisions
    are applied the meter reads 0 (the defect); after, the exact upload total - and ONLY
    the upload total, connector rows contribute nothing."""
    owner = "oid-owner"
    chunks = []
    for row in _rows():
        n_chunks = 3 if row["doc_external_id"] != "upload-fresh-11112222" else 2
        for _ in range(n_chunks):
            chunks.append({"tenant_id": row["tenant_id"], "owner_oid": owner,
                           "doc_external_id": row["doc_external_id"],
                           "doc_bytes": row["doc_bytes"]})

    before = _usage_bytes_the_pgvector_way(
        [c for c in chunks if c["doc_external_id"] != "upload-fresh-11112222"],
        TENANT, owner)
    assert before == 0, f"precondition failed: the rig cannot show the bug (before={before})"

    updates, _ = decide(_rows(), _blob_size_of)
    by_doc = {d: v for (_, d, v) in updates}
    for c in chunks:
        if c["doc_bytes"] is None and c["doc_external_id"] in by_doc:
            c["doc_bytes"] = by_doc[c["doc_external_id"]]

    after = _usage_bytes_the_pgvector_way(chunks, TENANT, owner)
    assert after == 150_000_000 + 61_000_000 + 777, after


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    sys.exit(1 if failures else 0)
