"""#842 - POST /ingest is metered and gated, exactly like POST /admin/upload.

FOUND by a cold adversarial review of the 260818 money path (#837). `/ingest` takes any
signed-in caller, writes the document's raw bytes to the same blob volume the upload path
uses, and passed NEITHER gate: not the #775 storage quota, not the #831 disk-headroom guard.
Worse, the document indexed with `doc_bytes` at its model default of 0 - `doc_bytes` had
exactly ONE producer in the tree, `connectors/upload.py` - so the account's `usage_bytes`
read ~0 no matter how much had been stored. A free-tier caller could POST unbounded text in
a loop: bytes on disk, an empty progress bar, no 402 ever, and no 507 protecting the disk
for everybody else.

WHY THIS IS ADR-CONSISTENT, not a policy change. ADR 0027 rule 3 meters "the bytes an
account has ENTRUSTED"; text a caller POSTs to /ingest is entrusted in exactly the sense an
upload is. Rule 1 is the other side and must not move: connector content is never metered
because it is never held. `SharePointConnector` serves BOTH callers, so the size rides the
seed item that `Edition.ingest_document` builds - a real crawl's items carry no such key and
stay 0. `test_a_connector_crawl_is_still_not_metered` is that boundary, and it is the test
that would catch a "fix" that metered the customer's own SharePoint.

THREE CLAUSES, THREE MUTATIONS (the #793 lesson - a fixture rescued by two clauses at once
proves neither):
  1. the meter can SEE it            -> test_ingested_text_is_counted_against_usage
  2. the quota REFUSES over it       -> test_an_over_quota_ingest_is_refused_with_402
  3. the disk guard runs FIRST       -> test_a_full_disk_refuses_with_507_not_402

    PYTHONPATH=src python3 tests/selftest_842_ingest_is_metered.py
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import app as appmod  # noqa: E402
from dbsearch.server import tiers as T  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import _edition, app  # noqa: E402

client = TestClient(app)

ALICE = "44444444-4444-4444-4444-444444444444"
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


def _cookie(oid: str) -> dict:
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": "tid-1", "exp": int(time.time()) + 3600})}


def _ingest(oid: str, external_id: str, text: str):
    return client.post("/ingest", cookies=_cookie(oid), json={
        "external_id": external_id, "title": external_id, "text": text,
        "acl": [oid], "uri": f"note://{external_id}"})


def _set_ladder(value):
    if value is None:
        os.environ.pop("DBSEARCH_TIERS", None)
    else:
        os.environ["DBSEARCH_TIERS"] = value
    T.reset_cache()


def test_ingested_text_is_counted_against_usage():
    """CLAUSE 1. The blind spot itself: bytes stored, meter reading zero."""
    _login()
    _set_ladder(TINY)
    oid = "44444444-0000-0000-0000-000000000001"
    before = _edition.index.usage_bytes(_edition.tenant_id, oid)
    text = "z" * 6000
    assert _ingest(oid, "ing-842-a", text).status_code == 200
    after = _edition.index.usage_bytes(_edition.tenant_id, oid)
    assert after - before >= 6000, (
        f"#842: /ingest stored {len(text)} bytes but usage only moved by {after - before}. "
        f"The meter cannot see this path, so the quota can never enforce on it.")


def test_a_connector_crawl_is_still_not_metered():
    """THE ADR BOUNDARY (rule 1). SharePointConnector serves the crawl too - metering it
    would put the customer's own in-tenant content on our bill. A fix that set doc_bytes
    inside to_documents unconditionally passes clause 1 and breaks exactly this."""
    from dbsearch.connectors.sharepoint import SharePointConnector

    crawled = SharePointConnector(tenant_id="t", seed=[], owner_oid="o").to_documents(
        {"external_id": "sp-1", "title": "T", "uri": "u", "acl": ["g"],
         "text": "the customer's own SharePoint content"})[0]
    assert crawled.doc_bytes == 0, (
        f"a real crawl item reported {crawled.doc_bytes} metered bytes - ADR 0027 rule 1 "
        f"says connector content is never metered because it is never held.")


def test_an_over_quota_ingest_is_refused_with_402():
    """CLAUSE 2. Independent of clause 1's assertion: this drives the REFUSAL."""
    _login()
    _set_ladder(TINY)
    oid = "44444444-0000-0000-0000-000000000002"
    assert _ingest(oid, "ing-842-fill1", "x" * 9000).status_code == 200
    assert _ingest(oid, "ing-842-fill2", "y" * 9000).status_code == 200
    r = _ingest(oid, "ing-842-over", "z" * 9000)
    assert r.status_code == 402, (
        f"#842: an over-quota /ingest was accepted ({r.status_code}). /admin/upload refuses "
        f"the identical bytes with 402.")
    detail = r.json().get("detail", "").lower()
    assert "storage" in detail and "upgrade" in detail, (
        f"the refusal does not name the remedy: {detail!r}")


def test_the_refused_ingest_was_never_indexed():
    """Refuse BEFORE the write. A refusal that still indexed would leave the account over
    quota holding a document it was told it could not have."""
    _login()
    _set_ladder(TINY)
    oid = "44444444-0000-0000-0000-000000000003"
    assert _ingest(oid, "ing-842-fill3", "q" * 18000).status_code == 200
    before = _edition.index.usage_bytes(_edition.tenant_id, oid)
    assert _ingest(oid, "ing-842-rejected", "r" * 9000).status_code == 402
    after = _edition.index.usage_bytes(_edition.tenant_id, oid)
    assert after == before, f"a refused ingest still consumed {after - before} bytes"
    rows = client.get("/admin/documents", cookies=_cookie(oid)).json()
    ids = [d["doc_external_id"] for d in (rows if isinstance(rows, list) else rows.get("documents", []))]
    assert "ing-842-rejected" not in ids, f"the refused document was indexed anyway: {ids}"


def test_a_full_disk_refuses_with_507_not_402():
    """CLAUSE 3. The availability guard, and its ORDER. 507 is an operator condition; a
    caller well inside their quota must never be told to upgrade because OUR disk is full."""
    _login()
    _set_ladder(TINY)
    oid = "44444444-0000-0000-0000-000000000004"
    had_own = "free_bytes" in vars(_edition.store)
    saved = vars(_edition.store).get("free_bytes")
    _edition.store.free_bytes = lambda: 1024          # far under the 2GiB floor
    try:
        r = _ingest(oid, "ing-842-fulldisk", "s" * 500)
    finally:
        if had_own:
            _edition.store.free_bytes = saved
        else:
            del _edition.store.free_bytes             # back to the class's own behaviour
    assert r.status_code == 507, (
        f"#842: /ingest wrote to a full disk ({r.status_code}). The 507 guard protects the "
        f"volume for EVERY account and this path never ran it.")
    assert "402" not in str(r.status_code)


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
