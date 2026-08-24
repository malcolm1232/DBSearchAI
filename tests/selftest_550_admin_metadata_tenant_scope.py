"""#550 - /admin/principals and /admin/sources are scoped to the caller's OWN tenant.

Both routes stay open to any signed-in user on purpose (#549 -> #550): /admin/principals
backs the ACL picker and /admin/sources restores a user's own SharePoint state, and gating
them by ROLE broke both. Their real defect is that neither was scoped to a tenant, so on the
open-signup deployment a SOLO account (a Google/email signup - e.g. 123@gmail.com, partition
`acct:<oid>`) or a FOREIGN Entra tenant could enumerate the HOME org's directory (group and
people NAMES, incl. "Global Administrator") and its source names + tenant metadata. Content,
credentials and query history were never exposed - this is a metadata disclosure - but for a
product whose whole claim is permission-faithfulness, a stranger reading the org directory is
the leak that most undercuts it. Relevant now because the product accepts open sign-up.

The fix is a HOME-TENANT scope, mirroring the codebase's existing home/foreign model
(`_is_foreign_partition`): the directory and the source registry belong to the deployment's
home tenant (`partition == _edition.tenant_id`), because that is the only tenant this
deployment can enumerate. A solo `acct:` account or a foreign tenant sees NEITHER - which is
also the honest answer (we cannot enumerate their directory), and it leaves the ACL picker's
"Only you / paste an oid" escape hatch, so nothing an ordinary solo user actually does breaks.

A DEV RIG is unaffected: dev-auth flattens every caller to the deployment constant
(resolve_tenant), so `partition == _edition.tenant_id` holds for everyone there, exactly as
`is_operator` is True for everyone there (ADR 0011 s3).

    PYTHONPATH=src python3 tests/selftest_550_admin_metadata_tenant_scope.py
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("SELFHOST_BACKEND", "memory")
os.environ.setdefault("DBSEARCH_SESSION_KEY", "test-session-key-550-deterministic")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server import app as app_module  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)

_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")
HOME = app_module._edition.tenant_id      # the deployment's home tenant partition
SECRET_GROUP = "SECRET Directory Group - Global Administrator"
SECRET_PERSON = "Confidential Home Person"


def _real_login_on():
    for k in _VARS:
        os.environ.pop(k, None)
    os.environ.update({"AUTH_TENANT_ID": HOME, "AUTH_CLIENT_ID": "cid",
                       "AUTH_CLIENT_SECRET": "sec"})


def _cookie(oid, tid):
    sess = {"oid": oid, "exp": int(time.time()) + 3600}
    if tid is not None:
        sess["tid"] = tid
    return {user_auth.COOKIE: user_auth.sign_session(sess)}


def HOME_USER(): return _cookie("home-oid", HOME)                  # partition == HOME
def FOREIGN_USER(): return _cookie("foreign-oid", "tid-OTHERORG")  # a different Entra tenant
def SOLO_USER(): return _cookie("solo-oid", None)                  # Google/email signup -> acct:<oid>


def _seed_home_directory():
    ident = app_module._edition.identity
    ident._user_groups["home-oid"] = ["secret-grp-oid"]
    ident.set_principal_name("secret-grp-oid", SECRET_GROUP)
    ident.set_principal_name("home-oid", SECRET_PERSON)


def test_the_home_org_user_still_sees_the_directory_and_sources():
    """The control: the fix must not lock out the people the routes exist for. A home-tenant
    user gets the named directory (the ACL picker) and the source registry (SharePoint
    restore) exactly as before."""
    _real_login_on()
    _seed_home_directory()
    pr = client.get("/admin/principals", cookies=HOME_USER()).json()
    assert pr.get("available") is True, f"the home user lost the directory: {pr}"
    names = [p["name"] for p in pr["principals"]]
    assert SECRET_GROUP in names, f"the home user cannot see their own directory: {names}"
    src = client.get("/admin/sources", cookies=HOME_USER()).json()
    assert any("SharePoint" in s.get("display_name", "") for s in src), (
        f"the home user lost sight of the deployment's sources: {src}")
    print("  PASS  the home-tenant user still sees the directory and the sources")


def test_a_solo_account_cannot_enumerate_the_home_directory():
    """THE LEAK, as the owner meets it: 123@gmail.com is a Google/email signup - partition
    acct:<oid>, no org. It must NOT be able to read the home org's group/people names."""
    _real_login_on()
    _seed_home_directory()
    r = client.get("/admin/principals", cookies=SOLO_USER())
    assert r.status_code == 200, r.status_code
    pr = r.json()
    assert pr.get("available") is False, (
        f"a solo account was handed the home org's directory (available=true): {pr}")
    blob = json.dumps(pr)
    assert SECRET_GROUP not in blob and SECRET_PERSON not in blob, (
        f"a solo account can read the home org's directory NAMES - the #550 leak: {blob[:200]}")
    print("  PASS  a solo account cannot enumerate the home org's directory")


def test_a_solo_account_gets_no_sources():
    _real_login_on()
    r = client.get("/admin/sources", cookies=SOLO_USER())
    assert r.status_code == 200, r.status_code
    src = r.json()
    assert src == [], (
        f"a solo account can read the deployment's source names + counts + tenant metadata: {src}")
    print("  PASS  a solo account gets an empty source list")


def test_a_foreign_tenant_user_cannot_enumerate_the_home_directory_or_sources():
    """The other half: a signed-in user from a DIFFERENT Entra tenant. The deployment can only
    enumerate its OWN tenant, so a foreign user's honest answer is 'nothing here is yours'."""
    _real_login_on()
    _seed_home_directory()
    pr = client.get("/admin/principals", cookies=FOREIGN_USER()).json()
    assert pr.get("available") is False, f"a foreign tenant saw the home directory: {pr}"
    assert SECRET_GROUP not in json.dumps(pr), "a foreign tenant read the home org's names"
    assert client.get("/admin/sources", cookies=FOREIGN_USER()).json() == [], (
        "a foreign tenant saw the home deployment's sources")
    print("  PASS  a foreign-tenant user sees neither the directory nor the sources")


def test_a_dev_rig_is_unaffected():
    """A dev rig (no real login) flattens every caller to the deployment constant, so the
    picker must keep working there - a fix that broke local rigs would relocate the pain."""
    for k in _VARS:
        os.environ.pop(k, None)
    os.environ["DBSEARCH_DEV_AUTH"] = "1"
    try:
        _seed_home_directory()
        pr = client.get("/admin/principals", headers={"X-DBSearch-User": "home-oid"}).json()
        assert pr.get("available") is True, f"the dev rig lost its directory: {pr}"
        assert SECRET_GROUP in [p["name"] for p in pr["principals"]], pr
    finally:
        os.environ.pop("DBSEARCH_DEV_AUTH", None)
    print("  PASS  a dev rig keeps its directory (everyone is the home tenant there)")


if __name__ == "__main__":
    failures = []
    for name in ["test_the_home_org_user_still_sees_the_directory_and_sources",
                 "test_a_solo_account_cannot_enumerate_the_home_directory",
                 "test_a_solo_account_gets_no_sources",
                 "test_a_foreign_tenant_user_cannot_enumerate_the_home_directory_or_sources",
                 "test_a_dev_rig_is_unaffected"]:
        try:
            globals()[name]()
        except AssertionError as e:
            failures.append(name); print(f"FAIL  {name}\n      {e}")
        except Exception as e:
            failures.append(name); print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
