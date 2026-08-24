"""GDriveConnector - ACL-aware PULL ingestion of a PUBLIC Google Drive folder (#712).

Slice 1 reads "Anyone with the link" content with a deployment API KEY - no user OAuth, no
consent screen, no Picker, no CASA. An API key is a project-attribution/quota token, not an
identity: it cannot read any private Drive, so its blast radius if leaked is quota, never
data. It still lives server-side only (LAW 1).

ACL CONVENTION - THE HONEST LIMIT (same argument as s3.py, see the spec).

A folder shared as "Anyone with the link" has no per-user audience to read: permissions.list
returns a single `type: anyone` entry, which maps to no principal in this product. So every
document takes the STORE's own audience, passed in by the factory, never guessed here - the
store's acl already gates who may query it, so this cannot widen anything (#551 / #673).
Slice 1 does NOT call permissions.list at all; the selftest pins that.

Slice 2 (private Drives) enters through `credential` - the caller's Google access token from
the existing GoogleRefreshExchange (resource: "drive") - and has FOUR named entry conditions
in the spec (email-as-principal ADR, Google-Groups stance, OAuth client to Production, CASA
if hosted). Do not start it casually.

INCREMENTAL (LAW 3): the changes feed is user-scoped and unavailable to an API key, so the
cursor is the newest `modifiedTime` seen, compared as ISO-8601 strings - a tie re-ingests
(REPLACES, stable external_id = file id), never skips.

LAW 7: `requests` imports lazily, inside the transport factory.
"""
from __future__ import annotations

import logging
import re

from dbsearch.core.models import Document, Principal
from dbsearch.ports.base import ConnectorPort, ItemUnreadable

_log = logging.getLogger("dbsearch.ingest")

_DRIVE = "https://www.googleapis.com/drive/v3"
_FOLDER_MIME = "application/vnd.google-apps.folder"

