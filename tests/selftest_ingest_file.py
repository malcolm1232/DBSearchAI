"""Self-test: Edition.ingest_file runs raw bytes through the pipeline with the rich
extractor and indexes a permission-faithful doc.

    python3 tests/selftest_ingest_file.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.ports.base import ReadScope  # noqa: E402

from dbsearch.server.edition import build_edition  # noqa: E402

PDF_HELLO = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 52>>stream\n"
    b"BT /F1 24 Tf 20 100 Td (Hello Falcon deal-team) Tj ET\n"
    b"endstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R/Size 6>>\nstartxref\n0\n%%EOF\n"
)


def test_ingest_and_trim():
    ed = build_edition()
    chunks = ed.ingest_file(
        external_id="upload-falcon-test",
        title="Falcon.pdf",
        data=PDF_HELLO,
        mime="application/pdf",
        acl=["deal-team"],
        uri="upload://Falcon.pdf",
    )
    assert chunks >= 1, chunks

    # LAW 2: alice (deal-team) retrieves it; bob (all-staff) does not.
    alice = ed.query_service.answer("alice", "falcon")
    bob = ed.query_service.answer("bob", "falcon")
    assert "upload-falcon-test" in alice.retrieved_docs, alice.retrieved_docs
    assert "upload-falcon-test" not in bob.retrieved_docs, bob.retrieved_docs


def test_edited_reupload_supersedes_old_version():
    """#90: an EDITED re-upload of the same file mints a NEW content-addressed
    external_id — the uri (upload://filename) is the stable logical identity, so the
    old version (and its possibly LOOSER ACL) must be superseded, never orphaned."""
    ed = build_edition()
    ed.ingest_document("upload-report-v1", "Report.txt",
                       "quarterly figures draft with secret alpha numbers",
                       ["all-staff"], uri="upload://Report.txt")
    # edited + ACL tightened -> different content hash -> different external_id
    ed.ingest_file(
        external_id="upload-report-v2",
        title="Report.txt",
        data=b"quarterly figures final with secret beta numbers",
        mime="text/plain",
        acl=["deal-team"],
        uri="upload://Report.txt",
    )
    docs = {d.doc_external_id for d in ed.index.list_doc_acls(ReadScope(ed.tenant_id))}
    assert "upload-report-v1" not in docs, "old looser-ACL version left orphaned (#90)"
    assert "upload-report-v2" in docs, docs
    # bob (all-staff) must not still see the OLD all-staff copy's content
    bob = ed.query_service.answer("bob", "secret alpha quarterly figures")
    assert "upload-report-v1" not in bob.retrieved_docs, bob.retrieved_docs
    # an unrelated doc with a DIFFERENT uri is untouched
    ed.ingest_document("other-doc", "Other.txt", "unrelated memo about parking",
                       ["all-staff"], uri="upload://Other.txt")
    ed.ingest_file(external_id="upload-report-v3", title="Report.txt",
                   data=b"quarterly figures final v3", mime="text/plain",
                   acl=["deal-team"], uri="upload://Report.txt")
    docs = {d.doc_external_id for d in ed.index.list_doc_acls(ReadScope(ed.tenant_id))}
    assert "other-doc" in docs and "upload-report-v2" not in docs, docs


def test_cross_owner_same_name_upload_survives():
    """#791: two DIFFERENT users each upload their own file named Report.pdf to the
    SAME tenant partition. The supersede-by-uri loop (#90) must be owner-scoped: bob's
    upload must never delete alice's document just because the filenames collide."""
    ed = build_edition()
    ed.ingest_file(
        external_id="upload-report-alice", title="Report.pdf",
        data=b"alice quarterly figures", mime="text/plain",
        acl=["deal-team"], uri="upload://Report.pdf",
        owner_oid="oid-alice",
    )
    ed.ingest_file(
        external_id="upload-report-bob", title="Report.pdf",
        data=b"bob completely different report", mime="text/plain",
        acl=["all-staff"], uri="upload://Report.pdf",
        owner_oid="oid-bob",
    )
    docs = {d.doc_external_id for d in ed.index.list_doc_acls(ReadScope(ed.tenant_id))}
    assert "upload-report-alice" in docs, f"bob's same-named upload DELETED alice's doc (#791): {docs}"
    assert "upload-report-bob" in docs, docs


def test_same_owner_reupload_still_supersedes_with_owner_set():
    """#791 guard for #90: when the SAME owner re-uploads an edited file, the old
    version must still be superseded — owner-scoping the loop must not orphan it."""
    ed = build_edition()
    ed.ingest_file(
        external_id="upload-memo-v1", title="Memo.txt",
        data=b"memo draft with secret alpha", mime="text/plain",
        acl=["all-staff"], uri="upload://Memo.txt",
        owner_oid="oid-alice",
    )
    ed.ingest_file(
        external_id="upload-memo-v2", title="Memo.txt",
        data=b"memo final with secret beta", mime="text/plain",
        acl=["deal-team"], uri="upload://Memo.txt",
        owner_oid="oid-alice",
    )
    docs = {d.doc_external_id for d in ed.index.list_doc_acls(ReadScope(ed.tenant_id))}
    assert "upload-memo-v1" not in docs, f"same-owner re-upload left old version orphaned (#90): {docs}"
    assert "upload-memo-v2" in docs, docs


def main():
    test_ingest_and_trim()
    test_edited_reupload_supersedes_old_version()
    test_cross_owner_same_name_upload_survives()
    test_same_owner_reupload_still_supersedes_with_owner_set()
    print("PASS selftest_ingest_file (incl. #90 supersede-by-uri, #791 owner-scoped)")


if __name__ == "__main__":
    main()
