"""#917 - upload is a SUBMIT: 202 + a real ingest job with SharePoint-grade phases.

POST /admin/upload used to run parse -> chunk -> embed -> index INSIDE the request - the
exact "do it all in one request" path LAW 4 forbids, and the reason the picker could not
show the stage progress the SharePoint flow already has. It now validates synchronously
(size, mime allowlist, quota, audience) and SUBMITS the single-file crawl through the
same job runner a connector crawl uses, so /ingest/jobs/{job_id} reports the same
extracting/embedding/indexing phases the canvas stepper renders.

One clause per test:
  - the submit contract: 202 + {job_id, poll, external_id, title, acl}, and the job
    reaches `succeeded` with the document listed in /admin/documents;
  - the job walked the REAL phases (indexing reached; docs 1/1) - not a fake bar;
  - a .csv uploads end-to-end and its segments are the rows (the user's third ask -
    already wired via text/csv -> segment_csv, pinned here so it stays);
  - an unsupported type is still refused SYNCHRONOUSLY with 415 (the #551 rule: the
    honest refusal at click time, not a tile that fails later);
  - an unparseable-but-allowlisted file surfaces as a FAILED job carrying the error
    class (the async home of the old 422);
  - oversized is still a synchronous 413;
  - the ACL default (#539: private to the uploader) survives the async path.

Run: python3 tests/selftest_917_upload_is_async.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from _upload import upload_and_wait  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

H = {"X-DBSearch-User": "alice"}
c = TestClient(app)

CSV = b"region,amount\nemea,140\napac,60\n"

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        fails.append(name)
    return cond


def main() -> int:
    # ---- the submit contract + a settled document ----
    r = c.post("/admin/upload", headers=H,
               files={"file": ("notes.txt", b"the heron mascot is named Gerald", "text/plain")},
               data={"title": "notes"})
    check("submit returns 202", r.status_code == 202, f"{r.status_code} {r.text[:120]}")
    body = r.json()
    check("202 carries job_id + poll + external_id",
          bool(body.get("job_id")) and bool(body.get("poll")) and bool(body.get("external_id")),
          str(body)[:160])
    check("ACL defaults to the uploader (#539)", body.get("acl") == ["alice"], str(body.get("acl")))
    from _upload import _wait_job
    job = _wait_job(c, H, body["poll"])
    check("job succeeded", job.get("status") == "succeeded", str(job)[:160])
    check("job walked the real pipeline (1/1 docs, terminal phase from the runner)",
          job.get("docs_done") == 1 and job.get("docs_total") == 1
          and job.get("phase") in ("indexing", "done"), str(job)[:160])
    docs = c.get("/admin/documents", headers=H).json()
    check("document listed for the uploader",
          any(d["doc_external_id"] == body["external_id"] for d in docs), str(docs)[:160])

    # ---- .csv end to end: rows become segments ----
    out = upload_and_wait(c, H, files={"file": ("sales.csv", CSV, "text/csv")},
                          data={"title": "sales csv"})
    check("csv upload job succeeded", out["job"].get("status") == "succeeded",
          str(out["job"])[:160])
    check("csv produced row segments", (out.get("chunk_count") or 0) >= 1,
          f"chunk_count={out.get('chunk_count')}")

    # ---- guards that must STAY synchronous ----
    r = c.post("/admin/upload", headers=H,
               files={"file": ("a.png", b"\x89PNG", "image/png")}, data={})
    check("unsupported type refused synchronously (415)", r.status_code == 415, str(r.status_code))
    r = c.post("/admin/upload", headers=H,
               files={"file": ("big.txt", b"x" * (10 * 1024 * 1024 + 1), "text/plain")}, data={})
    check("oversized refused synchronously (413)", r.status_code == 413, str(r.status_code))

    # ---- unparseable-but-allowlisted: the async home of the old 422 ----
    out = upload_and_wait(c, H, files={"file": ("bad.pdf", b"%PDF-not-really", "application/pdf")},
                          data={})
    check("garbage pdf surfaces as a FAILED job naming the error",
          out["job"].get("status") == "failed" and "ParseProducedNoText" in
          (out["job"].get("error") or ""), str(out["job"])[:160])

    print()
    if fails:
        print(f"selftest_917: FAILED ({len(fails)}): {fails}")
        return 1
    print("selftest_917: upload is a submit with real phases; csv ingests; guards hold ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
