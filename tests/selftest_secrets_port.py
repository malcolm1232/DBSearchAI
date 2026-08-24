"""#319 (ADR 0010): the SecretsPort write surface.

A secret is written ONCE and never read back. `describe_secret` exists so a UI can render
"password is set" without the server ever re-serving the value, and it returns a hint of at
most the last four characters - enough to recognise, useless to replay.

    PYTHONPATH=src python3 tests/selftest_secrets_port.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import EnvSecrets  # noqa: E402
from dbsearch.ports.base import SecretsPort  # noqa: E402


def test_port_declares_the_write_surface():
    for name in ("get_secret", "put_secret", "delete_secret", "describe_secret"):
        assert hasattr(SecretsPort, name), f"SecretsPort is missing {name}"
    print("  PASS  SecretsPort declares get/put/delete/describe")


def test_env_secrets_refuses_writes_because_it_is_operator_config():
    """EnvSecrets reads the server's own environment. A self-serve write into it would be
    either a no-op the caller believes worked, or an attempt to mutate the process env -
    both worse than an honest refusal."""
    s = EnvSecrets()
    for call in (lambda: s.put_secret("x", "y"), lambda: s.delete_secret("x")):
        try:
            call()
        except NotImplementedError as exc:
            assert "operator" in str(exc).lower() or "read-only" in str(exc).lower(), \
                f"refusal must say WHY: {exc}"
        else:
            raise AssertionError("EnvSecrets must refuse writes, not silently accept them")
    assert s.describe_secret("PATH") is not None, "describe must work for a set env var"
    assert s.describe_secret("DEFINITELY_NOT_SET_XYZ") is None
    print("  PASS  EnvSecrets refuses writes with an actionable reason, still describes")


def main():
    print("SecretsPort write surface (#319) self-test:")
    test_port_declares_the_write_surface()
    test_env_secrets_refuses_writes_because_it_is_operator_config()
    print("\nSECRETS PORT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
