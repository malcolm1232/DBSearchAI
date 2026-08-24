"""#841 - a re-upload supersedes the previous version only once it has SUCCEEDED.

FOUND by a cold adversarial review of the 260818 money path (#837), then reproduced through
the real `Edition.ingest_file` path before anything was changed.

THE DEFECT. `ingest_file` deleted every prior document sharing this upload's `uri` BEFORE
running the pipeline, and the pipeline runs `strict=True` so an unparseable file raises
(415/422 at the endpoint). A user with `Report.txt` indexed who uploaded an edited copy that
happened to be a scanned image got an error AND lost the version they already had. One
ordinary action, no attacker, permanent loss.

THE FIX, and why the order is safe. Ingest first, supersede after. That leaves a window,
inside this single request, where both versions are indexed - but the old version was already
exposing exactly that content for as long as it had existed, so the window adds no exposure,
and it is bounded by a request rather than being permanent. LAW 2 freshness (#90: stale
content under a possibly LOOSER acl must not linger) is unchanged for every successful
upload, which is what `test_a_successful_reupload_still_supersedes` pins.

THE SECOND HALF. Superseding removed only the INDEX rows. Both blob-deleting paths we have -
`DELETE /documents` and the retention sweep - enumerate FROM the index, so the old version's
`raw`/`segments`/`chunk` blobs became unreachable the moment its rows went: every edited
re-upload leaked its predecessor's bytes onto the same volume the #831 headroom guard
defends. `_reclaim_blobs` erases them through `retention.blob_prefixes`, the one list both
other paths already use.

TWO CLAUSES, TWO MUTATIONS (the #793 lesson). Reverting the ORDER alone must go red on
`test_a_failed_reupload_keeps_the_previous_version`; removing `_reclaim_blobs` alone must go
red on `test_superseding_reclaims_the_old_versions_blobs`. Neither test can be rescued by the
other half of the fix.

    PYTHONPATH=src python3 tests/selftest_841_supersede_after_success.py
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
URI = "upload://Report.txt"
V1 = "upload-report-aaaa1111"
V2 = "upload-report-bbbb2222"

GOOD_V1 = b"quarterly figures: revenue was 4.2 million in the APAC region"
GOOD_V2 = b"quarterly figures, revised: revenue was 4.9 million in the APAC region"
# a PNG header — a real "I edited my report and saved it as a scan" upload
UNPARSEABLE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _docs(ed):
    return {d.doc_external_id
            for d in ed.index.list_doc_acls(as_read_scope(ed.tenant_id))}


def _upload(ed, doc_id, data, mime="text/plain"):
    # ALICE is in the acl on purpose: the readability test below queries AS alice, and an
    # acl she is not in would return [] for a LAW 2 reason and look exactly like the data
    # loss this file is about. The first draft of this fixture made that mistake.
    return ed.ingest_file(external_id=doc_id, title="Report.txt", data=data, mime=mime,
                          acl=[ALICE, "all-staff"], uri=URI, owner_oid=ALICE)


def test_a_failed_reupload_keeps_the_previous_version():
    """CLAUSE 1. The defect verbatim: 415/422 must not cost the user the document they had."""
    ed = build_edition()
    _upload(ed, V1, GOOD_V1)
    assert V1 in _docs(ed), "fixture is broken: the first upload did not index"

    raised = None
    try:
        _upload(ed, V2, UNPARSEABLE, mime="image/png")
    except (UnsupportedMedia, ParseProducedNoText) as exc:
        raised = type(exc).__name__
    assert raised, ("fixture cannot reach the defect: the unparseable upload did NOT raise, "
                    "so nothing here exercises the failed-ingest path")

    after = _docs(ed)
    assert V1 in after, (
        f"#841: the previous good version was destroyed by a FAILED re-upload "
        f"(raised {raised}, docs now {sorted(after)}). The user got an error and lost "
        f"the document they already had.")
    assert V2 not in after, f"the failed version indexed anyway: {sorted(after)}"


def test_the_surviving_version_is_still_readable_after_a_failed_reupload():
    """Surviving the delete is not enough - it has to still ANSWER. A version whose rows
    remain but whose blobs were reclaimed would pass the test above and be useless here."""
    ed = build_edition()
    _upload(ed, V1, GOOD_V1)
    try:
        _upload(ed, V2, UNPARSEABLE, mime="image/png")
    except (UnsupportedMedia, ParseProducedNoText):
        pass
    answer = ed.query_service.answer(ALICE, "what was APAC revenue")
    assert V1 in answer.retrieved_docs, (
        f"the surviving version no longer retrieves: {answer.retrieved_docs}")


def test_a_successful_reupload_still_supersedes():
    """CONTROL for clause 1 - LAW 2 freshness (#90) must be exactly as strict as before.
    A fix that simply stopped superseding would pass the two tests above and break this."""
    ed = build_edition()
    _upload(ed, V1, GOOD_V1)
    _upload(ed, V2, GOOD_V2)
    after = _docs(ed)
    assert V2 in after, f"the new version did not index: {sorted(after)}"
    assert V1 not in after, (
        f"#90 REGRESSION: the old version survived a SUCCESSFUL re-upload {sorted(after)} - "
        f"stale content under a possibly looser acl is lingering invisibly.")


def test_superseding_reclaims_the_old_versions_blobs():
    """CLAUSE 2. Independent of clause 1: this drives the SUCCESS path, where the ordering
    fix makes no difference, so only `_reclaim_blobs` can turn it green."""
    ed = build_edition()
    _upload(ed, V1, GOOD_V1)
    v1_blobs = [k for k in ed.store._blobs if V1 in k]
    assert v1_blobs, "fixture is broken: the first upload wrote no blobs to reclaim"

    _upload(ed, V2, GOOD_V2)          # succeeds -> V1 is superseded

    left = [k for k in ed.store._blobs if V1 in k]
    assert left == [], (
        f"#841: the superseded version's blobs were orphaned - {left}. Nothing can ever "
        f"reclaim them: DELETE /documents and the retention sweep both enumerate FROM the "
        f"index, and its rows are gone.")
    kept = [k for k in ed.store._blobs if V2 in k]
    assert kept, f"reclaim took the LIVE version's blobs with it: {sorted(ed.store._blobs)}"


def test_a_failed_reupload_reclaims_nothing():
    """CONTROL for clause 2: reclaim must be reached only by an actual supersede. A version
    that survives with its blobs deleted is the same data loss wearing a different hat."""
    ed = build_edition()
    _upload(ed, V1, GOOD_V1)
    before = sorted(k for k in ed.store._blobs if V1 in k)
    try:
        _upload(ed, V2, UNPARSEABLE, mime="image/png")
    except (UnsupportedMedia, ParseProducedNoText):
        pass
    after = sorted(k for k in ed.store._blobs if V1 in k)
    assert after == before, (
        f"a FAILED re-upload reclaimed the surviving version's blobs: {before} -> {after}")


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
