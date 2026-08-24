"""KeyVaultSecrets — SecretsPort backed by Azure Key Vault.

Requires: pip install azure-keyvault-secrets azure-identity
"""
from __future__ import annotations

from dbsearch.ports.base import SecretsPort


class KeyVaultSecrets(SecretsPort):
    def __init__(self, vault_url: str, credential) -> None:
        from azure.keyvault.secrets import SecretClient

        self._client = SecretClient(vault_url=vault_url, credential=credential)

    @staticmethod
    def _kv_name(name: str) -> str:
        # Key Vault permits [a-zA-Z0-9-] only; handles are slash-separated (ADR 0010 s5).
        # Substitution alone could collide ("a/b" vs "a-b"), so a short digest of the
        # ORIGINAL handle is appended - the mapping stays one-to-one.
        import hashlib
        import re
        safe = re.sub(r"[^a-zA-Z0-9-]", "-", name)
        digest = hashlib.sha256(name.encode()).hexdigest()[:8]
        return f"{safe[:100]}-{digest}"

    def get_secret(self, name: str) -> str:
        # Imported lazily (matching __init__) so the adapter still imports without the
        # azure extra installed. Only the not-found case is caught: an absent secret must
        # report as "" like EncryptedFileSecrets.get_secret does, so ScopedSecretResolver's
        # friendly KeyError fires instead of a raw SDK traceback leaking the hashed secret
        # name. A genuine failure (auth, network, permissions) is a different problem and
        # must still raise - silently returning "" for those would be a debugging nightmare.
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self._client.get_secret(self._kv_name(name)).value
        except ResourceNotFoundError:
            return ""

    def put_secret(self, name: str, value: str) -> None:
        self._client.set_secret(self._kv_name(name), value)

    def delete_secret(self, name: str) -> None:
        self._client.begin_delete_secret(self._kv_name(name))

    def describe_secret(self, name: str) -> "dict | None":
        try:
            got = self._client.get_secret(self._kv_name(name))
        except Exception:
            return None
        v = got.value or ""
        return {"exists": True, "hint": v[-4:] if len(v) > 4 else ""}
