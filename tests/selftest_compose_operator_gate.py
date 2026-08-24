"""#423: the two manifest powers that belong to whoever RUNS the deployment.

Compose accepted, from ANY signed-in caller:
  1. `${ENV}` references, which the server resolves out of its OWN environment. A stranger
     could name `AUTH_CLIENT_SECRET` as a `host`, and the resolved value came straight back
     in the store's failure reason - a credential read primitive, and an env presence oracle.
  2. `kind: folder` (and `csv` with `files:`), which point the ingest rail at a path on OUR
     disk and then make its contents queryable - arbitrary server-side file read.

Both are now refused server-side for a non-operator, with data-free messages. Two things
that must NOT change: an operator keeps both (their deployment, their environment), and the
workspace REBUILD path replays a stored operator manifest untouched - the check is keyed on
who is asking at compose time, never on what a stored manifest happens to contain.

Separately, a skipped store's reason no longer echoes a server-resolved value.

    PYTHONPATH=src python3 tests/selftest_compose_operator_gate.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
for _k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dbsearch import router as r  # noqa: E402
from dbsearch.router.provider import StoreProviderPort  # noqa: E402
from dbsearch.router.store import SEMANTIC, StoreProfile, INDEXED  # noqa: E402
from dbsearch.server import router_api  # noqa: E402
from dbsearch.server.edition import build_edition  # noqa: E402
from dbsearch.server.manifest_store import InMemoryManifestStore  # noqa: E402

SECRET = "s3cret-value-that-must-never-be-echoed"
OPERATOR, STRANGER = "op-1", "stranger-9"
_TABLE = {"sales": {"columns": ["region", "amount"], "rows": [["emea", 100]]}}
_LOGIN = {"AUTH_TENANT_ID": "tid-1", "AUTH_CLIENT_ID": "cid", "AUTH_CLIENT_SECRET": SECRET}
_VARS = tuple(_LOGIN) + ("DBSEARCH_OPERATOR_OIDS",)


def _real_login(on: bool, operators: str = "") -> None:
    for k in _VARS:
        os.environ.pop(k, None)
    if on:
        os.environ.update(_LOGIN)
    if operators:
        os.environ["DBSEARCH_OPERATOR_OIDS"] = operators


def _manifest(store: dict) -> dict:
    return {"tenant": "acme", "stores": [store]}


def _csv(**over) -> dict:
    e = {"id": "s1", "kind": "csv", "business_unit": "eng", "acl": [STRANGER, OPERATOR],
         "config": {"tables": _TABLE}}
    e.update(over)
    return e


ENV_REF_STORE = _csv(config={"tables": _TABLE, "topics": ["${AUTH_CLIENT_SECRET}"]})
FOLDER_STORE = _csv(kind="folder", config={"path": "/etc"})
CSV_FILES_STORE = _csv(config={"files": ["/etc/passwd"]})


def _app(manifest_store=None):
    def current_user(request: Request) -> str:
        return request.headers["X-Test-User"]

    app = FastAPI()
    app.include_router(router_api.build_router_api(
        build_edition(), current_user, manifest_store=manifest_store,
        force_per_user_workspaces=True))
    return TestClient(app)


def _compose(client, manifest, user):
    return client.post("/router/compose", json={"manifest": manifest},
                       headers={"X-Test-User": user})


# --------------------------------------------------------------------- C2/C3: ${ENV} refs

def test_stranger_cannot_make_the_server_resolve_its_own_env():
    _real_login(True, operators=OPERATOR)
    resp = _compose(_app(), _manifest(ENV_REF_STORE), STRANGER)
    assert resp.status_code == 400, (resp.status_code, resp.text)
    assert "operator-only" in resp.json()["detail"], resp.text
    assert SECRET not in resp.text, "the refusal itself leaked the value"
    assert "AUTH_CLIENT_SECRET" not in resp.text, \
        "the refusal echoes the variable the caller probed - keep it data-free"


def test_stranger_cannot_smuggle_an_env_ref_through_a_sibling_field():
    """The first gate looked at `config:` and `delegation:` only - but provisioning builds
    the resolved dict from `id`, `business_unit`, `title` and `description` TOO, so
    `title: "${AUTH_CLIENT_SECRET}"` composed 200 with the client secret verbatim in the
    response and persisted into /router/catalog. A gate that enumerates fields drifts the
    moment someone adds one to that dict, which is exactly how this hole was born."""
    _real_login(True, operators=OPERATOR)
    for field in ("title", "description", "business_unit", "id"):
        entry = _csv(**{field: "${AUTH_CLIENT_SECRET}"})
        resp = _compose(_app(), _manifest(entry), STRANGER)
        assert resp.status_code == 400, (field, resp.status_code, resp.text)
        assert "operator-only" in resp.json()["detail"], (field, resp.text)
        assert SECRET not in resp.text, f"the {field} vector leaked the value"


def test_the_sibling_field_leak_never_reaches_the_catalog():
    """The response is not the only exposure: a composed store keeps its resolved title, so
    the value came back from /router/catalog on every later read."""
    _real_login(True, operators=OPERATOR)
    client = _app()
    _compose(client, _manifest(_csv(title="${AUTH_CLIENT_SECRET}")), STRANGER)
    cat = client.get("/router/catalog", headers={"X-Test-User": STRANGER})
    assert SECRET not in cat.text, f"the secret is readable from the catalog: {cat.text[:400]}"


def test_operator_keeps_env_refs_in_sibling_fields():
    _real_login(True, operators=OPERATOR)
    resp = _compose(_app(), _manifest(_csv(title="${AUTH_CLIENT_SECRET}")), OPERATOR)
    assert resp.status_code == 200, (resp.status_code, resp.text)


def test_stranger_cannot_slip_an_env_ref_through_a_delegation_block():
    _real_login(True, operators=OPERATOR)
    entry = _csv(delegation={"kind": "obo", "client_secret": "${AUTH_CLIENT_SECRET}"})
    resp = _compose(_app(), _manifest(entry), STRANGER)
    assert resp.status_code == 400, (resp.status_code, resp.text)
    assert "operator-only" in resp.json()["detail"], resp.text
    assert SECRET not in resp.text


def test_operator_keeps_env_refs():
    """Their deployment, their environment - and the local rig depends on this."""
    _real_login(True, operators="OP-1")        # M6: oids are GUIDs, case-insensitive
    resp = _compose(_app(), _manifest(ENV_REF_STORE), OPERATOR)
    assert resp.status_code == 200, (resp.status_code, resp.text)


def test_dev_rig_is_untouched():
    """No real login = somebody's own machine; every caller there is the operator.

    The folder store points at an empty temp dir the test owns: this asserts the GATE stays
    out of the way, and a real directory keeps the assertion about the gate rather than
    about whether this machine lets the test process read some system path."""
    import tempfile
    _real_login(False)
    with tempfile.TemporaryDirectory() as tmp:
        for m in (_manifest(ENV_REF_STORE), _manifest(_csv(kind="folder",
                                                           config={"path": tmp}))):
            resp = _compose(_app(), m, "alice")
            assert resp.status_code == 200, (resp.status_code, resp.text)


# ------------------------------------------------------------------ C4: local file sources

def test_stranger_cannot_index_the_servers_filesystem():
    _real_login(True, operators=OPERATOR)
    for m in (_manifest(FOLDER_STORE), _manifest(CSV_FILES_STORE)):
        resp = _compose(_app(), m, STRANGER)
        assert resp.status_code == 400, (resp.status_code, resp.text)
        assert "local file sources are operator-only" in resp.json()["detail"], resp.text


def test_a_strangers_own_inline_data_still_composes():
    """The gate is about the SERVER's files and environment, not about csv stores."""
    _real_login(True, operators=OPERATOR)
    resp = _compose(_app(), _manifest(_csv()), STRANGER)
    assert resp.status_code == 200, (resp.status_code, resp.text)
    assert resp.json()["stores"], f"the store must really compose: {resp.text}"


