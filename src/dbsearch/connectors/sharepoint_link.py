"""SharePointLinkConnector - PULL ingestion of a SharePoint / OneDrive folder shared as
"Anyone with the link", with NO Microsoft identity of any kind (#924).

This is gdrive.py slice 1 for SharePoint, and it is structurally simpler: there is no
deployment credential at all. Measured on a live tenant (260824), the whole mechanism is
three unauthenticated calls:

  1. GET the sharing link, no credential, no redirect-following. SharePoint answers 302 and
     sets a `FedAuth` cookie whose subject is `urn:spo:tenantanon#<tenant>` - an ANONYMOUS
     badge the link mints for itself. The redirect's `id=` query names the server-relative
     path of the shared folder.
  2. Classic REST `/_api/web/GetFolderByServerRelativeUrl('<path>')/Files` (and `/Folders`)
     honours that cookie: Name, ServerRelativeUrl, Length, TimeLastModified, UniqueId, ETag.
  3. `GetFileByServerRelativeUrl('<path>')/$value` returns the bytes.

Not the sp_connect.py model (#148: a multi-tenant Entra app + a tenant ADMIN's consent) and
not the Graph connector (sharepoint_graph.py: app-only Graph with Sites.Read.All). Those
serve tenants whose IT disables anonymous sharing - which is most consulting firms - and
this sits BESIDE them for the users who can mint such a link. It replaces nothing.

THE ROOT COMES FROM THE REDIRECT, NEVER FROM CONFIG. Microsoft security-trims the badge to
the shared subtree: out-of-scope CONTENT 403s, so nothing can leak - but an out-of-scope
CONTAINER lists as 200 with an EMPTY value (measured: the parent library listed exactly the
one shared folder and nothing else). A connector that crawled a typed path would therefore
build a store that synced "successfully" with zero documents and then told the reader the
source holds nothing that fits (#940/#941's dishonesty from a new side). So this class has no
path parameter: the only root it will ever crawl is the one the link's own 302 names.

ACL CONVENTION - THE HONEST LIMIT (gdrive.py / s3.py's argument). An anyone-with-the-link
share has no per-user audience to read, so every document takes the STORE's own audience,
passed in by the factory, never guessed here - the store's acl already gates who may query
it, so this cannot widen anything (#551 / #673). No permission endpoint is ever called.

INCREMENTAL (LAW 3): no changes feed is available to an anonymous caller, so the cursor is
the newest `TimeLastModified` seen, compared as ISO-8601 strings - a tie re-ingests
(REPLACES, stable external_id = the item's UniqueId), never skips.

THE BADGE IS PER CRAWL. It carries an expiry, and a store lives for weeks; a badge minted at
compose and trusted forever would make the first crawl after expiry a silently-partial one.
Every list_changes mints afresh, and a 403 on a fetch is re-tried ONCE with a fresh badge
before it is believed to be a genuine per-file refusal (#767's discipline: a transient
failure must fail the crawl, because the cursor is already past the item).

LAW 7: `requests` imports lazily, inside the transport factory.
"""
from __future__ import annotations

import logging
import re
import urllib.parse

from dbsearch.core.models import Document, Principal
from dbsearch.ports.base import ConnectorPort, ItemUnreadable

_log = logging.getLogger("dbsearch.ingest")

# Types the extraction pipeline can read - the same table as s3.py / app.py _EXT_MIME, keyed
# on extension because SharePoint serves every $value as application/octet-stream. Anything
# else is skipped at LISTING time, so a folder of images costs nothing.
_EXT_MIME = {
    ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json",
}

_MAX_BYTES = 64 * 1024 * 1024      # same stance as s3.py / gdrive.py: above this is a parse problem

# A SharePoint sharing link: https://<tenant>[-my].sharepoint.com/:<kind>:/<g|s|...>/...
# `:f:` is a folder. `:b:` (binary/pdf), `:w:` (Word), `:x:` (Excel), `:p:` (PowerPoint) are
# single-file shares - refused by NAME below, because the reader's fix is "share the folder".
_SHARE_RE = re.compile(r"^(https://[a-z0-9-]+\.sharepoint\.com)/:([a-z]):/", re.I)
_FILE_KINDS = {"b": "a PDF or other file", "w": "a Word document", "x": "an Excel workbook",
               "p": "a PowerPoint deck", "o": "a OneNote notebook", "v": "a video",
               "i": "an image", "t": "a text file", "u": "a file"}

