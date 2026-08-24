"""GraphSharePointConnector — the REAL SharePoint connector over Microsoft Graph.

Requires: pip install azure-identity requests
Implements ConnectorPort (LAW 3): isolated, incremental (delta), permission-aware.

Scope for Phase 1b: one document library (drive). It enumerates items via the drive
`delta` feed, downloads content, and reads per-item permissions to build the ACL.

⚠ ACL-fidelity caveats (hardened in Phase 3, see PERMISSIONS.md):
  - Graph permissions expose `grantedToV2`/`grantedToIdentitiesV2` with user/group
    identities; we map their `id` to principal oids. SharePoint-group and
    sharing-link semantics, external/guest shares, and unique vs inherited
    permissions need fuller handling before GA. Unknown/unmappable grants are
    treated as DENY (default-deny) — never broadened.
  - Requires Graph application permissions: Sites.Read.All, Files.Read.All,
    GroupMember.Read.All (see docs/DEPLOY_AZURE.md).
"""
from __future__ import annotations

import hashlib

from dbsearch.core.models import Document, Principal
from dbsearch.ports.base import ConnectorPort

_GRAPH = "https://graph.microsoft.com/v1.0"


class GraphSharePointConnector(ConnectorPort):
    def __init__(self, tenant_id: str, drive_id: str, az_tenant_id: str, client_id: str,
                 client_secret: str, folder_path: str = "", name_contains: str = "",
                 owner_oid: "str | None" = None) -> None:
        from azure.identity import ClientSecretCredential

        self.tenant_id = tenant_id              # OUR opaque tenant id (data residency key)
        self.owner_oid = owner_oid              # ADR 0012: attribution only
        self._drive_id = drive_id
        self._folder = folder_path.strip("/")   # optional: scope the crawl to one library folder
        self._name_contains = name_contains.lower()  # optional: only files whose name contains this
        self._cred = ClientSecretCredential(az_tenant_id, client_id, client_secret)

    def _token(self) -> str:
        return self._cred.get_token("https://graph.microsoft.com/.default").token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    def authenticate(self, config: dict) -> object:
        self._token()  # fail fast if creds/scopes are wrong
        return self._cred

    def list_changes(self, cursor: str | None) -> tuple[list[dict], str | None]:
        """Incremental crawl via the drive delta feed. `cursor` is the deltaLink.
        Returns (items, next_cursor) where items are file driveItems."""
        import requests

        url = cursor or f"{_GRAPH}/drives/{self._drive_id}/root/delta"
        items: list[dict] = []
        next_link = None
        while url:
            resp = requests.get(url, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            body = resp.json()
            # #910: deletion tombstones (the `deleted` facet) ride the same feed and MUST be
            # kept. They carry no `file` facet and often no parentReference, so neither the
            # file filter nor the folder scope can be applied to them - and neither needs to
            # be: the runner deletes by external id, which is a safe no-op for anything this
            # index never held.
            items.extend(it for it in body.get("value", [])
                         if it.get("deleted")
                         or (it.get("file") and self._in_scope(it)
                             and (not self._name_contains
                                  or self._name_contains in it.get("name", "").lower())))
            next_link = body.get("@odata.deltaLink")
            url = body.get("@odata.nextLink")
        return items, next_link

    def _in_scope(self, item: dict) -> bool:
        """When folder_path is set, keep only items inside that library folder (or its
        subfolders) — matched on the driveItem's parentReference path segment, so 'Books'
        includes 'Books/Sub' but never the sibling 'Books2'."""
        if not self._folder:
            return True
        path = item.get("parentReference", {}).get("path", "")
        after = path.split("root:", 1)[-1]      # e.g. '/Books' or '/Books/Sub'
        return after == f"/{self._folder}" or after.startswith(f"/{self._folder}/")

    def fetch_content(self, item: dict) -> tuple[bytes, str]:
        import requests

        item_id = item["id"]
        resp = requests.get(
            f"{_GRAPH}/drives/{self._drive_id}/items/{item_id}/content",
            headers=self._headers(), timeout=60,
        )
        resp.raise_for_status()
        mime = (item.get("file", {}) or {}).get("mimeType", "application/octet-stream")
        return resp.content, mime

    def external_ids(self, item: dict) -> list[str]:
        """Straight off the delta item — the whole point (ADR 0016): the default would route
        through to_documents -> fetch_acl, one Graph call per document a resume is about to
        skip."""
        return [item["id"]]

    def deletions(self, item: dict) -> list[str]:
        """#910: a delta tombstone deletes its own drive-item id; anything else deletes
        nothing. The id is the same one `to_documents` stamped as external_id, so the
        runner's delete lands on exactly the chunks the original ingest wrote."""
        return [item["id"]] if item.get("deleted") else []

    def fetch_acl(self, item: dict) -> list[Principal]:
        import requests

        resp = requests.get(
            f"{_GRAPH}/drives/{self._drive_id}/items/{item['id']}/permissions",
            headers=self._headers(), timeout=30,
        )
        resp.raise_for_status()
        principals: list[Principal] = []
        for perm in resp.json().get("value", []):
            grantees = perm.get("grantedToIdentitiesV2", [])
            if perm.get("grantedToV2"):
                grantees = [perm["grantedToV2"], *grantees]
            for g in grantees:
                if g.get("group", {}).get("id"):
                    principals.append(Principal(oid=g["group"]["id"], kind="group"))
                elif g.get("user", {}).get("id"):
                    principals.append(Principal(oid=g["user"]["id"], kind="user"))
                # else: unmappable grant (e.g. anonymous sharing link) -> DENY by omission
        # De-dup while preserving order.
        seen, unique = set(), []
        for p in principals:
            if p.oid not in seen:
                seen.add(p.oid)
                unique.append(p)
        return unique

    def to_documents(self, item: dict) -> list[Document]:
        content_hash = (item.get("file", {}) or {}).get("hashes", {}).get("quickXorHash") or item.get("eTag", "")
        return [
            Document(
                tenant_id=self.tenant_id,
                source_id=f"sharepoint:{self._drive_id}",
                external_id=item["id"],
                content_ref="",  # set by the runner after storing raw bytes
                acl=self.fetch_acl(item),
                title=item.get("name", ""),
                uri=item.get("webUrl", ""),
                content_hash=content_hash,
                source_meta={"mime": (item.get("file", {}) or {}).get("mimeType", "application/octet-stream")},
                owner_oid=self.owner_oid,
            )
        ]
