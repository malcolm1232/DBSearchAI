"""AzureBlobStore — ObjectStorePort backed by Azure Blob Storage (in the tenant).

Requires: pip install azure-storage-blob azure-identity
Auth: DefaultAzureCredential (managed identity in AKS) or an account key.
"""
from __future__ import annotations

from dbsearch.ports.base import ObjectStorePort


class AzureBlobStore(ObjectStorePort):
    def __init__(self, account_url: str, container: str, credential) -> None:
        from azure.storage.blob import BlobServiceClient

        self._svc = BlobServiceClient(account_url=account_url, credential=credential)
        self._container = container

    def put(self, key: str, data: bytes) -> str:
        payload = data if isinstance(data, bytes) else str(data).encode()
        self._svc.get_blob_client(self._container, key).upload_blob(payload, overwrite=True)
        return key

    def get(self, key: str) -> bytes:
        return self._svc.get_blob_client(self._container, key).download_blob().readall()
