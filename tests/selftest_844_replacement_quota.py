"""#844 - a replacement upload is measured by what it ADDS, not by its whole size.

FOUND by the cold adversarial review of the 260818 money path (#837).

The quota asked `used + incoming <= quota`, where `used` still included the version this
upload is about to SUPERSEDE (#90: same uri, owner-scoped, deleted as part of the same
write). So re-uploading an edited 9MB file when 9MB of it is already counted looked like
18MB of new storage. A user at the top of their tier editing a document they already own got
402 "Storage full... Upgrade your plan" for an operation whose net effect was about zero -
a wrong denial with a payment demand attached, which is the worst-flavoured false refusal
this surface can produce.

Bounded by _MAX_UPLOAD_BYTES, so it only bit within 10MB of the ceiling - which is exactly
where a user is most likely to be deleting and re-uploading to make room.

The fix measures what the account will hold once the write lands: `usage_bytes` learned to
exclude the rows this write is about to replace. Two keys, because the two ingest paths
replace by different things - /admin/upload supersedes by URI, and /ingest re-POSTing an
external_id replaces by that ID (run_ingestion deletes-before-indexing).

THE CONTROL IS THE POINT: a quota that just stopped counting things would pass the headline
test and let anyone store anything. `test_a_genuinely_oversized_upload_is_still_refused` and
`test_a_different_file_still_counts_the_old_one` are what keep this honest.

    PYTHONPATH=src python3 tests/selftest_844_replacement_quota.py
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from _upload import settle  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import tiers as T  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import _edition, app  # noqa: E402

client = TestClient(app)
_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")

#: 20KB free, so the whole file writes well under a megabyte.
TINY = json.dumps([
    {"name": "free", "quota_gb": 20_000 / (1024 ** 3), "price_cents": 0},
    {"name": "plus", "quota_gb": 50, "price_cents": 99, "stripe_price": "price_plus"},
])


def _login():
    for k in _VARS:
        os.environ.pop(k, None)
    os.environ.update({"AUTH_TENANT_ID": "tid-1", "AUTH_CLIENT_ID": "cid",
                       "AUTH_CLIENT_SECRET": "sec", "DBSEARCH_OPERATOR_OIDS": ""})
    os.environ["DBSEARCH_TIERS"] = TINY
    T.reset_cache()


def _cookie(oid):
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": "tid-1", "exp": int(time.time()) + 3600})}


def _upload(oid, name, body):
    return client.post("/admin/upload", cookies=_cookie(oid),
                       files={"file": (name, body, "text/plain")},
                       data={"audience": "org", "title": name})


def _upload_ok(oid, name, body):
    """#917: an accepted upload is a 202 SUBMIT; usage reflects only INDEXED chunks, so
    every accepted upload is settled to `succeeded` before the next quota question."""
    r = _upload(oid, name, body)
    assert r.status_code == 202, f"{r.status_code} {r.text[:300]}"
    job = settle(client, r, cookies=_cookie(oid))
    assert job["status"] == "succeeded", job
    return job


def _usage(oid):
    return _edition.index.usage_bytes(_edition.tenant_id, oid)


def test_a_replacement_that_nets_zero_is_accepted():
    """The defect verbatim: at the top of the tier, editing a file you already own."""
    _login()
    oid = "844-0000-0000-0000-000000000001"
    _upload_ok(oid, "big-844.txt", b"a" * 15000)
    used = _usage(oid)
    assert used >= 15000, f"fixture is broken: usage did not record the file ({used})"

    # SAME filename -> same uri -> supersedes. Net change is ~0 against a 20000 quota,
    # but used + incoming would be 15000 + 15000 = 30000.
    r = _upload(oid, "big-844.txt", b"b" * 15000)
    assert r.status_code == 202, (
        f"#844: a replacement that nets ~zero was refused {r.status_code} "
        f"({r.json().get('detail', '')[:120]}). used={used}, incoming=15000, quota=20000 - "
        f"the superseded version was counted twice.")
    job = settle(client, r, cookies=_cookie(oid))
    assert job["status"] == "succeeded", job
    after = _usage(oid)
    assert after <= used + 500, (
        f"the replacement was accepted but usage grew as if both versions were kept: "
        f"{used} -> {after}")


def test_a_genuinely_oversized_upload_is_still_refused():
    """CONTROL. A 'fix' that simply counted less would pass the test above and mean nobody
    can ever be over quota."""
    _login()
    oid = "844-0000-0000-0000-000000000002"
    _upload_ok(oid, "fill-844-a.txt", b"x" * 9000)
    _upload_ok(oid, "fill-844-b.txt", b"y" * 9000)
    r = _upload(oid, "over-844.txt", b"z" * 9000)
    assert r.status_code == 402, (
        f"a genuinely over-quota upload was accepted ({r.status_code}) - the quota stopped "
        f"enforcing at all")


def test_a_different_file_still_counts_the_old_one():
    """CONTROL for the KEY. The exclusion must be the uri this upload actually supersedes -
    excluding by owner, or unconditionally, would stop counting everything they hold."""
    _login()
    oid = "844-0000-0000-0000-000000000003"
    _upload_ok(oid, "keep-844.txt", b"k" * 15000)
    r = _upload(oid, "other-844.txt", b"o" * 9000)   # DIFFERENT name -> no supersede
    assert r.status_code == 402, (
        f"a different file was accepted ({r.status_code}) even though the existing 15000 "
        f"bytes are not superseded by it - the exclusion is not keyed on the uri")


def test_reposting_the_same_ingest_id_is_a_replacement_too():
    """The /ingest key. That path does not supersede by uri, but run_ingestion
    deletes-before-indexing for the id it writes, so re-POSTing an id replaces it."""
    _login()
    oid = "844-0000-0000-0000-000000000004"

    def ingest(doc_id, text):
        return client.post("/ingest", cookies=_cookie(oid), json={
            "external_id": doc_id, "title": doc_id, "text": text,
            "acl": [oid], "uri": f"note://{doc_id}"})

    assert ingest("ing-844", "m" * 15000).status_code == 200
    r = ingest("ing-844", "n" * 15000)
    assert r.status_code == 200, (
        f"#844: re-POSTing the same external_id was refused {r.status_code} - it REPLACES "
        f"the rows it is rewriting, so its old bytes must not be counted against it")


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