# The two shapes a folder link's redirect takes, measured / documented:
#   a library:   https://host[/sites/x]/<Library>/Forms/AllItems.aspx?id=<path>&...
#   OneDrive:    https://host-my/personal/<user>/_layouts/15/onedrive.aspx?id=<path>&...
# The web (the REST base) is everything before `/_layouts/` or before `/<Library>/Forms/`.
_LAYOUTS_RE = re.compile(r"^(?P<web>.*?)/_layouts/", re.I)
_FORMS_RE = re.compile(r"^(?P<web>.*?)/[^/]+/Forms/[^/]+\.aspx$", re.I)


def _parse_share_link(link: "str | None") -> tuple[str, str]:
    """A sharing link -> (origin, link). Refuses, with a sentence the reader can act on.

    #918's rule: EMPTY is not MALFORMED. A node whose link field has never been filled must
    be told so, not told that what it typed does not look like a link."""
    s = (link or "").strip()
    if not s:
        raise ValueError(
            "no sharing link yet - open this source and paste the folder's share link "
            "(in SharePoint or OneDrive: Share -> Anyone with the link -> Copy link)")
    m = _SHARE_RE.match(s)
    if not m:
        raise ValueError(
            "that does not look like a SharePoint sharing link - it should start with "
            "https://<your-tenant>.sharepoint.com/:f:/ (Share -> Anyone with the link -> "
            "Copy link, on a FOLDER)")
    origin, kind = m.group(1), m.group(2).lower()
    if kind != "f":
        what = _FILE_KINDS.get(kind, "a file")
        raise ValueError(
            f"that link shares a single file ({what}); this source reads a folder - in "
            "SharePoint, share the folder that holds it instead (Share -> Anyone with the "
            "link -> Copy link)")
    return origin, s


def _mime_for(name: str) -> "str | None":
    lowered = name.lower()
    for ext, mime in _EXT_MIME.items():
        if lowered.endswith(ext):
            return mime
    return None


def _rest_path(path: str) -> str:
    """A server-relative path as it goes INSIDE GetFolderByServerRelativeUrl('...'): percent-
    encoded (spaces, non-ASCII), slashes kept, and the OData string-literal escape for a
    quote (`'` -> `''`) - a folder named "Q3 '26" is a real folder."""
    return urllib.parse.quote(path, safe="/").replace("'", "''")


