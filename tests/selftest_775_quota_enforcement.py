"""#775 - an over-quota upload is refused loudly, and told what to do about it.

ADR 0027's LAW 2 section is the boundary this file guards: quota acts ONLY on the write
path. Nothing here changes what a caller may read, and a full account can still search
everything it already has.

The refusal itself is the part worth testing carefully, because a storage limit that fails
badly is worse than no limit at all:

  * it must refuse BEFORE ingesting, not half-way through, or the account ends up over quota
    with a document it was told it could not have
  * it must say the remedy out loud (upgrade, or delete something), because "413" on its own
    is the kind of dead end that makes people leave
  * it must not leak how much OTHER people are storing, only this caller's own numbers
  * a deployment that cannot meter must not enforce at all: a self-hosted box holds its own
    storage and is free forever (ADR 0027 rule 6), so refusing its uploads would be enforcing
    a bill nobody sends

    PYTHONPATH=src python3 tests/selftest_775_quota_enforcement.py
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

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")

#: 20KB free, so the whole test writes less than a megabyte.
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


def _upload(oid: str, name: str, body: bytes):
    return client.post("/admin/upload", cookies=_cookie(oid),
                       files={"file": (name, body, "text/plain")},
                       data={"audience": "org", "title": name})


def _upload_ok(oid: str, name: str, body: bytes) -> dict:
    """#917: an accepted upload is a 202 SUBMIT; usage reflects only INDEXED chunks, so
    every accepted upload is settled to `succeeded` before the next quota question."""
    r = _upload(oid, name, body)
    assert r.status_code == 202, f"{r.status_code} {r.text[:300]}"
    job = settle(client, r, cookies=_cookie(oid))
    assert job["status"] == "succeeded", job
    return job


def _set_ladder(value):
    if value is None:
        os.environ.pop("DBSEARCH_TIERS", None)
    else:
        os.environ["DBSEARCH_TIERS"] = value
    T.reset_cache()


def test_an_upload_within_quota_is_accepted():
    _login()
    _set_ladder(TINY)
    r = _upload(ALICE, "small-775-a.txt", b"a" * 4000)
    assert r.status_code == 202, f"{r.status_code} {r.text[:300]}"


def test_an_upload_that_would_exceed_the_quota_is_refused_and_names_the_remedy():
    _login()
    _set_ladder(TINY)
    _upload_ok(BOB, "fill-775-1.txt", b"x" * 9000)
    _upload_ok(BOB, "fill-775-2.txt", b"y" * 9000)
    r = _upload(BOB, "over-775.txt", b"z" * 9000)
    assert r.status_code == 402, f"expected 402 Payment Required, got {r.status_code} {r.text[:300]}"
    detail = r.json().get("detail", "").lower()
    for word in ("storage", "upgrade"):
        assert word in detail, f"the refusal does not mention {word!r}: {detail!r}"
    assert "delete" in detail or "remove" in detail, (
        f"the refusal offers no option except paying: {detail!r}")


def test_the_refused_document_was_never_ingested():
    """Refuse BEFORE the write, not half-way through it. A refusal that still indexed the
    document would leave the account over quota holding a file it was told it could not
    have, and the next upload would be refused because of a document that 'does not exist'."""
    _login()
    _set_ladder(TINY)
    oid = "33333333-3333-3333-3333-333333333333"
    _upload_ok(oid, "fill-775-3.txt", b"q" * 18000)
    before = _edition.index.usage_bytes(_edition.tenant_id, oid)
    r = _upload(oid, "rejected-775.txt", b"r" * 9000)
    assert r.status_code == 402, r.status_code
    after = _edition.index.usage_bytes(_edition.tenant_id, oid)
    assert after == before, f"a refused upload still consumed {after - before} bytes"
    rows = client.get("/admin/documents", cookies=_cookie(oid)).json()
    assert not any(d["doc_external_id"].startswith("upload-rejected-775") for d in rows), (
        "the refused document is in the listing")


def test_the_refusal_never_names_another_accounts_usage():
    """The numbers in the message are the caller's own. Reporting deployment-wide usage
    would turn a billing message into a metadata leak about colleagues."""
    _login()
    _set_ladder(TINY)
    oid = "44444444-4444-4444-4444-444444444444"
    _upload_ok(oid, "fill-775-4.txt", b"m" * 19000)
    r = _upload(oid, "over-775-b.txt", b"n" * 9000)
    assert r.status_code == 402
    detail = r.json().get("detail", "")
    for other in (ALICE, BOB):
        assert other not in detail, f"the refusal names another account: {detail!r}"


def test_a_deployment_that_cannot_meter_does_not_enforce():
    """ADR 0027 rule 6: a self-hosted box holds its own storage and is free forever. An
    index with no usage_bytes must therefore not have a quota applied to it at all, rather
    than being treated as 'zero used' or 'infinitely full'."""
    _login()
    _set_ladder(TINY)
    index = _edition.index
    real = type(index).usage_bytes

    def _unmetered(self, tenant_id, owner_oid):
        raise NotImplementedError("this backend cannot meter")

    type(index).usage_bytes = _unmetered
    try:
        oid = "55555555-5555-5555-5555-555555555555"
        r = _upload(oid, "unmetered-775.txt", b"u" * 30000)   # far over the 20KB ladder
        assert r.status_code == 202, (
            f"an unmeterable deployment enforced a quota anyway: {r.status_code} {r.text[:200]}")
        job = settle(client, r, cookies=_cookie(oid))
        assert job["status"] == "succeeded", job
    finally:
        type(index).usage_bytes = real


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {e}")
            except Exception as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    _set_ladder(None)
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
