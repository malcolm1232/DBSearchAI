# tests/e2e_upload.py
"""End-to-end: a doc uploaded via /admin/upload is permission-faithful in Ask.
Upload Falcon (acl=deal-team) -> alice sees it, bob does not (LAW 2 through the real
query path). Proves the upload feature can't create a permission hole.

    python3 tests/e2e_upload.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _upload import settle  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

PDF_HELLO = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n4 0 obj<</Length 52>>stream\n"
    b"BT /F1 24 Tf 20 100 Td (Hello Falcon deal-team) Tj ET\nendstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R/Size 6>>\nstartxref\n0\n%%EOF\n"
)
c = TestClient(app)


def _docs(user, q="falcon"):
    r = c.post("/search", headers={"X-DBSearch-User": user},
               json={"question": q})
    assert r.status_code == 200, r.text
    return r.json()["authorized_docs"]


def main():
    r = c.post("/admin/upload", headers={"X-DBSearch-User": "alice"},
               files={"file": ("Falcon.pdf", PDF_HELLO, "application/pdf")},
               data={"acl": ["deal-team"], "title": "Falcon"})
    assert r.status_code == 202, r.text
    ext_id = r.json()["external_id"]
    job = settle(c, r, headers={"X-DBSearch-User": "alice"})
    assert job["status"] == "succeeded", job

    alice_docs = _docs("alice")
    bob_docs = _docs("bob")
    assert ext_id in alice_docs, f"alice should see uploaded Falcon: {alice_docs}"
    assert ext_id not in bob_docs, f"LAW 2 BREACH: bob saw uploaded Falcon: {bob_docs}"

    # #90: re-uploading an EDITED Falcon.pdf (same filename, new content hash -> new
    # external_id) must SUPERSEDE the old version, not orphan it.
    edited = PDF_HELLO.replace(b"Hello Falcon deal-team", b"Bye   Falcon deal-team")
    r2 = c.post("/admin/upload", headers={"X-DBSearch-User": "alice"},
                files={"file": ("Falcon.pdf", edited, "application/pdf")},
                data={"acl": ["deal-team"], "title": "Falcon"})
    assert r2.status_code == 202, r2.text
    new_id = r2.json()["external_id"]
    job2 = settle(c, r2, headers={"X-DBSearch-User": "alice"})
    assert job2["status"] == "succeeded", job2
    assert new_id != ext_id, "edited content should mint a new external_id"
    alice_docs = _docs("alice")
    assert new_id in alice_docs, alice_docs
    assert ext_id not in alice_docs, f"#90: old version left orphaned: {alice_docs}"

    print("PASS e2e_upload (uploaded doc permission-faithful + #90 supersede)")


if __name__ == "__main__":
    main()
