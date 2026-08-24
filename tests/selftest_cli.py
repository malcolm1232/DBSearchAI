"""E9 (card #107) — the `dbsearch` CLI: one command from stores.yml to a composed,
queryable federation, over the /router API of a running data-plane server.

The transport is injectable, so these tests drive the REAL FastAPI app through
TestClient — the CLI's full loop (compose up → ask → sync → catalog) runs against
live routing with zero network. ${ENV} secrets in the manifest pass through
UNRESOLVED — they resolve server-side, in-tenant (LAW 1).

Run: python3 tests/selftest_cli.py
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
os.environ.pop("USERS_FILE", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.cli import main as cli_main  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)


def _transport(method, path, payload, user):
    fn = client.post if method == "POST" else client.get
    kwargs = {"headers": {"X-DBSearch-User": user}}
    if payload is not None:
        kwargs["json"] = payload
    r = fn(path, **kwargs)
    return r.status_code, r.json()


def _run(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli_main(argv, transport=_transport)
    return code, out.getvalue()


MANIFEST_YAML = """
tenant: acme-cli
stores:
  - id: hr-wiki
    kind: local
    business_unit: hr
    acl: [all-staff]
    title: HR Wiki
    description: human resources parental leave holidays
    config:
      seed:
        - {external_id: hb, title: Handbook, uri: u1, acl: [all-staff],
           text: parental leave is sixteen weeks}
      user_groups: {alice: [all-staff], bob: [all-staff]}
  - id: sales-bq
    kind: bigquery
    mode: pushdown
    business_unit: sales
    acl: [all-staff]
    config: {}
"""


def _manifest_file(suffix=".yml", text=MANIFEST_YAML):
    f = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False)
    f.write(text)
    f.close()
    return f.name


def test_compose_up_from_yaml():
    code, out = _run(["compose", "up", "-f", _manifest_file()])
    assert code == 0, out
    assert "hr-wiki" in out and "✓" in out, out
    # honest: the credential-gated cloud kind is SKIPPED with a reason, not faked
    assert "sales-bq" in out and "skipped" in out.lower(), out


def test_ask_and_pin():
    code, out = _run(["ask", "what is our parental leave policy", "--user", "bob"])
    assert code == 0 and "sixteen weeks" in out, out
    code, out = _run(["ask", "anything", "--user", "bob", "--store", "hr-wiki"])
    assert code == 0 and "manual" in out, out


def test_catalog_and_sync_404():
    code, out = _run(["catalog", "--user", "bob"])
    assert code == 0 and "hr-wiki" in out, out
    code, out = _run(["sync", "hr-wiki"])          # local kind: no connector rail
    assert code == 1 and "404" in out, out


def test_json_manifest_without_yaml_dep():
    spec = {"tenant": "acme-json",
            "stores": [{"id": "hr-wiki", "kind": "local", "business_unit": "hr",
                        "acl": ["all-staff"],
                        "config": {"seed": [], "user_groups": {}}}]}
    path = _manifest_file(".json", json.dumps(spec))
    code, out = _run(["compose", "up", "-f", path])
    assert code == 0 and "hr-wiki" in out, out


def test_env_placeholders_pass_through_unresolved():
    # LAW 1: the CLI must NOT resolve ${SECRET} client-side — the server does, in-tenant.
    calls = []

    def spy(method, path, payload, user):
        calls.append((method, path, payload))
        return 200, {"tenant": "t", "stores": [], "skipped": []}

    spec = {"tenant": "t", "stores": [{"id": "x", "kind": "local", "business_unit": "b",
                                       "acl": [], "config": {"path": "${SECRET_PATH}"}}]}
    path = _manifest_file(".json", json.dumps(spec))
    code, _ = cli_main(["compose", "up", "-f", path], transport=spy), None
    sent = calls[0][2]["manifest"]["stores"][0]["config"]["path"]
    assert sent == "${SECRET_PATH}", sent


def test_http_error_is_exit_1():
    code, out = _run(["ask", ""])                  # empty question -> server 4xx/no route
    # composing hasn't been torn down; an empty question still routes — use a bad path
    code, out = _run(["sync", "definitely-not-a-store"])
    assert code == 1, out


def main():
    print("E9 CLI self-test:")
    test_compose_up_from_yaml()
    print("  PASS  compose up from stores.yml (+ honest skip of credential-gated kind)")
    test_ask_and_pin()
    test_catalog_and_sync_404()
    print("  PASS  ask / manual pin / catalog / sync 404 -> exit 1")
    test_json_manifest_without_yaml_dep()
    test_env_placeholders_pass_through_unresolved()
    test_http_error_is_exit_1()
    print("  PASS  json manifest / ${ENV} passes through unresolved (LAW 1) / error exit")
    print("\nE9 CLI SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
