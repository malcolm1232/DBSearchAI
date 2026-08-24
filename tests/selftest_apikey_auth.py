"""Self-test: resolve_identity routes a `Bearer dbk_...` token through the injected api-key
resolver FIRST (works in dev and prod), and a failing dbk_ token hard-fails — never falls
through to the dev-header path.

    python3 tests/selftest_apikey_auth.py
"""
import os
import sys
from pathlib import Path

os.environ["DBSEARCH_DEV_AUTH"] = "1"   # dev mode on — dbk_ must still take precedence
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.api import auth  # noqa: E402
from dbsearch.api.auth import resolve_identity, set_api_key_resolver, AuthError  # noqa: E402


def hdr(d):
    return lambda n: d.get(n.lower())


def main():
    # a resolver that knows one key
    def fake_resolver(token):
        if token == "dbk_live_aa.secret":
            return "alice"
        raise AuthError("invalid api key")
    set_api_key_resolver(fake_resolver)

    # 1. valid dbk_ token -> bound user, EVEN with a different X-DBSearch-User present
    uid = resolve_identity(hdr({"authorization": "Bearer dbk_live_aa.secret",
                                "x-dbsearch-user": "bob"}))
    assert uid == "alice", f"dbk_ must win over dev header, got {uid}"

    # 2. failing dbk_ token hard-fails (does NOT fall through to the dev header)
    try:
        resolve_identity(hdr({"authorization": "Bearer dbk_live_aa.WRONG",
                              "x-dbsearch-user": "bob"}))
        assert False, "failed dbk_ must raise, not fall through to dev header"
    except AuthError:
        pass

    # 3. no dbk_ token -> existing dev-header path still works (unchanged)
    assert resolve_identity(hdr({"x-dbsearch-user": "bob"})) == "bob"

    # cleanup so other suites importing auth aren't affected
    set_api_key_resolver(None)
    print("PASS selftest_apikey_auth")


if __name__ == "__main__":
    main()
