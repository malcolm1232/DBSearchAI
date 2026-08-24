"""#319 (ADR 0010): KeyVaultSecrets must honour the same absent-secret contract as
EncryptedFileSecrets - get_secret returns "" for a name that has never been stored, not a
raw SDK exception.

Without this, ScopedSecretResolver.resolve's friendly `KeyError("no stored credential for
X.Y - store it first, then compose")` never fires under Key Vault: the caller instead gets
a raw azure.core.exceptions.ResourceNotFoundError whose traceback contains the internal
hashed secret name. A genuine failure (auth, network, permissions) must still raise - a
missing secret and a broken vault are different problems and must not look the same.

    PYTHONPATH=src python3 tests/selftest_keyvault_adapter.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.azure.keyvault import KeyVaultSecrets  # noqa: E402

try:
    from azure.core.exceptions import ResourceNotFoundError as _NotFoundError
except ImportError:
    print("SKIP  azure-core is not installed; selftest_keyvault_adapter needs it to "
          "construct a real ResourceNotFoundError (pip install azure-core)")
    sys.exit(0)


class _FakeAuthError(Exception):
    """Stands in for an auth/permissions/network failure - anything that is NOT a missing
    secret, and must propagate rather than being swallowed."""


class _FakeSecretValue:
    def __init__(self, value):
        self.value = value


class StubSecretClient:
    """Stands in for azure.keyvault.secrets.SecretClient. KeyVaultSecrets.get_secret runs
    the real _kv_name() transform before calling the client, so this stub is keyed on the
    already-transformed kv_name exactly as the real client would see it."""

    def __init__(self, data_by_kv_name: dict, raise_not_found_cls, raise_auth_on_kv_name=None):
        self._data = data_by_kv_name
        self._not_found_cls = raise_not_found_cls
        self._raise_auth_on = raise_auth_on_kv_name

    def get_secret(self, kv_name):
        if kv_name == self._raise_auth_on:
            raise _FakeAuthError("401 Client Error: access denied")
        if kv_name in self._data:
            return _FakeSecretValue(self._data[kv_name])
        raise self._not_found_cls("A secret with (name/id) was not found in this key vault.")


def _make_adapter(stub_client):
    adapter = KeyVaultSecrets.__new__(KeyVaultSecrets)
    adapter._client = stub_client
    return adapter


def test_missing_secret_returns_empty_string_not_a_raw_sdk_exception():
    client = StubSecretClient(data_by_kv_name={}, raise_not_found_cls=_NotFoundError)
    adapter = _make_adapter(client)
    assert adapter.get_secret("acme/oid-alice/sales-db/password") == "", \
        "a missing secret must come back as '', matching EncryptedFileSecrets"
    print("  PASS  a missing secret returns '' instead of raising the SDK's raw exception")


def test_present_secret_returns_its_value():
    name = "acme/oid-alice/sales-db/password"
    kv_name = KeyVaultSecrets._kv_name(name)
    client = StubSecretClient(
        data_by_kv_name={kv_name: "hunter2"}, raise_not_found_cls=_NotFoundError)
    adapter = _make_adapter(client)
    assert adapter.get_secret(name) == "hunter2"
    print("  PASS  a present secret still returns its real value")


def test_an_auth_style_error_still_raises_rather_than_being_swallowed():
    """Only the not-found case is caught. Silently returning '' for an auth failure would
    look identical to a missing credential and send the caller chasing the wrong bug."""
    name = "locked/oid-alice/sales-db/password"
    kv_name = KeyVaultSecrets._kv_name(name)
    client = StubSecretClient(
        data_by_kv_name={}, raise_not_found_cls=_NotFoundError,
        raise_auth_on_kv_name=kv_name)
    adapter = _make_adapter(client)
    try:
        adapter.get_secret(name)
    except _FakeAuthError:
        pass
    else:
        raise AssertionError("an auth-style failure must propagate, not resolve to ''")
    print("  PASS  a non-not-found error (auth/network/permissions) still raises")


def main():
    print("KeyVaultSecrets missing-secret contract (#319) self-test:")
    test_missing_secret_returns_empty_string_not_a_raw_sdk_exception()
    test_present_secret_returns_its_value()
    test_an_auth_style_error_still_raises_rather_than_being_swallowed()
    print("\nKEYVAULT ADAPTER SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
