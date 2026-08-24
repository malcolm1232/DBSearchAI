"""#319 (ADR 0010 s2): a manifest value may be a literal, ${ENV}, or secret://<handle>.

The three are ADDITIVE - every manifest that worked before must still work, which is what
makes ADR 0010 reversible. The new form must also fail LOUDLY when no resolver is wired,
because silently leaving "secret://..." in a config would ship that literal string to a
database driver as if it were a password.

    PYTHONPATH=src python3 tests/selftest_secret_manifest_resolution.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.provisioning import resolve_env  # noqa: E402
from dbsearch.router.secret_handles import ScopedSecretResolver, format_handle  # noqa: E402


class FakeSecrets:
    def __init__(self, data):
        self.data = data

    def get_secret(self, name):
        return self.data.get(name, "")


ALICE = "oid-alice"
H = format_handle("acme", ALICE, "sales-db", "password")
RESOLVER = ScopedSecretResolver(FakeSecrets({f"acme/{ALICE}/sales-db/password": "hunter2"}),
                                "acme", ALICE)


def test_literals_and_env_still_resolve_exactly_as_before():
    env = {"HOST_VAR": "db.example.com"}
    got = resolve_env({"host": "${HOST_VAR}", "user": "sa", "tables": ["sales"]}, env)
    assert got == {"host": "db.example.com", "user": "sa", "tables": ["sales"]}, got
    print("  PASS  literal and ${ENV} forms are unchanged (ADR 0010 is additive)")


def test_a_handle_resolves_through_the_scoped_resolver():
    got = resolve_env({"host": "db", "password": H}, {}, secrets=RESOLVER)
    assert got["password"] == "hunter2", got
    print("  PASS  secret:// resolves through the caller-scoped resolver")


def test_a_handle_with_no_resolver_raises_instead_of_passing_the_string_through():
    try:
        resolve_env({"password": H}, {})
    except (KeyError, ValueError, PermissionError) as exc:
        assert "secret" in str(exc).lower(), exc
    else:
        raise AssertionError(
            "a secret:// value survived with no resolver - that string would be handed to a "
            "database driver as the password")
    print("  PASS  a handle with no resolver wired is a hard error, never a pass-through")


def test_a_foreign_handle_is_refused_during_manifest_resolution():
    foreign = format_handle("evilcorp", "oid-mallory", "sales-db", "password")
    try:
        resolve_env({"password": foreign}, {}, secrets=RESOLVER)
    except PermissionError:
        pass
    else:
        raise AssertionError("a foreign handle resolved during compose (LAW 5 breach)")
    print("  PASS  a foreign handle is refused at manifest-resolution time")


def test_nested_structures_are_walked():
    got = resolve_env({"a": {"b": [{"password": H}]}}, {}, secrets=RESOLVER)
    assert got["a"]["b"][0]["password"] == "hunter2", got
    print("  PASS  handles resolve at any depth")


def main():
    print("Manifest secret resolution (#319) self-test:")
    test_literals_and_env_still_resolve_exactly_as_before()
    test_a_handle_resolves_through_the_scoped_resolver()
    test_a_handle_with_no_resolver_raises_instead_of_passing_the_string_through()
    test_a_foreign_handle_is_refused_during_manifest_resolution()
    test_nested_structures_are_walked()
    print("\nMANIFEST SECRET RESOLUTION SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
