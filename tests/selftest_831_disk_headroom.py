"""#831 - the disk-headroom guard: the quota protects revenue, THIS protects availability.

A per-account quota cannot prevent disk exhaustion, because it only ever counts one account:
two free accounts at quota already exceed the prod disk, and one filling upload takes prod
down for everybody rather than for the uploader. And on self-host there is no quota AT ALL -
infinite entitlement, very finite disk.

So uploads are refused when free space on the blob volume would drop under a floor,
independent of the caller's tier:

  - 507 Insufficient Storage, never 402: this is an OPERATOR condition, not a billing one,
    and the message must not send an innocent user to the upgrade button.
  - a REFUSAL with the reason named, never a silent drop (the empty-success lesson) - and
    the refused document never reaches the index.
  - the check counts the INCOMING bytes: an upload that would itself cross the floor is
    refused, not accepted-then-regretted.
  - a store that cannot fill the local disk (in-memory does not persist, cloud writes
    remotely) raises NotImplementedError from free_bytes() and is not enforced against -
    fail-open is correct here because the filesystem store is the only adapter whose
    writes land on the disk being protected.

    PYTHONPATH=src python3 tests/selftest_831_disk_headroom.py
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

from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import _edition, app  # noqa: E402

client = TestClient(app)

CARER = "33333333-3333-3333-3333-333333333333"
_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")


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


def _with_free_bytes(value, fn):
    """Give the live edition's store a fake free-space reading, restore in finally.
    Patches the CLASS (the 775 pattern): the base port defines free_bytes, so the
    override must land on the concrete type to be seen."""
    store_type = type(_edition.store)
    had = "free_bytes" in vars(store_type)
    original = vars(store_type).get("free_bytes")

    def _fake(self):
        return value

    store_type.free_bytes = _fake
    try:
        fn()
    finally:
        if had:
            store_type.free_bytes = original
        else:
            del store_type.free_bytes


def test_an_upload_that_would_cross_the_floor_is_refused_with_507():
    _login()
    os.environ["DBSEARCH_DISK_FLOOR_BYTES"] = "50"

    def go():
        # free 100, floor 50, incoming 60: 100 < 50 + 60 -> refuse.
        r = _upload(CARER, "big.txt", b"x" * 60)
        assert r.status_code == 507, (r.status_code, r.text)
        detail = r.json()["detail"].lower()
        assert "storage" in detail or "disk" in detail, detail
        for billing_word in ("quota", "upgrade", "plan", "tier"):
            assert billing_word not in detail, (
                f"a disk refusal must not talk billing ({billing_word!r}): {detail}")

    try:
        _with_free_bytes(100, go)
    finally:
        os.environ.pop("DBSEARCH_DISK_FLOOR_BYTES", None)


def test_the_incoming_bytes_count_a_smaller_upload_still_fits():
    """Same free space, same floor - only the upload size differs. This is what makes
    `free >= floor` and `free >= floor + incoming` different guards."""
    _login()
    os.environ["DBSEARCH_DISK_FLOOR_BYTES"] = "50"

    def go():
        r = _upload(CARER, "small.txt", b"y" * 20)   # 100 >= 50 + 20 -> fits
        assert r.status_code == 202, (r.status_code, r.text)
        job = settle(client, r, cookies=_cookie(CARER))   # inside the patched window
        assert job["status"] == "succeeded", job

    try:
        _with_free_bytes(100, go)
    finally:
        os.environ.pop("DBSEARCH_DISK_FLOOR_BYTES", None)


def test_the_refused_upload_never_reached_the_index():
    _login()
    os.environ["DBSEARCH_DISK_FLOOR_BYTES"] = "50"

    def go():
        r = _upload(CARER, "never-lands.txt", b"z" * 500)
        assert r.status_code == 507, (r.status_code, r.text)

    try:
        _with_free_bytes(100, go)
    finally:
        os.environ.pop("DBSEARCH_DISK_FLOOR_BYTES", None)
    docs = client.get("/admin/documents", cookies=_cookie(CARER)).json()
    assert not any("never-lands" in json.dumps(d) for d in docs), docs


def test_a_store_that_cannot_measure_is_not_enforced_against():
    """The in-memory store's free_bytes raises NotImplementedError (unpatched): it does
    not persist, so it cannot fill the disk, so nothing is protected by refusing."""
    _login()
    os.environ["DBSEARCH_DISK_FLOOR_BYTES"] = str(1024 ** 4)   # absurd floor: 1TB
    try:
        r = _upload(CARER, "unmeasured.txt", b"ok")
        assert r.status_code == 202, (r.status_code, r.text)
        job = settle(client, r, cookies=_cookie(CARER))
        assert job["status"] == "succeeded", job
    finally:
        os.environ.pop("DBSEARCH_DISK_FLOOR_BYTES", None)


def test_ample_headroom_uploads_normally():
    _login()

    def go():
        r = _upload(CARER, "plenty.txt", b"fine")
        assert r.status_code == 202, (r.status_code, r.text)
        job = settle(client, r, cookies=_cookie(CARER))   # inside the patched window
        assert job["status"] == "succeeded", job

    _with_free_bytes(10 * 1024 ** 3, go)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
