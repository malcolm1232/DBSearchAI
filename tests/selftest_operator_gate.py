"""ADR 0011 s3: /config's `operator` flag and env_present gating.
- non-real-login rig -> operator true (the deployment IS the operator's)
- real login + oid on DBSEARCH_OPERATOR_OIDS -> true, env_present populated
- real login + foreign/absent oid -> false, env_present [] (env names are operator data)
- the oid list itself never appears in the response

    PYTHONPATH=src python3 tests/selftest_operator_gate.py
"""
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")
os.environ["AZURE_SQL_SERVER"] = "resolvable.example"     # one resolvable connector var

from fastapi.testclient import TestClient  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")


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


def test_dev_rig_is_operator():
    _real_login(False)
    c = client.get("/config").json()
    assert c["operator"] is True
    assert "AZURE_SQL_SERVER" in c["env_present"]

def test_listed_oid_is_operator():
    _real_login(True, operators="op-1, op-2")
    c = client.get("/config", cookies=_cookie("op-1")).json()
    assert c["operator"] is True
    assert "AZURE_SQL_SERVER" in c["env_present"]

def test_foreign_oid_is_not():
    _real_login(True, operators="op-1")
    c = client.get("/config", cookies=_cookie("stranger-9"))
    body = c.json()
    assert body["operator"] is False
    assert body["env_present"] == []
    assert "op-1" not in json.dumps(body)      # the list never ships

def test_no_cookie_under_real_login_is_not():
    _real_login(True, operators="op-1")
    assert client.get("/config").json()["operator"] is False

if __name__ == "__main__":
    try:
        test_dev_rig_is_operator()
        test_listed_oid_is_operator()
        test_foreign_oid_is_not()
        test_no_cookie_under_real_login_is_not()
    finally:
        for k in _VARS:
            os.environ.pop(k, None)
    print("OK selftest_operator_gate")
