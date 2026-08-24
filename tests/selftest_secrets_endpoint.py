"""#319 (ADR 0010 s3): POST /secrets - the write-once credential seam.

The plaintext crosses the HTTP boundary exactly once, in the write request body, and no
endpoint ever hands it back - not the write response, not the read-back, not a compose
response, not an answer, not an exception message. Read-back is existence plus a <=4 char
hint. `/secrets` is live-only (current_user): a demo:* identity 403s, an anonymous caller
401s/403s, and one user can never read or delete another user's handle (LAW 5).

This also proves the Task 4 review's two carried findings are actually closed now that
`router_api.py` wires a `ScopedSecretResolver` into compose (#319 Task 5): a foreign secret
handle in a manifest's `config:` block, and one in a `delegation:` block, must each produce
the SAME clean 403 refusal - never a silently-skipped store (`config:`) and never an
unhandled 500 (`delegation:`) - and neither response may contain the handle string.

    PYTHONPATH=src python3 tests/selftest_secrets_endpoint.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
# Hermetic default model (ExtractiveLlm) regardless of the dev machine's env.
for _k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
    os.environ.pop(_k, None)
_SRC = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, _SRC)

from cryptography.fernet import Fernet  # noqa: E402

# Set BEFORE importing the app: EncryptedFileSecrets is constructed lazily at app.py import
# time, so the key and store file must exist in the environment first (mirrors the env
# preamble in tests/selftest_demo_scope_boundary.py).
os.environ["DBSEARCH_SECRET_KEY"] = Fernet.generate_key().decode()
_secret_dir = tempfile.mkdtemp(prefix="dbsearch-secrets-endpoint-")
os.environ["DBSEARCH_SECRET_FILE"] = str(Path(_secret_dir) / "secrets.json")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.router.secret_handles import format_handle  # noqa: E402
from dbsearch.server import app as app_module  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

ALICE = {"X-DBSearch-User": "alice"}
BOB = {"X-DBSearch-User": "bob"}
ALICE_OID = "alice"
TENANT_ID = app_module._edition.tenant_id


def test_a_stored_secret_is_never_returned_by_any_endpoint():
    client = TestClient(app)
    r = client.post("/secrets", json={"store_id": "sales-db", "field": "password",
                                      "value": "hunter2-correct-horse"}, headers=ALICE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "hunter2-correct-horse" not in r.text, f"THE WRITE RESPONSE ECHOED THE SECRET: {r.text}"
    assert body["handle"].startswith("secret://"), body
    assert body.get("hint") == "orse", body

    g = client.get("/secrets/" + body["handle"], headers=ALICE)
    assert g.status_code == 200, g.text
    assert "hunter2-correct-horse" not in g.text, f"THE READ ENDPOINT SERVED THE SECRET: {g.text}"
    assert g.json()["exists"] is True
    print("  PASS  the value is never echoed by the write or the read endpoint")


def test_a_demo_visitor_cannot_write_a_secret():
    client = TestClient(app)
    r = client.post("/secrets", json={"store_id": "s", "field": "password", "value": "x"},
                    headers={"X-DBSearch-Demo-User": "alice"})
    assert r.status_code == 403, f"a demo identity must not reach the secret store: {r.status_code}"
    print("  PASS  a demo identity is refused (live-only surface, #279 default-deny)")


def test_an_anonymous_caller_cannot_write_a_secret():
    client = TestClient(app)
    r = client.post("/secrets", json={"store_id": "s", "field": "password", "value": "x"})
    assert r.status_code in (401, 403), r.status_code
    print("  PASS  an unauthenticated caller is refused")


def test_one_user_cannot_read_or_delete_another_users_handle():
    client = TestClient(app)
    h = client.post("/secrets", json={"store_id": "sales-db", "field": "password",
                                      "value": "hunter2"}, headers=ALICE).json()["handle"]
    g = client.get("/secrets/" + h, headers=BOB)
    assert g.status_code == 403, f"bob read alice's handle: {g.status_code} {g.text}"
    d = client.delete("/secrets/" + h, headers=BOB)
    assert d.status_code == 403, f"bob deleted alice's handle: {d.status_code}"
    assert client.get("/secrets/" + h, headers=ALICE).json()["exists"] is True
    print("  PASS  handles are owner-scoped for read and delete (LAW 5)")


def test_the_handle_the_endpoint_mints_actually_composes_and_queries():
    """The end-to-end property #319 exists for: store a credential, reference it from a
    manifest, and have the store build. Uses a csv store so no external database is needed -
    what is proven is that the HANDLE resolves during compose, which is the new machinery."""
    client = TestClient(app)
    h = client.post("/secrets", json={"store_id": "fx", "field": "note", "value": "topsecret"},
                    headers=ALICE).json()["handle"]
    manifest = {"tenant": "acme", "stores": [{
        "id": "fx", "kind": "csv", "business_unit": "sales", "acl": [ALICE_OID],
        "title": "Fixture", "description": "region amounts",
        "config": {"note": h, "tables": {"sales": {"columns": ["region", "amount"],
                                                   "rows": [["emea", 100]]}}}}]}
    c = client.post("/router/compose", json={"manifest": manifest}, headers=ALICE)
    assert c.status_code == 200, c.text
    assert "topsecret" not in c.text, f"COMPOSE ECHOED THE SECRET: {c.text}"
    a = client.post("/router/ask", json={"question": "total amount by region"}, headers=ALICE)
    assert a.status_code == 200, a.text
    assert "topsecret" not in a.text, "an ANSWER echoed the secret"
    print("  PASS  a minted handle resolves during compose and never appears in a response")


def test_a_foreign_handle_in_config_is_a_403_not_a_silent_skip():
    """Task 4 review finding (a): a cross-tenant/cross-owner secret handle probed from a
    manifest's `config:` block raises PermissionError inside `load_manifest`. Before this
    task that landed indistinguishable from any other build/probe failure - silently
    downgraded to `skipped` with a 200 response. It must now propagate to a clean 403, and
    the response must never contain the handle it tried."""
    client = TestClient(app)
    foreign = format_handle(TENANT_ID, "mallory-not-alice", "victim-store", "password")
    manifest = {"tenant": "acme-config-probe", "stores": [{
        "id": "fx-config-probe", "kind": "csv", "business_unit": "sales", "acl": [ALICE_OID],
        "title": "Fixture", "description": "region amounts",
        "config": {"note": foreign, "tables": {"sales": {"columns": ["region", "amount"],
                                                          "rows": [["emea", 100]]}}}}]}
    r = client.post("/router/compose", json={"manifest": manifest}, headers=ALICE)
    assert r.status_code == 403, (
        f"a foreign handle in config: must 403, not silently skip (got {r.status_code}): {r.text}")
    assert foreign not in r.text, f"THE REFUSAL ECHOED THE FOREIGN HANDLE: {r.text}"
    assert "mallory" not in r.text, f"THE REFUSAL LEAKED THE FOREIGN OWNER: {r.text}"
    print("  PASS  a foreign handle in config: is a clean 403, not a silently-skipped store")


def test_a_foreign_handle_in_delegation_is_a_403_not_a_500():
    """Task 4 review finding (b): `register_delegations` only ever caught ValueError, and its
    caller (`_compose_manifest`) only caught (KeyError, ValueError) - a PermissionError from a
    foreign handle in a `delegation:` block would have escaped as an unhandled 500. Must be the
    SAME clean 403 as the config: case, and the response must never contain the handle."""
    client = TestClient(app)
    foreign = format_handle(TENANT_ID, "mallory-not-alice", "victim-vault", "token")
    manifest = {"tenant": "acme-delegation-probe", "stores": [{
        "id": "fx-delegation-probe", "kind": "csv", "business_unit": "sales", "acl": [ALICE_OID],
        "title": "Fixture", "description": "region amounts",
        "config": {"tables": {"sales": {"columns": ["region", "amount"],
                                        "rows": [["emea", 100]]}}},
        "delegation": {"kind": "static", "token": foreign}}]}
    r = client.post("/router/compose", json={"manifest": manifest}, headers=ALICE)
    assert r.status_code == 403, (
        f"a foreign handle in delegation: must 403, not 500 (got {r.status_code}): {r.text}")
    assert foreign not in r.text, f"THE REFUSAL ECHOED THE FOREIGN HANDLE: {r.text}"
    assert "mallory" not in r.text, f"THE REFUSAL LEAKED THE FOREIGN OWNER: {r.text}"
    print("  PASS  a foreign handle in delegation: is a clean 403, not an unhandled 500")


def test_probe_and_health_resolve_a_stored_handle_no_unresolved_error():
    """C4 (review finding, 260727): this is the motivating bug ADR 0010 opens by quoting -
    'Cannot check ... could not prepare check: "manifest references unset env var ..."' -
    reproduced for the self-serve path: /router/probe and /router/health did not thread a
    ScopedSecretResolver, so a manifest entry referencing a handle the caller had JUST
    stored still hit resolve_env's no-resolver-wired refusal. Store a handle, reference it
    from a probe/health entry, and confirm the button a user presses first ("Test
    connection") now actually resolves it instead of reporting an unresolvable reference."""
    client = TestClient(app)
    h = client.post("/secrets", json={"store_id": "probe-store", "field": "note",
                                      "value": "probe-secret-value"}, headers=ALICE).json()["handle"]

    probe_entry = {"id": "probe-secret-x", "kind": "local", "business_unit": "hr",
                   "title": "Probe Secret", "config": {"note": h}}
    pr = client.post("/router/probe", json={"entry": probe_entry}, headers=ALICE)
    assert pr.status_code == 200, pr.text
    pbody = pr.json()
    assert pbody.get("available") is True, (
        f"probe did not resolve the caller's own stored handle: {pbody}")
    assert "no secret resolver" not in pr.text and "unset env var" not in pr.text, pr.text

    health_entry = {
        "id": "health-secret-x", "kind": "local", "business_unit": "hr",
        "title": "Handbook", "description": "parental leave",
        "config": {"note": h,
                   "seed": [{"external_id": "d", "title": "Handbook", "uri": "u",
                             "acl": ["all-staff"],
                             "text": "handbook parental leave holidays"}],
                   "user_groups": {"alice": ["all-staff"]}}}
    hr = client.post("/router/health", json={"entry": health_entry}, headers=ALICE)
    assert hr.status_code == 200, hr.text
    hbody = hr.json()
    assert hbody.get("status") == "healthy", (
        f"health did not resolve the caller's own stored handle: {hbody}")
    assert "no secret resolver" not in hr.text and "unset env var" not in hr.text, hr.text
    print("  PASS  /router/probe and /router/health resolve a caller's own stored handle "
          "(the ADR 0010 motivating bug, on the self-serve write path)")


def test_a_foreign_handle_to_probe_and_health_is_a_clean_403():
    """Same Task 5 policy as compose (config: and delegation:), now also on /probe and
    /health: a cross-owner secret handle must be a clean 403, never a 500 and never a
    silent available=False/failed downgrade that looks just like an ordinary bad-credential
    result - and the refusal must never contain the handle or the foreign owner."""
    client = TestClient(app)
    foreign = format_handle(TENANT_ID, "mallory-not-alice", "victim-store", "password")

    probe_entry = {"id": "probe-foreign-x", "kind": "local", "business_unit": "hr",
                   "title": "Probe Foreign", "config": {"note": foreign}}
    pr = client.post("/router/probe", json={"entry": probe_entry}, headers=ALICE)
    assert pr.status_code == 403, (
        f"a foreign handle to /probe must 403, not silently fail-available (got "
        f"{pr.status_code}): {pr.text}")
    assert foreign not in pr.text, f"THE PROBE REFUSAL ECHOED THE FOREIGN HANDLE: {pr.text}"
    assert "mallory" not in pr.text, f"THE PROBE REFUSAL LEAKED THE FOREIGN OWNER: {pr.text}"

    health_entry = {"id": "health-foreign-x", "kind": "local", "business_unit": "hr",
                    "title": "Health Foreign", "config": {"note": foreign}}
    hr = client.post("/router/health", json={"entry": health_entry}, headers=ALICE)
    assert hr.status_code == 403, (
        f"a foreign handle to /health must 403, not a failed verdict (got "
        f"{hr.status_code}): {hr.text}")
    assert foreign not in hr.text, f"THE HEALTH REFUSAL ECHOED THE FOREIGN HANDLE: {hr.text}"
    assert "mallory" not in hr.text, f"THE HEALTH REFUSAL LEAKED THE FOREIGN OWNER: {hr.text}"
    print("  PASS  a foreign handle to /router/probe and /router/health is the same clean "
          "403 as compose - never a silent fail-open, never a 500")


def test_missing_secret_key_503s_without_crashing_the_whole_server():
    """A deployment with DBSEARCH_SECRET_KEY unset must still boot - every OTHER feature
    works, and only /secrets is unavailable (503, actionable message), never the whole
    process refusing to start. Run in a subprocess: the module-level app in THIS process
    already constructed its EncryptedFileSecrets from the env set at the top of this file,
    and app.py builds it once at import time - a fresh process is the only clean way to
    exercise the "key never set" boot path."""
    script = f"""
import os, sys
os.environ.pop("DBSEARCH_SECRET_KEY", None)
os.environ["DBSEARCH_SECRET_FILE"] = "/tmp/dbsearch-secrets-endpoint-should-not-exist.json"
os.environ["SELFHOST_BACKEND"] = "memory"
for k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
    os.environ.pop(k, None)
sys.path.insert(0, {_SRC!r})
from fastapi.testclient import TestClient
from dbsearch.server.app import app
client = TestClient(app)
h = client.get("/health")
assert h.status_code == 200, ("the whole server must still boot", h.status_code, h.text)
r = client.post("/secrets", json={{"store_id": "s", "field": "password", "value": "x"}},
                headers={{"X-DBSearch-User": "alice"}})
assert r.status_code == 503, ("no key configured -> 503, not a crash", r.status_code, r.text)
assert "DBSEARCH_SECRET_KEY" in r.text, r.text
print("  PASS  subprocess: no DBSEARCH_SECRET_KEY -> /secrets 503s, the rest of the app boots fine")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    print(result.stdout.strip())


def main():
    print("POST /secrets write-once credential seam (#319 Task 5) self-test:")
    test_a_stored_secret_is_never_returned_by_any_endpoint()
    test_a_demo_visitor_cannot_write_a_secret()
    test_an_anonymous_caller_cannot_write_a_secret()
    test_one_user_cannot_read_or_delete_another_users_handle()
    test_the_handle_the_endpoint_mints_actually_composes_and_queries()
    test_a_foreign_handle_in_config_is_a_403_not_a_silent_skip()
    test_a_foreign_handle_in_delegation_is_a_403_not_a_500()
    test_probe_and_health_resolve_a_stored_handle_no_unresolved_error()
    test_a_foreign_handle_to_probe_and_health_is_a_clean_403()
    test_missing_secret_key_503s_without_crashing_the_whole_server()
    print("\nPOST /secrets SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
