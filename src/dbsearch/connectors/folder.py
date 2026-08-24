"""FolderConnector — ACL-aware PULL ingestion of a local directory (a second connector
alongside SharePoint, for on-prem file shares / air-gapped demos).

ACL convention (LAW 2 — every doc MUST carry an ACL): each **immediate sub-directory of the
root is a group**, and every file under it is readable by that group. So:

    root/
      deal-team/   falcon.pdf      -> acl = ["deal-team"]
      all-staff/   handbook.txt    -> acl = ["all-staff"]
      shared/eng/  spec.md         -> acl = ["shared"]   (group = the TOP-level subdir)

Files placed directly in `root/` have no group and are SKIPPED unless `default_acl` is given
(default-deny — never index content whose audience is unknown).

Incremental + resumable (LAW 3): `list_changes(cursor)` returns only files whose mtime is newer
than the cursor (a float-seconds string); re-running with the returned cursor yields nothing new.
Idempotent: external_id is the stable relative path, so re-ingestion truly REPLACES, never
duplicates and never orphans — the runner deletes each doc's prior chunk set before indexing
the fresh one, so a file that shrinks (or moves ACL group) leaves no surplus stale chunks.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from dbsearch.core.models import Document, Principal
from dbsearch.ports.base import ConnectorPort

_EXT_MIME = {
    ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json",
}


class FolderConnector(ConnectorPort):
    def __init__(self, tenant_id: str, root: str, default_acl: list[str] | None = None) -> None:
        self.tenant_id = tenant_id
        self._root = Path(root)
        self._default_acl = default_acl or []

    def authenticate(self, config: dict) -> object:
        return object()

    def _acl_for(self, rel: Path) -> list[str]:
        # group = the top-level sub-directory; files directly in root use default_acl.
        return [rel.parts[0]] if len(rel.parts) > 1 else list(self._default_acl)

    def list_changes(self, cursor: str | None):
        # #815: rglob on a nonexistent root is an EMPTY ITERATOR, so a typo'd path used to
        # read as a healthy empty folder - probe green, exercise "may be empty", nothing
        # anywhere saying the directory is not there. Refuse here, the one home probe, sync
        # and ingest all pass through, and NAME the path so the typo is findable. "May be
        # empty" stays for a directory that exists and is empty, because that one may be.
        if not self._root.is_dir():
            raise FileNotFoundError(
                f"folder path does not exist or is not a directory: {self._root}")
        since = float(cursor) if cursor else None
        items: list[dict] = []
        max_mtime = since or 0.0
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _EXT_MIME:
                continue
            rel = path.relative_to(self._root)
            acl = self._acl_for(rel)
            if not acl:
                continue  # default-deny: unknown audience -> never indexed (LAW 2)
            mtime = path.stat().st_mtime
            max_mtime = max(max_mtime, mtime)
            if since is not None and mtime <= since:
                continue  # already ingested in a prior crawl
            items.append({
                "external_id": rel.as_posix(),
                "title": path.name,
                "uri": path.as_uri(),
                "path": str(path),
                "mime": _EXT_MIME[path.suffix.lower()],
                "acl": acl,
            })
        return items, repr(max_mtime)

    def fetch_content(self, item: dict):
        return Path(item["path"]).read_bytes(), item["mime"]

    def external_ids(self, item: dict) -> list[str]:
        return [item["external_id"]]      # the stable relative path (ADR 0016 resume)

    def fetch_acl(self, item: dict):
        return [Principal(oid=g, kind="group") for g in item["acl"]]

    def to_documents(self, item: dict):
        data = Path(item["path"]).read_bytes()
        return [
            Document(
                tenant_id=self.tenant_id,
                source_id="folder",
                external_id=item["external_id"],
                content_ref="",  # set by the runner after storing raw bytes
                acl=self.fetch_acl(item),
                title=item["title"],
                uri=item["uri"],
                content_hash=hashlib.sha256(data).hexdigest(),
                source_meta={"mime": item["mime"]},
            )
        ]
