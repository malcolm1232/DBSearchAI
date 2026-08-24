"""Self-test: POST /admin/upload — multipart ingest + guard status codes.

    python3 tests/selftest_upload_endpoint.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _upload import upload_and_wait  # noqa: E402
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
H = {"X-DBSearch-User": "alice"}
c = TestClient(app)


def main():
    # happy path
    # #917: upload is a SUBMIT (202 + job) - the helper follows it to the settled doc.
    body = upload_and_wait(c, H,
               files={"file": ("Falcon.pdf", PDF_HELLO, "application/pdf")},
               data={"acl": ["deal-team"], "title": "Falcon"})
    assert body["job"]["status"] == "succeeded", body["job"]
    assert body["acl"] == ["deal-team"] and body["chunk_count"] >= 1, body

    # #539: a MISSING acl is no longer a 400 — it means PRIVATE TO THE UPLOADER.
    # This assertion was inverted deliberately, because the 400 it used to demand was the live
    # bug: the upload form could only offer the DEMO groups, so a real signed-in user picked a
    # principal they did not hold and then could not read their own document back ("no documents
    # you are permitted to see have been indexed yet"). Defaulting to the caller cannot widen
    # access — it returns the bytes to the person who just supplied them.
    r = c.post("/admin/upload", headers=H,
               files={"file": ("a.pdf", PDF_HELLO, "application/pdf")}, data={})
    assert r.status_code == 202, (r.status_code, r.text)
    assert r.json()["acl"] == ["alice"], r.json()["acl"]

    # 415 unsupported mime
    r = c.post("/admin/upload", headers=H,
               files={"file": ("a.png", b"\x89PNG", "image/png")},
               data={"acl": ["all-staff"]})
    assert r.status_code == 415, r.status_code

    # textless pdf: the async home of the old 422 - a FAILED job naming the error (#917)
    empty = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
             b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
             b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]>>endobj\n"
             b"trailer<</Root 1 0 R/Size 4>>\nstartxref\n0\n%%EOF\n")
    out = upload_and_wait(c, H,
               files={"file": ("e.pdf", empty, "application/pdf")},
               data={"acl": ["all-staff"]})
    assert out["job"]["status"] == "failed" and "ParseProducedNoText" in out["job"]["error"], out["job"]

    # 413 oversized upload — Content-Length pre-read guard
    oversized = b"%PDF-1.4 " + b"0" * (10 * 1024 * 1024 + 100)
    r = c.post("/admin/upload", headers=H,
               files={"file": ("big.pdf", oversized, "application/pdf")},
               data={"acl": ["all-staff"]})
    assert r.status_code == 413, (r.status_code, r.text)

    print("PASS selftest_upload_endpoint (5/5)")


if __name__ == "__main__":
    main()
