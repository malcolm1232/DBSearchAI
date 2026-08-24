"""S3Connector - ACL-aware PULL ingestion of an Amazon S3 bucket (or a prefix within one).

#673, and it rides entirely on ADR 0024: the credential is the caller's OWN vaulted AWS
access keys, redeemed into a short-lived STS triple exactly as the Redshift rail does. A user
who has linked Amazon in the account panel has already done everything this needs.

ACL CONVENTION - AND THE HONEST LIMIT, WHICH IS THE WHOLE DESIGN DECISION.

LAW 2 requires every document to carry an audience, and LAW 3 says a connector that cannot
return ACLs is not shippable. S3 cannot answer "who may read this object?" as a principal
list: access is decided per REQUEST by IAM policies, bucket policies and (legacy) object
ACLs, evaluated together. There is no stored per-object principal set to read back. Deriving
one would mean re-implementing IAM policy evaluation inside DBSearch - precisely the
identity-to-policy mapping ADR 0006 refuses to own, where every mapping bug is a silent
disclosure.

So slice 1 does the one thing that is trivially correct: **every document is ACL'd to the
linking user alone.** They are the only principal, so no trim can ever be wrong. The panel
says so out loud ("visible only to you") - an unspoken narrow ACL would look like a broken
search to a team, which is its own kind of dishonesty (#551's silently-empty-store lesson).

Widening this is an OWNER decision (ACL to a business unit / tenant principal), never an
inference from AWS. That is slice 2. Real IAM-derived ACLs are slice 3 and need their own ADR.

INCREMENTAL + RESUMABLE (LAW 3): `list_changes(cursor)` returns only objects whose
LastModified is newer than the cursor (an ISO-8601 string), and returns the newest timestamp
seen as the next cursor. `external_id` is the object key, which is stable, so re-ingestion
REPLACES rather than duplicates. Pagination is followed to the end - a bucket with more than
1000 objects that silently ingested only the first page would be the worst failure shape
available (a store that looks full and is not).

LAW 7: boto3 imports lazily, inside the client factory.
"""
from __future__ import annotations

import hashlib
import json

from dbsearch.core.models import Document, Principal
from dbsearch.ports.base import ConnectorPort

# Only what the extraction pipeline can actually read. An object we cannot parse is skipped
# at LISTING time rather than downloaded and failed - a bucket of images should cost nothing.
_EXT_MIME = {
    ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json",
}

# A single object big enough to be a parse problem rather than a document. 64MB is well above
# any real policy/report and well below "this crawl will exhaust the worker".
_MAX_BYTES = 64 * 1024 * 1024


def _mime_for(key: str) -> "str | None":
    lowered = key.lower()
    for ext, mime in _EXT_MIME.items():
        if lowered.endswith(ext):
            return mime
    return None


class S3Connector(ConnectorPort):
    """config: bucket (+ optional prefix, region). Credential: the caller's STS triple.

    `owner_principal` is the ONLY audience any document from this connector gets - see the
    module docstring. It is passed in by the factory from the store's own acl, never guessed
    here, so this class cannot widen an audience even by accident.
    """

    def __init__(self, tenant_id: str, bucket: str, owner_principal: str, *,
                 prefix: str = "", region: "str | None" = None,
                 credential: "str | None" = None,
                 client_factory=None) -> None:
        self.tenant_id = tenant_id
        self._bucket = bucket
        self._prefix = prefix or ""
        self._region = region
        self._credential = credential
        self._owner = owner_principal
        self._client_factory = client_factory or self._default_client
        self._client = None

    def _default_client(self):
        import boto3   # lazy optional dep (LAW 7)

        if not self._credential:
            # No delegated credential: fall back to the box's ambient AWS identity, which is
            # the self-host topology (the operator IS the credential owner). On a hosted
            # deployment there is none, and boto3 raises - which is the honest outcome, not a
            # silent read of somebody else's bucket.
            return boto3.client("s3", region_name=self._region)
        c = json.loads(self._credential)
        return boto3.client("s3", region_name=self._region,
                            aws_access_key_id=c["access_key_id"],
                            aws_secret_access_key=c["secret_access_key"],
                            aws_session_token=c.get("session_token"))

    def _s3(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def authenticate(self, config: dict) -> object:
        return self._s3()

    def list_changes(self, cursor: "str | None"):
        """Every readable object under the prefix, newer than `cursor`.

        PAGINATES TO THE END. list_objects_v2 caps at 1000 keys per call and reports
        IsTruncated; stopping at the first page would index a bucket's first 1000 objects and
        report success, and nothing downstream could tell that from a small bucket.
        """
        client = self._s3()
        items: list[dict] = []
        newest = cursor or ""
        token = None
        while True:
            kwargs = {"Bucket": self._bucket}
            if self._prefix:
                kwargs["Prefix"] = self._prefix
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if key.endswith("/"):
                    continue                       # a folder marker, not an object
                mime = _mime_for(key)
                if mime is None:
                    continue                       # not extractable; skip before downloading
                if int(obj.get("Size", 0)) > _MAX_BYTES:
                    continue
                modified = obj["LastModified"]
                stamp = modified.isoformat() if hasattr(modified, "isoformat") else str(modified)
                if stamp > newest:
                    newest = stamp
                if cursor and stamp <= cursor:
                    continue                       # already ingested by a prior crawl
                items.append({
                    "external_id": key,
                    "title": key.rsplit("/", 1)[-1] or key,
                    "uri": f"s3://{self._bucket}/{key}",
                    "key": key,
                    "mime": mime,
                    "modified": stamp,
                })
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
            if not token:
                break
        return items, newest

    def fetch_content(self, item: dict):
        body = self._s3().get_object(Bucket=self._bucket, Key=item["key"])["Body"].read()
        return body, item["mime"]

    def external_ids(self, item: dict) -> list[str]:
        return [item["external_id"]]      # the object key (stable) - ADR 0016 resume

    def fetch_acl(self, item: dict):
        """The linking user, and nobody else. See the module docstring: S3 has no per-object
        principal list to read, so this connector never claims to know one."""
        return [Principal(oid=self._owner, kind="user")]

    def to_documents(self, item: dict):
        data, _ = self.fetch_content(item)
        return [
            Document(
                tenant_id=self.tenant_id,
                source_id="s3",
                external_id=item["external_id"],
                content_ref="",  # set by the runner after storing raw bytes
                acl=self.fetch_acl(item),
                title=item["title"],
                uri=item["uri"],
                content_hash=hashlib.sha256(data).hexdigest(),
                source_meta={"mime": item["mime"], "bucket": self._bucket,
                             "modified": item["modified"]},
            )
        ]
