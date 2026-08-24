"""UploadConnector — a single in-memory file presented to the ingestion pipeline.

One uploaded file = one connector item carrying its raw bytes + mime. Isolates the
upload path from the seed SharePointConnector; downstream chunk/embed/index is identical.
"""
from __future__ import annotations

import hashlib

from dbsearch.core.models import Document, Principal
from dbsearch.ports.base import ConnectorPort


class UploadConnector(ConnectorPort):
    def __init__(self, tenant_id: str, external_id: str, title: str,
                 data: bytes, mime: str, acl: list[str], uri: str = "",
                 owner_oid: "str | None" = None) -> None:
        self.tenant_id = tenant_id
        self.owner_oid = owner_oid
        self._item = {
            "external_id": external_id, "title": title, "uri": uri,
            "data": data, "mime": mime, "acl": acl,
        }

    def authenticate(self, config: dict) -> object:
        return object()

    def list_changes(self, cursor):
        return [self._item], None

    def fetch_content(self, item: dict):
        return item["data"], item["mime"]

    def external_ids(self, item: dict) -> list[str]:
        return [item["external_id"]]

    def fetch_acl(self, item: dict):
        return [Principal(oid=g, kind="group") for g in item["acl"]]

    def to_documents(self, item: dict):
        return [
            Document(
                tenant_id=self.tenant_id,
                source_id="upload",
                external_id=item["external_id"],
                content_ref="",  # set by the runner after storing raw bytes
                acl=self.fetch_acl(item),
                title=item["title"],
                uri=item["uri"],
                content_hash=hashlib.sha256(item["data"]).hexdigest(),
                source_meta={"mime": item["mime"]},
                owner_oid=self.owner_oid,
                # #775: the raw size the user handed us. This is the ONE connector that
                # stores bytes of ours, so it is the one that reports any (ADR 0027 rule 3).
                doc_bytes=len(item["data"]),
            )
        ]