# Native Google types have no downloadable bytes: files.get?alt=media 403s on them, and
# files.export converts. (mimeType -> (export mimeType, effective mime for the extractor)).
_EXPORT = {
    "application/vnd.google-apps.document": ("text/plain", "text/plain"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", "text/csv"),
    "application/vnd.google-apps.presentation": ("text/plain", "text/plain"),
}

# Binary types the extraction pipeline can read - skipped at LISTING time otherwise, so a
# folder of images costs nothing (same rule as s3.py's _EXT_MIME, keyed on Drive mimeType).
_BINARY_OK = {
    "application/pdf", "text/plain", "text/markdown", "text/csv", "application/json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_MAX_BYTES = 64 * 1024 * 1024      # same stance as s3.py: above this is a parse problem
_EXPORT_CAP = 10 * 1024 * 1024     # files.export hard limit; skip loudly, never silently

_LINK_RE = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def _folder_id_from_link(link: str) -> str:
    """A Drive FOLDER link (or a bare folder id) -> the folder id.

    Deliberately a regex, not a resolve round-trip: unlike a SharePoint sharing link
    (sp_connect.py, a Graph call), a Drive folder link carries its id in the path. File and
    Docs links are refused - this connector crawls folders."""
    s = (link or "").strip()
    m = _LINK_RE.search(s)
    if m:
        return m.group(1)
    if _BARE_ID_RE.match(s):   # anchored ^...$ already excludes "/" and "."
        return s
    # #918: EMPTY is not MALFORMED, and telling the two apart is the difference between a
    # message that helps and one that accuses. Measured on the owner's live canvas: gdrive-1
    # had `link: ""` - the field had never been filled - and the node's red note read "that
    # does not look like a Drive folder link", which blames the reader for input they never
    # gave. A node that has simply not been configured yet must say SO; that is this card's
    # whole standard ("the canvas tells no lies").
    if not s:
        raise ValueError(
            "no folder link yet - open this source and paste the folder's share link "
            "(in Drive: Share -> General access -> 'Anyone with the link', then Copy link)")
    raise ValueError(
        "that does not look like a Drive folder link - expected "
        "https://drive.google.com/drive/folders/<id> (share the folder as "
        "'Anyone with the link' and paste the link from the Share dialog)")


class GDriveConnector(ConnectorPort):
    """config: a public folder id (+ the STORE's acl). Slice 1 credential: a deployment
    API key. `acl` is the ONLY audience any document gets - passed in by the factory from
    the store's own acl, never guessed here (see the module docstring)."""

    def __init__(self, tenant_id: str, folder_id: str, acl: list, *,
                 api_key: str = "", credential: "str | None" = None,
                 http_factory=None) -> None:
        self.tenant_id = tenant_id
        self._folder_id = folder_id
        self._acl = list(acl)
        self._api_key = api_key
        self._credential = credential
        self._http_factory = http_factory or self._default_http
        self._http = None

    def _default_http(self):
        import requests   # lazy optional dep (LAW 7)
        return requests.Session()

    def _session(self):
        if self._http is None:
            self._http = self._http_factory()
        return self._http

    def _get(self, url: str, params: dict):
        """Every Drive request goes through here: auth + shared-drive support in ONE place.
        Bearer credential (slice 2) wins over the API key; both stamped here so a request
        can never go out unauthenticated by omission."""
        params = dict(params)
        params["supportsAllDrives"] = "true"
        headers = {}
        if self._credential:
            headers["Authorization"] = f"Bearer {self._credential}"
        elif self._api_key:
            params["key"] = self._api_key
        return self._session().get(url, params=params, headers=headers, timeout=30)

    def authenticate(self, config: dict) -> object:
        """Fail fast, and say the useful thing.

        With neither an api_key nor a credential, `_get` would send the request with no
        auth at all - Drive would 403 it, and the folder-sharing message below would send
        the operator to fix the USER's Drive sharing for what is actually a missing
        deployment config (GOOGLE_API_KEY unset). Name that case explicitly, before making
        any request, so the two failure modes are never conflated.

        Once a key/credential IS present, a 403/404 on a folder the user just pasted means
        it is not shared as 'Anyone with the link' (or the id is wrong)."""
        if not self._api_key and not self._credential:
            raise RuntimeError(
                "GOOGLE_API_KEY is not set for this deployment - a gdrive store needs it "
                "to read a public folder (see .env.example); this is missing deployment "
                "config, not a problem with the folder's sharing")
        resp = self._get(f"{_DRIVE}/files/{self._folder_id}", {"fields": "id,name"})
        if resp.status_code != 200:
            raise RuntimeError(
                "this folder isn't shared as 'Anyone with the link' (or the link is "
                "wrong) - in Drive: Share -> General access -> Anyone with the link")
        return self._session()

    def list_changes(self, cursor: "str | None"):
        """Breadth-first over the folder tree. PAGINATES TO THE END and RECURSES to the
        bottom - stopping early is a store that looks full and is not (s3.py's words).
        Filtering happens HERE, at listing time: a folder of images costs nothing."""
        items: list[dict] = []
        newest = cursor or ""
        queue = [self._folder_id]
        while queue:
            fid = queue.pop(0)
            token = None
            while True:
                params = {
                    "q": f"'{fid}' in parents and trashed=false",
                    "fields": ("nextPageToken,files(id,name,mimeType,modifiedTime,"
                               "size,md5Checksum,version,webViewLink)"),
                    "pageSize": "1000",
                    "includeItemsFromAllDrives": "true",
                }
                if token:
                    params["pageToken"] = token
                resp = self._get(f"{_DRIVE}/files", params)
                if resp.status_code != 200:
                    # A quota 403 (or any non-200) must fail the crawl LOUDLY rather than
                    # return a partial result that looks like a complete one - a half-listed
                    # folder that reports success is the worst available failure shape.
                    raise RuntimeError(
                        f"Drive listing failed ({resp.status_code}) for folder {fid}")
                body = resp.json()
                for f in body.get("files", []):
                    mime = f.get("mimeType", "")
                    if mime == _FOLDER_MIME:
                        queue.append(f["id"])
                        continue
                    if mime not in _EXPORT and mime not in _BINARY_OK:
                        continue                       # not extractable; never fetched
                    size = int(f.get("size") or 0)
                    if size > _MAX_BYTES:
                        # Dropped HERE, not raised as ItemUnreadable: this file is never
                        # fetched, so it never reaches the runner's per-item path where the
                        # unreadable counter lives (unlike an export-cap refusal, which is
                        # discovered per-file at fetch time and IS counted). An oversized PDF
                        # is a document the user expects in the store though, unlike the mime
                        # filter just above (a folder of images is legitimately free) - so
                        # the drop must not be silent, even without a counter for it.
                        _log.warning(
                            "gdrive: dropping %r at listing - %d bytes exceeds the %d byte "
                            "cap and will never be fetched", f.get("name", "?"), size,
                            _MAX_BYTES)
                        continue
                    stamp = f.get("modifiedTime", "")
                    if stamp > newest:
                        newest = stamp
                    # Strict `<`, not `<=`: a TIE with the cursor must RE-INGEST, never
                    # skip. external_id is the stable file id, so a tie costs one
                    # idempotent re-ingest - skipping it would make the document
                    # invisible to every future crawl forever, since the cursor never
                    # regresses below this value (module docstring: "a tie re-ingests
                    # ... never skips").
                    if cursor and stamp < cursor:
                        continue                       # already ingested by a prior crawl
                    items.append({
                        "external_id": f["id"],
                        "title": f.get("name", ""),
                        "uri": f.get("webViewLink", ""),
                        "mime": mime,
                        "modified": stamp,
                        # no "size" key: the size decision (the _MAX_BYTES check above) is
                        # already made at listing time, and nothing downstream reads it.
                        "md5": f.get("md5Checksum", ""),
                        "version": f.get("version", ""),
                    })
                token = body.get("nextPageToken")
                if not token:
                    break
        return items, newest

    def external_ids(self, item: dict) -> list[str]:
        return [item["external_id"]]      # stable file id - ADR 0016 resume, replaces on re-ingest

    def fetch_content(self, item: dict):
        """Binary files via alt=media; native Google types via files.export (alt=media 403s
        on them - the branch is mandatory, not an optimization). A per-file refusal raises
        ItemUnreadable: the runner skips-and-COUNTS it (LAW 3, #551 - a silent skip is a
        silently-partial store). See `_raise_for_fetch_failure` for why only 403/404 may
        take that path - anything else must fail the whole crawl instead."""
        fid, mime = item["external_id"], item["mime"]
        title = item.get("title", "")
        if mime in _EXPORT:
            export_mime, effective = _EXPORT[mime]
            resp = self._get(f"{_DRIVE}/files/{fid}/export", {"mimeType": export_mime})
            if resp.status_code != 200:
                if resp.status_code == 403 and self._is_export_size_limit(resp):
                    # Google reports an over-cap export as a 403 with
                    # exportSizeLimitExceeded in the BODY, not a distinct status code -
                    # this must be checked before the generic 403/404 classification below,
                    # or a real size problem gets reported as "an individually-restricted
                    # file": an operator checks Drive, finds the doc IS shared publicly to
                    # everyone, and is left with no explanation.
                    raise ItemUnreadable(self._export_cap_message(fid, title))
                self._raise_for_fetch_failure(resp, "export", fid, title)
            if len(resp.content) > _EXPORT_CAP:
                # Belt-and-braces: production takes the 403-body branch above (Drive
                # refuses the request outright once it knows the export is over cap, it
                # does not hand back 200 with an oversized body) - this guard stays for
                # any case where Drive's behaviour differs from the documented shape.
                raise ItemUnreadable(self._export_cap_message(fid, title))
            return resp.content, effective
        resp = self._get(f"{_DRIVE}/files/{fid}", {"alt": "media"})
        if resp.status_code != 200:
            self._raise_for_fetch_failure(resp, "download", fid, title)
        return resp.content, mime

    @staticmethod
    def _export_cap_message(fid: str, title: str) -> str:
        return f"export refused - exceeds the 10MB Drive export cap for file {fid} ({title})"

    @staticmethod
    def _is_export_size_limit(resp) -> bool:
        """Defensive: the body may not be JSON, and even if it is, may not carry the keys
        Google's documented error shape promises - never let inspecting it raise and mask
        the real 403."""
        try:
            body = resp.json()
        except Exception:
            return False
        if not isinstance(body, dict):
            return False
        error = body.get("error")
        if not isinstance(error, dict):
            return False
        if "exportSizeLimitExceeded" in str(error.get("message", "")):
            return True
        for e in error.get("errors") or []:
            if isinstance(e, dict) and e.get("reason") == "exportSizeLimitExceeded":
                return True
        return False

    # Reasons that mean "this one file, this identity, no" - a refusal retrying cannot fix.
    # Deliberately a CLOSED list: the unknown-403 default has to be "fail loudly", because
    # the cost of the two mistakes is not symmetric (#767).
    _PERMISSION_403 = frozenset({
        "insufficientfilepermissions",     # the ordinary "not shared with you"
        "appnotauthorizedtofile",          # this API client may not touch this file
        "domainpolicy",                    # the owning domain forbids the operation
        "cannotdownloadabusivefile",       # flagged content; a retry never changes it
        "filenotdownloadable",             # no byte stream exists to serve
        "notfound",                        # 403-shaped notFound; same as a 404
    })

    @classmethod
    def _is_permission_refusal(cls, resp) -> bool:
        """Does this 403 positively identify itself as a per-file permission problem?

        Reads BOTH `error.errors[].reason` (the real API's shape) and `error.message`,
        because a reason sometimes only survives in the message. Never lets a malformed or
        non-JSON body raise - the anti-automation interstitial is HTML, and an exception
        here would mask the very 403 being classified. Unknown or unreadable => False =>
        the caller fails the crawl, which is the safe direction."""
        try:
            body = resp.json()
        except Exception:
            return False                    # HTML interstitial: unreadable, so not proven
        if not isinstance(body, dict):
            return False
        error = body.get("error")
        if not isinstance(error, dict):
            return False
        found = {str(error.get("message", "")).lower()}
        for e in error.get("errors") or []:
            if isinstance(e, dict):
                found.add(str(e.get("reason", "")).lower())
        return any(r in f for f in found for r in cls._PERMISSION_403)

    def _raise_for_fetch_failure(self, resp, what: str, fid: str, title: str = "") -> None:
        """Classifies a non-200 per-file fetch response. This is NOT the same discipline
        as list_changes' blanket "any non-200 fails the crawl" - a per-file 403/404
        genuinely means "this one file, this identity, no", so it is safe (and required
        by LAW 3) to skip-and-count it via ItemUnreadable.

        Everything else (429 rate limit, 5xx, or any other unexpected status) must NOT
        take that path, for a reason specific to how the cursor works here:
        list_changes computes next_cursor from modifiedTime seen at LISTING time,
        BEFORE any content is fetched - so it has no idea whether this fetch will
        later succeed or fail. If a transient 429/5xx were counted as ItemUnreadable,
        the runner would skip-and-count it same as a real refusal, the cursor would
        still advance past this item's modifiedTime, and the NEXT incremental crawl
        would see stamp < cursor ("already ingested by a prior crawl") and never
        re-list it - permanently and silently dropping a document because of a
        transient failure, while the sync reports success with a count that reads
        like a permissions problem. So anything that might resolve on retry must
        raise here instead, failing the WHOLE crawl loudly - exactly the same
        discipline list_changes already applies to a failed listing call - so the
        cursor is never advanced past this item and it is retried on the next crawl.
        Erring toward failing loudly is deliberate: a wrongly-failed crawl just
        retries; a wrongly-skipped document is gone.

        #767: THE STATUS CODE ALONE CANNOT MAKE THIS DECISION. The paragraph above is
        right about the mechanism and wrong about the set - it names the transient cases
        as "429, 5xx", but Google's rate limiter answers **403**. Drive documents
        userRateLimitExceeded, rateLimitExceeded, dailyLimitExceeded and
        sharingRateLimitExceeded all as 403 and all retryable, and under sustained load
        Drive also serves a bare HTML anti-automation interstitial - 403, no JSON body,
        no reason field. Every one of those was landing on the skip-and-count path, which
        is the precise data-loss this docstring exists to prevent. Seen live, not
        theorised: a public folder whose only file was perfectly readable ingested zero
        documents, reported `ingested@` as if healthy, and answered "the source is there
        and readable - it simply holds nothing that fits."

        So a 403 is now read, not assumed. Only a 403 that positively identifies itself
        as a per-file permission problem may be skipped; an unrecognised one - including
        any non-JSON body - fails the crawl. That default is the asymmetry restated: a
        crawl failed by a throttle retries a minute later, a document skipped by one is
        never listed again. 404 is unconditional, being the one status that genuinely
        cannot improve on retry."""
        if resp.status_code == 404 or (resp.status_code == 403
                                       and self._is_permission_refusal(resp)):
            raise ItemUnreadable(
                f"{what} refused ({resp.status_code}) for file {fid} ({title}) - an "
                "individually-restricted or deleted file")
        if resp.status_code == 403:
            raise RuntimeError(
                f"Drive {what} refused (403) for file {fid} ({title}) with no recognisable "
                "permission reason - treating it as throttling or a transient refusal and "
                "failing the crawl, so the cursor is NOT advanced past this item and it is "
                "retried next crawl. If this file really is restricted, the fix is to teach "
                "_is_permission_refusal that reason, never to widen this branch (#767)")
        raise RuntimeError(
            f"Drive {what} failed ({resp.status_code}) for file {fid} ({title}) - failing "
            "the crawl (not counting it unreadable) so the cursor is not advanced past "
            "this item and it is retried on the next incremental crawl")

    def fetch_acl(self, item: dict):
        """The store's audience, and nobody else - see the module docstring. Slice 1 never
        calls permissions.list; the selftest pins that."""
        return [Principal(oid=oid, kind="user") for oid in self._acl]

    def to_documents(self, item: dict):
        # No fetch here, deliberately: the hash comes off the LISTING (md5Checksum for
        # binaries; native docs have none, so version+modifiedTime - otherwise every crawl
        # re-ingests every Doc). The runner fetches content separately.
        content_hash = item["md5"] or f"{item['version']}:{item['modified']}"
        return [
            Document(
                tenant_id=self.tenant_id,
                source_id="gdrive",
                external_id=item["external_id"],
                content_ref="",  # set by the runner after storing raw bytes
                acl=self.fetch_acl(item),
                title=item["title"],
                uri=item["uri"],
                content_hash=content_hash,
                source_meta={"mime": item["mime"], "modified": item["modified"]},
            )
        ]
