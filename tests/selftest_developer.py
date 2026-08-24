"""Self-test: ApiKeyRegistry — create/resolve/list/revoke; sha256-at-rest, show-once,
constant-time, owner-only revoke.

    python3 tests/selftest_developer.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.api.keys import ApiKeyRegistry, ApiKeyRecord  # noqa: E402
from dbsearch.api.auth import AuthError  # noqa: E402


def main():
    reg = ApiKeyRegistry()

    # create returns a record + a full token shown ONCE
    rec, token = reg.create("alice", "ci-pipeline")
    assert isinstance(rec, ApiKeyRecord)
    assert rec.bound_user == "alice" and rec.label == "ci-pipeline"
    assert rec.id.startswith("dbk_live_")
    assert token.startswith(rec.id + ".")
    assert rec.revoked is False and rec.request_count == 0

    # secret is NOT stored in plaintext anywhere on the record
    assert token.split(".", 1)[1] not in repr(rec.__dict__), "plaintext secret leaked onto record"

    # resolve the full token -> bound user, bumps counters
    assert reg.resolve(token) == "alice"
    again = [r for r in reg.list_for("alice") if r.id == rec.id][0]
    assert again.request_count == 1 and again.last_used_at is not None

    # wrong secret -> AuthError
    bad = f"{rec.id}.not-the-secret"
    try:
        reg.resolve(bad); assert False, "expected AuthError on wrong secret"
    except AuthError:
        pass

    # unknown id -> AuthError
    try:
        reg.resolve("dbk_live_deadbeef.whatever"); assert False, "expected AuthError unknown id"
    except AuthError:
        pass

    # list_for scoping: bob sees none of alice's
    assert reg.list_for("bob") == []

    # owner-only revoke: bob cannot revoke alice's key (KeyError = 404 at the route)
    try:
        reg.revoke(rec.id, "bob"); assert False, "expected KeyError on non-owned revoke"
    except KeyError:
        pass

    # revoke by owner, then resolve -> AuthError (revoked), no wrong-vs-revoked distinction
    reg.revoke(rec.id, "alice")
    try:
        reg.resolve(token); assert False, "expected AuthError on revoked key"
    except AuthError:
        pass

    # revoke missing id -> KeyError
    try:
        reg.revoke("dbk_live_missing", "alice"); assert False, "expected KeyError missing"
    except KeyError:
        pass

    print("PASS selftest_developer")


if __name__ == "__main__":
    main()
