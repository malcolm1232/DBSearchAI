"""#539 — a person who uploads a document must be able to read it back.

THE BUG, reproduced on the LIVE site before this was written: a signed-in user uploaded an
HR policy through the Admin console, it indexed cleanly ("ACL all-staff"), and then /ask
told them:

    "No documents you are permitted to see have been indexed yet.
     Ask whoever administers your sources for access."

Nothing was broken. The permission trim did exactly its job. The ACL it was handed was
nonsense: `/admin/upload` REQUIRES an acl, and the only thing the UI could offer was the
DEMO groups (all-staff, deal-team) — not the principals the uploader actually holds. So the
user picked a plausible-looking group, the document landed ACL'd to somebody else, and
their own upload became invisible to them.

THE FIX (Malcolm's call, 260806b): an omitted acl means PRIVATE TO THE UPLOADER — the
caller's own oid — rather than a 400. Sharing is a separate deliberate act, not a guess
made at upload time by someone who has no way to guess right.

Why default-private is the safe direction rather than a loosening: the alternative defaults
are a GROUP (which is precisely this bug, and over-shares) or a hard failure (which is what
users hit today). Defaulting to the caller can never widen access beyond the one person who
already holds the bytes they just uploaded.

    PYTHONPATH=src python3 tests/selftest_539_upload_private_by_default.py
"""
import io
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from _upload import settle  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)

ALICE = "aaaaaaaa-0000-0000-0000-000000000539"
BOB = "bbbbbbbb-0000-0000-0000-000000000539"
_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")

BODY = b"Primary carers receive 18 weeks of fully paid parental leave. Meal allowance 65 euros."


def _real_login(on: bool, operators: str = ""):
    for k in _VARS:
        os.environ.pop(k, None)
    if on:
        os.environ.update({"AUTH_TENANT_ID": "tid-1", "AUTH_CLIENT_ID": "cid",
                           "AUTH_CLIENT_SECRET": "sec"})
    if operators:
        os.environ["DBSEARCH_OPERATOR_OIDS"] = operators


def _cookie(oid: str) -> dict:
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": "tid-1", "exp": int(time.time()) + 3600})}


def _upload(oid, name, acl=None):
    """Post the multipart form exactly as the browser does — including the case the browser
    now sends, which is NO acl field at all."""
    data = {"title": name}
    files = {"file": (name, io.BytesIO(BODY), "text/plain")}
    if acl is not None:
        data["acl"] = acl
    return client.post("/admin/upload", cookies=_cookie(oid), data=data, files=files)


def test_upload_without_an_acl_is_private_to_the_uploader():
    _real_login(True)
    r = _upload(ALICE, "hr-policy-a.txt")
    assert r.status_code == 202, f"upload with no acl was refused: {r.status_code} {r.text[:200]}"
    assert r.json()["acl"] == [ALICE], f"expected private-to-uploader, got {r.json()['acl']}"


def test_the_uploader_can_actually_read_it_back():
    """The whole point. This is the assertion the live failure would have caught."""
    _real_login(True)
    r = _upload(ALICE, "hr-policy-b.txt")
    assert r.status_code == 202, (r.status_code, r.text[:200])
    job = settle(client, r, cookies=_cookie(ALICE))
    assert job["status"] == "succeeded", job
    listing = json.dumps(client.get("/admin/documents", cookies=_cookie(ALICE)).json())
    assert "hr-policy-b.txt" in listing, "the uploader cannot see their own document"

    answer = client.post("/search", cookies=_cookie(ALICE),
                         json={"question": "how many weeks of paid parental leave"})
    assert answer.status_code == 200
    blob = json.dumps(answer.json())
    assert "18 weeks" in blob or "18" in blob, f"uploader cannot query their own document: {blob[:300]}"


def test_it_stays_private_from_everyone_else():
    _real_login(True)
    r = _upload(ALICE, "hr-policy-c.txt")
    assert r.status_code == 202, (r.status_code, r.text[:200])
    job = settle(client, r, cookies=_cookie(ALICE))
    assert job["status"] == "succeeded", job   # indexed, so bob's blindness below is real
    blob = json.dumps(client.post("/search", cookies=_cookie(BOB),
                                  json={"question": "how many weeks of paid parental leave"}).json())
    assert "18 weeks" not in blob, "a private upload leaked to another user"
    assert "hr-policy-c.txt" not in json.dumps(
        client.get("/admin/documents", cookies=_cookie(BOB)).json()), "bob can see alice's upload"


def test_an_explicit_acl_is_still_honoured():
    """Defaulting must not remove the ability to say otherwise — the connector and manifest
    paths depend on setting an ACL deliberately."""
    _real_login(True)
    r = _upload(ALICE, "hr-policy-d.txt", acl=["deal-team"])
    assert r.status_code == 202
    assert r.json()["acl"] == ["deal-team"], f"explicit acl was overridden: {r.json()['acl']}"


def test_an_empty_acl_field_is_treated_as_omitted():
    """A form that posts acl="" (an untouched control) must mean 'private', not 400 — that
    is the shape the old UI actually sent, and a 400 here is the live bug in a new costume."""
    _real_login(True)
    r = _upload(ALICE, "hr-policy-e.txt", acl=[""])
    assert r.status_code == 202, f"empty acl still refused: {r.status_code} {r.text[:200]}"
    assert r.json()["acl"] == [ALICE], f"expected private-to-uploader, got {r.json()['acl']}"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
    _real_login(False)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