class SharePointLinkConnector(ConnectorPort):
    """config: a folder sharing link (+ the STORE's acl). No credential of any kind. `acl` is
    the ONLY audience any document gets - passed in by the factory from the store's own acl,
    never guessed here (see the module docstring)."""

    def __init__(self, tenant_id: str, link: str, acl: list, *, http_factory=None) -> None:
        self.tenant_id = tenant_id
        self._origin, self._link = _parse_share_link(link)
        self._acl = list(acl)
        self._http_factory = http_factory or self._default_http
        self._http = None
        self._badge: "str | None" = None    # the FedAuth cookie value for the CURRENT crawl
        self._web = ""                       # server-relative web the folder lives in
        self._root = ""                      # server-relative path of the shared folder

    def _default_http(self):
        # The MODULE, not a Session, deliberately: a Session's jar would absorb the badge at
        # mint time and re-send it merged with the explicit Cookie header below, so the
        # request's cookie would no longer be the one this class chose - and after a re-mint
        # the jar could carry the stale one. Every cookie this connector sends is explicit.
        import requests   # lazy optional dep (LAW 7)
        return requests

    def _session(self):
        if self._http is None:
            self._http = self._http_factory()
        return self._http

    # ------------------------------------------------------------------------ the badge
    def _mint(self) -> None:
        """Step 1 of the measured mechanism. GET the link WITHOUT following the redirect: the
        302 is the payload - its Set-Cookie is the badge and its Location names the folder.

        No badge means the link is not (or no longer) anonymous: revoked, expired (`?e=`),
        or scoped to 'People in <org>' - all of which redirect to a Microsoft sign-in. That
        must raise, never fall through to a crawl: a crawl with no badge would 403 (measured)
        or, worse, list an empty container as 200 and build a store that synced 'successfully'
        with nothing in it."""
        resp = self._session().get(self._link, allow_redirects=False, timeout=30)
        badge = None
        try:
            badge = resp.cookies.get("FedAuth")
        except Exception:
            badge = None
        location = ""
        try:
            location = resp.headers.get("Location", "") or ""
        except Exception:
            location = ""
        if not badge or resp.status_code not in (301, 302, 303, 307, 308):
            raise RuntimeError(
                "this link isn't shared as 'Anyone with the link' - or it was revoked or has "
                "expired - so SharePoint asks for a sign-in instead of opening it. In "
                "SharePoint: Share -> Anyone with the link -> Copy link, on the folder")
        parsed = urllib.parse.urlparse(location)
        root = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
        path = urllib.parse.unquote(parsed.path)
        m = _LAYOUTS_RE.match(path) or _FORMS_RE.match(path)
        if not root or m is None:
            raise RuntimeError(
                "the link opened, but SharePoint's redirect did not name a folder - this "
                "connector reads a folder shared as 'Anyone with the link'; a link to a "
                "single file, a list, or a whole site cannot be crawled this way")
        self._badge = badge
        self._web = m.group("web").rstrip("/")
        self._root = root

    def _api(self) -> str:
        return f"{self._origin}{self._web}/_api/web"

    def _get(self, url: str):
        """Every REST request goes through here, so one can never go out without the badge
        by omission (measured: no cookie -> 403 on every _api route)."""
        if self._badge is None:
            self._mint()
        return self._session().get(
            url, headers={"Cookie": f"FedAuth={self._badge}",
                          "Accept": "application/json;odata=nometadata"},
            allow_redirects=False, timeout=30)

    def authenticate(self, config: dict) -> object:
        """Fail fast at Test-connection with the sentence that helps: `_parse_share_link`
        already refused an empty or single-file link at construction, so what is left to
        learn here is whether the link is genuinely anonymous."""
        self._mint()
        return self._session()

    # ----------------------------------------------------------------------- the listing
    def _rows(self, folder: str, tail: str, select: str) -> list[dict]:
        """One folder's Files or Folders, PAGED TO THE END: the real API caps a page at 100
        rows and continues via `odata.nextLink` - stopping at the first page is a store that
        looks full and is not. Any non-200 fails the crawl LOUDLY (a half-listed folder that
        reports success is the worst available failure shape)."""
        url = f"{self._api()}/GetFolderByServerRelativeUrl('{_rest_path(folder)}')/{tail}?$select={select}"
        rows: list[dict] = []
        while url:
            resp = self._get(url)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"SharePoint listing failed ({resp.status_code}) for folder {folder!r}")
            body = resp.json()
            rows.extend(body.get("value", []))
            url = body.get("odata.nextLink") or body.get("@odata.nextLink") or ""
        return rows

    def list_changes(self, cursor: "str | None"):
        """Breadth-first over the shared folder. Mints its OWN badge (see the module
        docstring), recurses to the bottom, filters at listing time, never fetches."""
        self._mint()
        items: list[dict] = []
        newest = cursor or ""
        queue = [self._root]
        while queue:
            folder = queue.pop(0)
            for f in self._rows(folder, "Folders", "Name,ServerRelativeUrl"):
                # `Forms` is the library's own system folder (views, templates); its content
                # 403s for an anonymous badge anyway (measured) and is never a document.
                if f.get("Name") == "Forms":
                    continue
                queue.append(f.get("ServerRelativeUrl") or f"{folder}/{f.get('Name', '')}")
            for f in self._rows(folder, "Files",
                                "Name,ServerRelativeUrl,Length,TimeLastModified,UniqueId,ETag"):
                name = f.get("Name", "")
                mime = _mime_for(name)
                if mime is None:
                    continue                       # not extractable; never fetched
                size = int(f.get("Length") or 0)
                if size > _MAX_BYTES:
                    # Dropped HERE, not raised as ItemUnreadable: never fetched, so it never
                    # reaches the runner's per-item path where the unreadable counter lives.
                    # A reader expects this document in the store though (unlike an image),
                    # so the drop must not be silent.
                    _log.warning(
                        "sharepoint_link: dropping %r at listing - %d bytes exceeds the %d "
                        "byte cap and will never be fetched", name, size, _MAX_BYTES)
                    continue
                stamp = f.get("TimeLastModified", "")
                if stamp > newest:
                    newest = stamp
                # Strict `<`, not `<=`: a TIE with the cursor must RE-INGEST, never skip.
                # external_id is stable, so a tie costs one idempotent re-ingest - skipping
                # it would make the document invisible to every future crawl forever, since
                # the cursor never regresses below this value.
                if cursor and stamp < cursor:
                    continue                       # already ingested by a prior crawl
                path = f.get("ServerRelativeUrl") or f"{folder}/{name}"
                items.append({
                    "external_id": f.get("UniqueId") or path,
                    "title": name,
                    "uri": self._origin + urllib.parse.quote(path, safe="/"),
                    "path": path,
                    "mime": mime,
                    "modified": stamp,
                    "etag": f.get("ETag", ""),
                })
        return items, newest

    def external_ids(self, item: dict) -> list[str]:
        return [item["external_id"]]      # stable UniqueId - ADR 0016 resume, replaces on re-ingest

    # ----------------------------------------------------------------------- the content
    def fetch_content(self, item: dict):
        """Step 3. A per-file refusal raises ItemUnreadable so the runner skips-and-COUNTS it
        (LAW 3, #551); anything that might resolve on retry must fail the WHOLE crawl instead,
        because list_changes already advanced the cursor past this item (gdrive.py's
        `_raise_for_fetch_failure`, same reasoning, same asymmetry: a wrongly-failed crawl
        just retries, a wrongly-skipped document is gone).

        A 403 here has two causes with opposite remedies - the badge expired mid-crawl
        (transient) or this one file carries its own permissions the folder link never
        covered (permanent). So a 403 is re-tried ONCE with a fresh badge; only a 403 that
        survives that is believed."""
        path, mime, title = item["path"], item["mime"], item.get("title", "")
        url = f"{self._api()}/GetFileByServerRelativeUrl('{_rest_path(path)}')/$value"
        resp = self._get(url)
        if resp.status_code == 403:
            self._mint()
            resp = self._get(url)
            if resp.status_code == 403:
                raise ItemUnreadable(
                    f"download refused (403) for {title!r} even with a freshly re-minted badge "
                    "- this file carries its own permissions that the folder's link does not "
                    "cover (an individually-restricted file)")
        if resp.status_code == 404:
            raise ItemUnreadable(
                f"download refused (404) for {title!r} - deleted at the source since listing")
        if resp.status_code != 200:
            raise RuntimeError(
                f"SharePoint download failed ({resp.status_code}) for {title!r} - failing the "
                "crawl (not counting it unreadable) so the cursor is not advanced past this "
                "item and it is retried on the next incremental crawl")
        return resp.content, mime

    def fetch_acl(self, item: dict):
        """The store's audience, and nobody else - see the module docstring. No permission
        endpoint is ever called; the selftest pins that."""
        return [Principal(oid=oid, kind="user") for oid in self._acl]

    def to_documents(self, item: dict):
        # No fetch here, deliberately: the hash comes off the LISTING (ETag, else the stamp)
        # - otherwise every crawl re-ingests every document. The runner fetches separately.
        content_hash = item.get("etag") or item["modified"]
        return [
            Document(
                tenant_id=self.tenant_id,
                source_id="sharepoint_link",
                external_id=item["external_id"],
                content_ref="",  # set by the runner after storing raw bytes
                acl=self.fetch_acl(item),
                title=item["title"],
                uri=item["uri"],
                content_hash=content_hash,
                source_meta={"mime": item["mime"], "modified": item["modified"]},
            )
        ]
