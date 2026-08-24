"""#845 - an ingestion that RAISED does not leave its raw bytes stranded on the volume.

OBSERVED ON PROD, not inferred: while proving #841 I uploaded a corrupt PDF, got the honest
422, and then found `raw/selfhost/upload-dbs841probe-d1d4175f` still sitting on the live disk.
That document was never indexed, so no index row ever referenced the blob - and BOTH
blob-deleting paths in the product (`DELETE /documents` and the retention sweep) enumerate
FROM the index. Nothing in the product could ever reach those bytes again. Rejected uploads
are precisely the ones users retry, so the leak compounds on the same volume the #831
headroom guard defends.

DISTINCT FROM #841. That card reclaimed the SUPERSEDED version's blobs on the success path.
This is the failure path, which #841 deliberately did not touch.

THE GUARD IS ORPHANHOOD, NOT THE EXCEPTION. `run_ingestion` deletes-before-indexing for the
id it is writing, so a failure can land with an existing id half-written; reclaiming purely
because an exception happened would then destroy a document that is still readable. So the
reclaim asks the index first, and `test_a_failure_never_takes_a_live_documents_blobs` is the
control that would catch the careless version.

    PYTHONPATH=src python3 tests/selftest_845_failed_upload_reclaims.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.ports.base import (ParseProducedNoText, UnsupportedMedia,  # noqa: E402
                                 as_read_scope)
from dbsearch.server.edition import build_edition  # noqa: E402

ALICE = "oid-alice"
UNPARSEABLE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GOOD = b"the quarterly figures for the APAC region"


def _blobs(ed, doc_id):
    return sorted(k for k in ed.store._blobs if doc_id in k)


def _docs(ed):
    return {d.doc_external_id for d in ed.index.list_doc_acls(as_read_scope(ed.tenant_id))}


def _upload(ed, doc_id, data, mime, name="f.txt"):
    return ed.ingest_file(external_id=doc_id, title=name, data=data, mime=mime,
                          acl=[ALICE], uri=f"upload://{name}", owner_oid=ALICE)


def test_a_failed_upload_leaves_no_orphan_blobs():
    """The defect verbatim: 415/422 to the user, raw bytes stranded forever."""
    ed = build_edition()
    raised = None
    try:
        _upload(ed, "u-845-fail", UNPARSEABLE, "image/png", "bad.png")
    except (UnsupportedMedia, ParseProducedNoText) as exc:
        raised = type(exc).__name__
    assert raised, ("fixture cannot reach the defect: the unparseable upload did not raise, "
                    "so no failure path was exercised")
    assert "u-845-fail" not in _docs(ed), "fixture is broken: the failure indexed anyway"
    left = _blobs(ed, "u-845-fail")
    assert left == [], (
        f"#845: a failed upload stranded {left} on the volume. No index row references them, "
        f"and DELETE /documents and the retention sweep both enumerate FROM the index, so "
        f"nothing in the product can ever reclaim them.")


def test_a_successful_upload_keeps_its_blobs():
    """CONTROL. A reclaim that fires on the success path would delete the document's own
    content - answers are read from chunk/ at query time."""
    ed = build_edition()
    _upload(ed, "u-845-ok", GOOD, "text/plain", "ok.txt")
    kept = _blobs(ed, "u-845-ok")
    assert kept, "a SUCCESSFUL upload lost its blobs - the reclaim fired on the wrong path"
    assert any(k.startswith("raw/") for k in kept), kept
    assert any(k.startswith("chunk/") for k in kept), (
        f"the chunk text blobs answers are read from are gone: {kept}")


def test_a_failure_never_takes_a_live_documents_blobs():
    """THE CONTROL THAT MATTERS, and the reason the guard is orphanhood rather than 'an
    exception happened'. A document is live; a later ingestion under the SAME id fails. Its
    blobs must survive, because the index still points at them."""
    ed = build_edition()
    _upload(ed, "u-845-live", GOOD, "text/plain", "live.txt")
    before = _blobs(ed, "u-845-live")
    assert before, "fixture is broken: the live document wrote no blobs"

    # force a failure for an id the index still holds
    class _Boom(Exception):
        pass

    real = ed._reclaim_blobs
    calls = []
    ed._reclaim_blobs = lambda p, d: calls.append(d)   # observe, do not delete
    try:
        ed._reclaim_orphan_blobs(ed.tenant_id, "u-845-live")
    finally:
        ed._reclaim_blobs = real

    assert calls == [], (
        f"#845: the reclaim was willing to delete blobs of a document the index still holds "
        f"({calls}) - a failed re-ingest of an existing id would destroy a live document")
    assert _blobs(ed, "u-845-live") == before, "the live document's blobs changed"


def test_the_ingest_document_path_reclaims_too():
    """SECOND HOME (#799). /ingest writes the raw blob the same way, so a raise strands it
    the same way - less often, because text almost always parses, but identically."""
    ed = build_edition()

    boom = RuntimeError("embedding backend unavailable")
    real_embed = ed.embedder.embed
    ed.embedder.embed = lambda *a, **k: (_ for _ in ()).throw(boom)
    try:
        ed.ingest_document("u-845-ing", "Notes", "some entrusted text", [ALICE],
                           uri="note://845", owner_oid=ALICE)
    except Exception:
        pass
    finally:
        ed.embedder.embed = real_embed

    assert "u-845-ing" not in _docs(ed), "fixture is broken: the failure indexed anyway"
    left = _blobs(ed, "u-845-ing")
    assert left == [], f"#845: the /ingest path stranded {left}"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}\n        {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