# --------------------------------------------------------- the rebuild path is not a caller

def test_rebuild_replays_a_stored_operator_manifest_ungated():
    """A workspace warming itself from Postgres is not a request. Gating it would delete an
    operator's own stores the first time their process restarted."""
    _real_login(True, operators=OPERATOR)
    store = InMemoryManifestStore()
    assert _compose(_app(store), _manifest(ENV_REF_STORE), OPERATOR).status_code == 200
    # ...now the SAME owner is no longer an operator (list rotated), and the process restarts.
    _real_login(True, operators="someone-else")
    cold = _app(store)
    cat = cold.get("/router/catalog", headers={"X-Test-User": OPERATOR})
    assert cat.status_code == 200, (cat.status_code, cat.text)
    assert "s1" in cat.text, f"the stored workspace failed to rebuild: {cat.text[:400]}"


# ------------------------------------------------- C3: skipped reasons echo no resolved value

class _Boom(StoreProviderPort):
    """A provider whose driver quotes what it was handed - which is what real ones do."""
    kind = "boom"
    modes = ("pushdown",)

    def probe(self, config: dict) -> StoreProfile:
        return StoreProfile(store_id=config["id"], title="", description="", kind=INDEXED,
                            capabilities={SEMANTIC}, business_unit="")

    def build(self, config: dict):
        raise RuntimeError(f"connection to host {config['host']!r} refused")


