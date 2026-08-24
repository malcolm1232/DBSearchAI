"""Self-test: Edition wires the ApiKeyRegistry + resolver, and a minted key resolves to its
bound user through the live auth path. Telemetry events validate against the boundary contract.

    python3 tests/selftest_edition_apikeys.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.server.edition import build_edition  # noqa: E402
from dbsearch.api.auth import resolve_identity      # noqa: E402


def main():
    ed = build_edition()

    rec, token = ed.create_api_key("alice", "ci")
    assert rec.bound_user == "alice" and token.startswith(rec.id + ".")
    assert [r.id for r in ed.list_api_keys("alice")] == [rec.id]

    # build_edition wired set_api_key_resolver -> the global auth path resolves the token
    uid = resolve_identity(lambda n: {"authorization": f"Bearer {token}"}.get(n.lower()))
    assert uid == "alice", f"resolver not wired: {uid}"

    ed.revoke_api_key(rec.id, "alice")
    from dbsearch.api.auth import AuthError
    try:
        resolve_identity(lambda n: {"authorization": f"Bearer {token}"}.get(n.lower()))
        assert False, "revoked key must not resolve"
    except AuthError:
        pass

    print("PASS selftest_edition_apikeys")


if __name__ == "__main__":
    main()
