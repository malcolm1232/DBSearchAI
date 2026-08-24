"""SharePoint connector (the wedge source — consulting firms live here).

PHASE 1: seed-backed. The interface (ConnectorPort) is real and final; the backend
is a fixed in-memory dataset standing in for Microsoft Graph so the spine runs end
to end without an Azure tenant. PHASE 3 replaces `_SEED`/`list_changes` with real
Graph delta queries + drive-item ACLs — NOTHING downstream changes (LAW 3/7).

Each seed doc carries a real ACL (allowed Entra group oids), because content without
an ACL is unsafe to index (LAW 2).
"""
from __future__ import annotations

import hashlib

from dbsearch.core.models import Document, Principal
from dbsearch.ports.base import ConnectorPort

# Sample consulting-firm corpus with deliberately different permissions.
_SEED = [
    {
        "external_id": "prop-retail-bank-2025",
        "title": "Retail Bank Digital Transformation — Proposal 2025",
        "uri": "https://contoso.sharepoint.com/proposals/retail-bank-2025",
        "acl": ["grp-all-consultants"],
        "text": (
            "Proposal for a retail bank digital transformation programme. Scope: mobile "
            "banking redesign, core banking cloud migration, customer onboarding journey, "
            "and a data platform. Prior relevant engagements and credentials included."
        ),
    },
    {
        "external_id": "falcon-ma-confidential",
        "title": "Project Falcon — Confidential M&A Engagement",
        "uri": "https://contoso.sharepoint.com/falcon/engagement-brief",
        "acl": ["grp-falcon-team"],  # NOT visible to all consultants — Chinese wall
        "text": (
            "Project Falcon confidential. Merger and acquisition advisory for a retail bank "
            "acquirer. Target valuation, due diligence findings, and deal structure are "
            "restricted to the Falcon deal team only."
        ),
    },
    {
        "external_id": "healthcare-sap-case",
        "title": "Healthcare SAP Migration — Case Study",
        "uri": "https://contoso.sharepoint.com/cases/healthcare-sap",
        "acl": ["grp-all-consultants"],
        "text": (
            "Case study: SAP ERP migration for a healthcare provider. Implementation "
            "approach, cutover plan, and outcomes for a hospital network."
        ),
    },
]


class SharePointConnector(ConnectorPort):
    def __init__(self, tenant_id: str, seed: list[dict] | None = None,
                 owner_oid: "str | None" = None) -> None:
        self.tenant_id = tenant_id
        self.owner_oid = owner_oid
        self._seed = seed if seed is not None else _SEED

    def authenticate(self, config: dict) -> object:
        # Phase 3: acquire a Graph token via the tenant's least-privilege app registration.
        return object()

    def list_changes(self, cursor: str | None) -> tuple[list[dict], str | None]:
        # Phase 1: full crawl of the seed. Phase 3: Graph delta query keyed off `cursor`.
        return list(self._seed), None

    def fetch_content(self, item: dict) -> tuple[bytes, str]:
        return item["text"].encode("utf-8"), "text/plain"

    def external_ids(self, item: dict) -> list[str]:
        return [item["external_id"]]

    def fetch_acl(self, item: dict) -> list[Principal]:
        return [Principal(oid=g, kind="group") for g in item["acl"]]

    def to_documents(self, item: dict) -> list[Document]:
        content_hash = hashlib.md5(item["text"].encode()).hexdigest()
        return [
            Document(
                tenant_id=self.tenant_id,
                source_id="sharepoint",
                external_id=item["external_id"],
                content_ref="",  # set by the ingestion runner after storing raw bytes
                acl=self.fetch_acl(item),
                title=item["title"],
                uri=item["uri"],
                content_hash=content_hash,
                source_meta={"mime": "text/plain"},
                owner_oid=self.owner_oid,
                # #842: metered ONLY when the caller says so. This connector serves two very
                # different callers: a real SharePoint crawl, whose content is the customer's
                # and is never metered (ADR 0027 rules 1/3 - it is never held), and the
                # single-item seed `Edition.ingest_document` builds for POST /ingest, whose
                # text the account has ENTRUSTED to us exactly like an upload. The seed item
                # carries the size; a crawl item has no such key and stays 0.
                doc_bytes=int(item.get("doc_bytes") or 0),
            )
        ]