def _skipped_reason(config: dict) -> str:
    reg = r.ProviderRegistry()
    reg.register(_Boom())
    skipped: list = []
    r.load_manifest({"tenant": "acme", "stores": [
        {"id": "s1", "kind": "boom", "business_unit": "eng", "acl": ["u"],
         "config": config}]}, registry=reg, skipped=skipped)
    assert len(skipped) == 1, skipped
    return skipped[0]["reason"]


def test_a_failure_reason_never_echoes_a_resolved_env_value():
    os.environ.update(_LOGIN)
    reason = _skipped_reason({"host": "${AUTH_CLIENT_SECRET}"})
    assert SECRET not in reason, f"the resolved value leaked into the caller's reason: {reason}"
    assert "RuntimeError" in reason, f"the failure CLASS must survive: {reason}"


def test_a_failure_reason_keeps_the_callers_own_literals():
    reason = _skipped_reason({"host": "db.customer.example"})
    assert "db.customer.example" in reason, \
        f"a caller's own typo'd host must stay debuggable without a log grep: {reason}"


def main():
    print("Compose operator gate (#423):")
    try:
        test_stranger_cannot_make_the_server_resolve_its_own_env()
        print("  PASS  a stranger cannot make the server resolve its own ${ENV}")
        test_stranger_cannot_smuggle_an_env_ref_through_a_sibling_field()
        print("  PASS  ...through title / description / business_unit / id either")
        test_the_sibling_field_leak_never_reaches_the_catalog()
        print("  PASS  ...and no resolved value reaches /router/catalog")
        test_operator_keeps_env_refs_in_sibling_fields()
        print("  PASS  the operator keeps ${ENV} in sibling fields")
        test_stranger_cannot_slip_an_env_ref_through_a_delegation_block()
        print("  PASS  ...nor through a delegation block")
        test_operator_keeps_env_refs()
        print("  PASS  the operator keeps ${ENV} refs (case-insensitive oid match)")
        test_dev_rig_is_untouched()
        print("  PASS  a non-real-login rig is untouched")
        test_stranger_cannot_index_the_servers_filesystem()
        print("  PASS  folder / csv-files are refused for a non-operator")
        test_a_strangers_own_inline_data_still_composes()
        print("  PASS  a stranger's own inline data still composes")
        test_rebuild_replays_a_stored_operator_manifest_ungated()
        print("  PASS  the workspace rebuild path replays stored manifests ungated")
        test_a_failure_reason_never_echoes_a_resolved_env_value()
        print("  PASS  a skipped store's reason withholds server-resolved values")
        test_a_failure_reason_keeps_the_callers_own_literals()
        print("  PASS  ...and keeps the caller's own literals")
    finally:
        for k in _VARS:
            os.environ.pop(k, None)
    print("\nCOMPOSE OPERATOR GATE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
