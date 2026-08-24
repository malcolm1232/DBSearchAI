"""#924 - a SharePoint "Anyone with the link" FOLDER as a document source, with NO Microsoft
identity of any kind.

The owner's words (260824): "the google sharepoint: DOESNT need to connect to msft account
right? i want to same for sharepoint but same as google drive. so anyone that DOESNT sign in
can just drop a link".

What the probe on their own tenant proved, and what these tests pin:
  1. GET the sharing link, unauthenticated -> 302 + a FedAuth cookie whose subject is
     `urn:spo:tenantanon#<tenant>`: an anonymous badge the link mints for itself. The
     redirect's `id=` query is the server-relative path of the shared folder.
  2. Classic REST (`/_api/web/GetFolderByServerRelativeUrl(...)/Files`) honours that cookie:
     a full listing with Name, ServerRelativeUrl, Length, TimeLastModified.
  3. `GetFileByServerRelativeUrl(...)/$value` returns the bytes.
  4. The badge is security-trimmed to the shared subtree by Microsoft: out-of-scope CONTENT
     403s - but an out-of-scope CONTAINER lists as 200 with an empty value. So a connector
     that trusted a typed path would build a store that synced "successfully" with zero
     documents, which is #940/#941's dishonesty arriving from a new side. The crawl root
     therefore comes ONLY from the redirect, never from config.

The design is gdrive.py slice 1 with no deployment credential at all: every document takes
the STORE's own audience (#551/#673), the cursor is newest-TimeLastModified with a tie
re-ingesting (LAW 3), and a per-file refusal is skipped-and-counted only when it is proven
permanent (#767's discipline: a 403 is re-tried with a FRESH badge before it is believed).

    PYTHONPATH=src python3 tests/selftest_924_sharepoint_link.py
"""
import json
import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from dbsearch.connectors.sharepoint_link import (  # noqa: E402
    SharePointLinkConnector, _MAX_BYTES, _parse_share_link)
from dbsearch.ports.base import ItemUnreadable  # noqa: E402

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
CANVAS_TEXT = CANVAS.read_text()
ROUTER_API = (ROOT / "src/dbsearch/server/router_api.py").read_text()
ROUTER_INIT = (ROOT / "src/dbsearch/router/__init__.py").read_text()
ORIGINS = (ROOT / "src/dbsearch/router/origins.py").read_text()
PROBE = ROOT / "tests/canvas_files_and_links_dom_probe.mjs"

ORIGIN = "https://acme.sharepoint.com"
WEB = "/sites/hr"                                   # a SUB-site: the REST base must follow it
LINK = f"{ORIGIN}/:f:/s/hr/EqQ1abcDEFghijklMNOPqrstuvw?e=Rk8GtS"
ROOT_PATH = "/sites/hr/Shared Documents/policies"


class _Resp:
    def __init__(self, status, payload=None, content=b"", headers=None, cookies=None):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.headers = headers or {}
        self.cookies = cookies or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _f(name, path, modified, size=100, uid=None, etag='"1"'):
    return {"Name": name, "ServerRelativeUrl": path, "TimeLastModified": modified,
            "Length": str(size), "UniqueId": uid or ("uid-" + name), "ETag": etag}


TREE = {
    ROOT_PATH: {
        "files": [
            _f("handbook.pdf", ROOT_PATH + "/handbook.pdf", "2026-08-01T00:00:00Z"),
            _f("logo.png", ROOT_PATH + "/logo.png", "2026-08-09T00:00:00Z"),      # unparseable
            _f("notes.txt", ROOT_PATH + "/notes.txt", "2026-08-03T00:00:00Z"),
        ],
        "folders": [
            {"Name": "2026", "ServerRelativeUrl": ROOT_PATH + "/2026"},
            {"Name": "Forms", "ServerRelativeUrl": ROOT_PATH + "/Forms"},           # system
        ],
    },
    ROOT_PATH + "/2026": {
        "files": [_f("deep.docx", ROOT_PATH + "/2026/deep.docx", "2026-08-05T00:00:00Z")],
        "folders": [],
    },
    ROOT_PATH + "/Forms": {"files": [_f("AllItems.aspx", ROOT_PATH + "/Forms/AllItems.aspx",
                                        "2026-08-07T00:00:00Z")], "folders": []},
}
CONTENTS = {
    ROOT_PATH + "/handbook.pdf": b"%PDF-1.7 handbook",
    ROOT_PATH + "/notes.txt": b"plain notes",
    ROOT_PATH + "/2026/deep.docx": b"PK docx",
}

_REST = re.compile(r"^(?P<base>https://[^/]+(?:/[^_][^/]*)*)/_api/web/"
                   r"(?P<fn>GetFolderByServerRelativeUrl|GetFileByServerRelativeUrl)"
                   r"\('(?P<path>(?:[^']|'')*)'\)/(?P<tail>Files|Folders|\$value)(?:\?(?P<q>.*))?$")


class FakeSharePoint:
    """The transport the probe measured, as a fake. Mints a fresh badge on every hit of the
    sharing link (so re-mints are countable), 403s every REST call that carries no badge
    (measured: `no_cookie status=403`), lists and serves only what the tree holds, and
    pages the way the real API does (`odata.nextLink`) when page_size is set."""

    def __init__(self, tree=TREE, contents=CONTENTS, *, login_redirect=False, restricted=(),
                 force_status=None, page_size=None, forbid_second_badge=False):
        self._tree = tree
        self._contents = contents
        self._login_redirect = login_redirect
        self._restricted = set(restricted)
        self._force_status = dict(force_status or {})
        self._page = page_size
        self._forbid_second = forbid_second_badge
        self.mints = 0
        self.calls = []                     # (url, headers)

    def _badge(self):
        return f"badge-{self.mints}"

    def get(self, url, headers=None, allow_redirects=True, timeout=None, params=None):
        headers = dict(headers or {})
        self.calls.append((url, headers))
        if url.startswith(LINK.split("?")[0]):
            assert allow_redirects is False, "the mint must NOT follow the redirect"
            if self._login_redirect:
                return _Resp(302, headers={"Location": "https://login.microsoftonline.com/x"})
            self.mints += 1
            loc = (f"{ORIGIN}{WEB}/Shared%20Documents/Forms/AllItems.aspx?id="
                   f"{urllib.parse.quote(ROOT_PATH, safe='')}&p=true&ga=1")
            return _Resp(302, headers={"Location": loc}, cookies={"FedAuth": self._badge()})
        m = _REST.match(url)
        if not m:
            return _Resp(404, {"error": "no such route"})
        cookie = headers.get("Cookie", "")
        if "FedAuth=" not in cookie:
            return _Resp(403, {"odata.error": {"message": {"value": "Access denied."}}})
        badge = cookie.split("FedAuth=")[1].split(";")[0]
        if self._forbid_second and badge != "badge-1":
            return _Resp(403, {"odata.error": {"message": {"value": "Access denied."}}})
        if m.group("base") != ORIGIN + WEB:
            return _Resp(404, {"odata.error": {"message": {"value": "wrong web"}}})
        path = urllib.parse.unquote(m.group("path")).replace("''", "'")
        tail = m.group("tail")
        if tail == "$value":
            if path in self._force_status:
                forced = self._force_status[path]
                return forced if isinstance(forced, _Resp) else _Resp(forced)
            if path in self._restricted:
                return _Resp(403, {"odata.error": {"message": {"value": "Access denied."}}})
            if path not in self._contents:
                return _Resp(404, {"odata.error": {"message": {"value": "File Not Found."}}})
            return _Resp(200, content=self._contents[path])
        node = self._tree.get(path)
        if node is None:
            return _Resp(404, {"odata.error": {"message": {"value": "File Not Found."}}})
        rows = node["files" if tail == "Files" else "folders"]
        q = urllib.parse.parse_qs(m.group("q") or "")
        start = int(q.get("$skiptoken", ["0"])[0])
        if self._page:
            page = rows[start:start + self._page]
            body = {"value": page}
            if start + self._page < len(rows):
                body["odata.nextLink"] = url.split("&$skiptoken")[0] + f"&$skiptoken={start + self._page}"
            return _Resp(200, body)
        return _Resp(200, {"value": rows})


def _conn(fake=None, link=LINK, acl=("alice-oid",)):
    fake = fake or FakeSharePoint()
    c = SharePointLinkConnector("acme", link, list(acl), http_factory=lambda: fake)
    return c, fake


def _rest_calls(fake):
    return [(u, h) for (u, h) in fake.calls if "/_api/" in u]


# --------------------------------------------------------------------------- the link parser
def test_the_link_parser_accepts_folder_links_and_names_why_it_refuses_others():
    for good in (LINK,
                 "https://acme.sharepoint.com/:f:/g/EqQ1abcDEFghijklMNOPqrstuvwXYZ?e=abc",
                 "https://acme-my.sharepoint.com/:f:/g/personal/x_acme_com/EvJ2?e=1",
                 "  " + LINK + "  "):
        origin, url = _parse_share_link(good)
        assert origin.startswith("https://acme"), (good, origin)
        assert url == good.strip(), (good, url)
    for junk in ("https://drive.google.com/drive/folders/abc", "not a link",
                 "http://acme.sharepoint.com/:f:/g/abc"):          # http, not https
        try:
            _parse_share_link(junk)
            raise AssertionError(f"accepted junk link: {junk!r}")
        except ValueError as e:
            assert "sharepoint.com" in str(e), e
    print("  PASS  the link parser accepts :f: folder links (site, group, OneDrive), refuses junk")


def test_an_EMPTY_link_says_so_instead_of_accusing_the_reader():
    """#918's rule, carried over verbatim: empty and malformed are different situations."""
    for empty in ("", "   ", None):
        try:
            _parse_share_link(empty)
            raise AssertionError("accepted an empty link")
        except ValueError as e:
            assert "no sharing link yet" in str(e), e
            assert "does not look like" not in str(e), (
                "an EMPTY link is being reported as MALFORMED - the #918 defect")
    print("  PASS  an empty link says 'no sharing link yet', never 'does not look like'")


def test_a_single_FILE_link_is_refused_by_name():
    """`:b:` (pdf/binary), `:w:` (Word), `:x:` (Excel), `:p:` (PowerPoint) are single-file
    shares. This connector crawls a folder, and the fix is one the reader can act on - share
    the folder - so the message must say that, not 'malformed'."""
    for kind in ("b", "w", "x", "p"):
        try:
            _parse_share_link(f"https://acme.sharepoint.com/:{kind}:/g/EvJ2abc?e=1")
            raise AssertionError(f"accepted a :{kind}: single-file link")
        except ValueError as e:
            assert "single file" in str(e) and "folder" in str(e), e
    print("  PASS  a single-file link is refused with 'share the folder instead'")


# ------------------------------------------------------------------- the root and the badge
def test_the_crawl_root_comes_from_the_redirect_and_the_rest_base_follows_the_site():
    """The load-bearing rule from the probe. A typed path that is out of scope lists as 200
    with an empty value, so the ONLY root this connector will ever crawl is the one the
    link's own redirect names - and the REST base is that web, not the tenant root, or a
    sub-site's folder 404s from the root web."""
    c, fake = _conn()
    items, cursor = c.list_changes(None)
    listed = {u.split("('")[1].split("')")[0] for (u, _) in _rest_calls(fake)
              if "GetFolderByServerRelativeUrl" in u}
    assert urllib.parse.quote(ROOT_PATH, safe="/") in listed, (
        f"the resolved root {ROOT_PATH!r} was never listed; listed {listed}")
    for (u, _) in _rest_calls(fake):
        assert u.startswith(ORIGIN + WEB + "/_api/web/"), (
            f"a REST call left the link's web: {u}")
    assert not hasattr(c, "_path_from_config"), "no typed path may ever reach the crawl"
    print("  PASS  the crawl root is the redirect's id=, and REST calls stay on the link's web")


def test_a_link_that_is_not_anonymous_fails_LOUDLY_in_authenticate_and_in_listing():
    """Revocation, expiry, or a link scoped to 'People in <org>' all redirect to a Microsoft
    sign-in with NO badge. Both doors must refuse - listing especially: a store whose crawl
    returned [] would sync 'successfully' with zero documents and then tell the reader the
    source holds nothing that fits (#940's sentence), when the truth is that the link died."""
    c, fake = _conn(FakeSharePoint(login_redirect=True))
    for door in (lambda: c.authenticate({}), lambda: c.list_changes(None)):
        try:
            door()
            raise AssertionError("a non-anonymous link was accepted")
        except RuntimeError as e:
            assert "Anyone with the link" in str(e), e
            assert "revoked" in str(e) or "expired" in str(e), e
    assert not _rest_calls(fake), "REST was called without a badge"
    print("  PASS  a link with no anonymous badge fails both authenticate and list_changes")


def test_every_rest_call_carries_the_badge_and_a_crawl_mints_its_own():
    """Measured on prod: `no_cookie status=403`. And a badge carries an expiry, so a store
    that lives for weeks must mint per crawl rather than trust one it minted at compose."""
    c, fake = _conn()
    c.list_changes(None)
    c.list_changes(None)
    for (u, h) in _rest_calls(fake):
        assert "FedAuth=" in h.get("Cookie", ""), f"a REST call went out without the badge: {u}"
    assert fake.mints == 2, f"expected one mint per crawl, got {fake.mints}"
    print("  PASS  every REST call carries FedAuth and each crawl mints its own badge")


# ------------------------------------------------------------------------------ the listing
def test_listing_recurses_skips_the_system_folder_and_filters_at_listing_time():
    c, fake = _conn()
    items, cursor = c.list_changes(None)
    titles = sorted(i["title"] for i in items)
    assert titles == ["deep.docx", "handbook.pdf", "notes.txt"], titles
    assert "logo.png" not in titles, "an unparseable type was listed (a folder of images must cost nothing)"
    assert "AllItems.aspx" not in titles, "the library's Forms/ system folder was crawled"
    assert cursor == "2026-08-05T00:00:00Z", cursor
    fetched = [u for (u, _) in _rest_calls(fake) if "$value" in u]
    assert not fetched, "list_changes fetched content"
    print("  PASS  listing recurses, skips Forms/, filters by extension, never fetches")


def test_listing_follows_odata_nextLink_to_the_end():
    """The real API caps a page at 100 rows and hands back `odata.nextLink`. A crawl that
    stops at the first page is a store that looks full and is not (s3.py's words)."""
    c, fake = _conn(FakeSharePoint(page_size=2))
    items, _ = c.list_changes(None)
    assert sorted(i["title"] for i in items) == ["deep.docx", "handbook.pdf", "notes.txt"], (
        [i["title"] for i in items])
    print("  PASS  listing pages through odata.nextLink")


def test_an_oversized_file_is_dropped_at_listing_with_a_warning_never_fetched():
    import logging
    big = {ROOT_PATH: {"files": [_f("huge.pdf", ROOT_PATH + "/huge.pdf",
                                    "2026-08-01T00:00:00Z", size=_MAX_BYTES + 1)],
                       "folders": []}}
    c, fake = _conn(FakeSharePoint(tree=big))
    records = []
    h = logging.Handler(); h.emit = records.append
    log = logging.getLogger("dbsearch.ingest"); log.addHandler(h); log.setLevel(logging.WARNING)
    try:
        items, _ = c.list_changes(None)
    finally:
        log.removeHandler(h)
    assert items == [], items
    assert any("huge.pdf" in r.getMessage() for r in records), "the drop was silent"
    print("  PASS  an oversized file is dropped loudly at listing time")


def test_the_cursor_is_strict_so_a_tie_re_ingests_never_skips():
    """gdrive.py's rule for the same reason: there is no changes feed for an anonymous
    caller, so the cursor is the newest stamp seen. A tie skipped would make that document
    invisible to every future crawl - the cursor never regresses."""
    c, _ = _conn()
    items, _ = c.list_changes("2026-08-03T00:00:00Z")
    titles = sorted(i["title"] for i in items)
    assert "notes.txt" in titles, f"a document stamped EQUAL to the cursor was skipped: {titles}"
    assert "handbook.pdf" not in titles, f"an older document was re-listed: {titles}"
    assert "deep.docx" in titles, titles
    print("  PASS  cursor is strict: a tie re-ingests, older is skipped")


def test_a_failed_listing_fails_the_crawl_rather_than_returning_a_partial_store():
    class Flaky(FakeSharePoint):
        def get(self, url, **kw):
            r = super().get(url, **kw)
            if "/2026')/Files" in urllib.parse.unquote(url):
                return _Resp(500, {"odata.error": {"message": {"value": "boom"}}})
            return r
    c, _ = _conn(Flaky())
    try:
        c.list_changes(None)
        raise AssertionError("a 500 on a sub-folder listing returned a partial crawl")
    except RuntimeError as e:
        assert "500" in str(e), e
    print("  PASS  a failed sub-folder listing fails the whole crawl loudly")


# ------------------------------------------------------------------------------ the content
def test_fetch_content_returns_bytes_and_a_mime_from_the_extension():
    c, _ = _conn()
    items, _ = c.list_changes(None)
    by = {i["title"]: i for i in items}
    raw, mime = c.fetch_content(by["handbook.pdf"])
    assert raw == b"%PDF-1.7 handbook" and mime == "application/pdf", (raw, mime)
    raw, mime = c.fetch_content(by["deep.docx"])
    assert mime.endswith("wordprocessingml.document"), mime
    print("  PASS  fetch_content returns the bytes and an extension-derived mime")


def test_a_404_is_unreadable_but_a_429_or_5xx_fails_the_crawl():
    """#767's asymmetry. The cursor advanced at LISTING time, so a transient failure that
    took the skip-and-count path would never be re-listed: permanent, silent data loss.
    Only a failure that cannot improve on retry may be counted unreadable."""
    p = ROOT_PATH + "/handbook.pdf"
    c, _ = _conn(FakeSharePoint(force_status={p: 404}))
    items, _ = c.list_changes(None)
    item = next(i for i in items if i["title"] == "handbook.pdf")
    try:
        c.fetch_content(item)
        raise AssertionError("a 404 was not reported")
    except ItemUnreadable:
        pass
    for status in (429, 500, 502, 503):
        c, _ = _conn(FakeSharePoint(force_status={p: status}))
        items, _ = c.list_changes(None)
        item = next(i for i in items if i["title"] == "handbook.pdf")
        try:
            c.fetch_content(item)
            raise AssertionError(f"a {status} returned content")
        except ItemUnreadable:
            raise AssertionError(
                f"a {status} was counted unreadable - the cursor is already past this item, "
                "so it would never be re-listed")
        except RuntimeError as e:
            assert str(status) in str(e), e
    print("  PASS  404 -> ItemUnreadable; 429/5xx -> the crawl fails and the item is retried")


def test_a_403_on_fetch_is_retried_with_a_FRESH_badge_before_it_is_believed():
    """A 403 mid-crawl has two causes with opposite remedies: the badge expired (transient -
    mint again) or this one file carries its own permissions the folder link never covered
    (permanent - skip and count). Believing the first 403 turns an expired badge into a
    silently-partial store; never believing it hangs the crawl. So: one fresh badge, then
    the verdict."""
    p = ROOT_PATH + "/handbook.pdf"
    # Case 1: the first badge is dead for fetches, a fresh one works -> content, not a skip.
    class ExpiredOnce(FakeSharePoint):
        def get(self, url, headers=None, **kw):
            cookie = (headers or {}).get("Cookie", "")
            if url.endswith("$value") and "badge-1" in cookie:
                self.calls.append((url, dict(headers or {})))
                return _Resp(403, {"odata.error": {"message": {"value": "Access denied."}}})
            return super().get(url, headers=headers, **kw)
    c, fake = _conn(ExpiredOnce())
    items, _ = c.list_changes(None)
    item = next(i for i in items if i["title"] == "handbook.pdf")
    raw, _ = c.fetch_content(item)
    assert raw == b"%PDF-1.7 handbook", raw
    assert fake.mints == 2, f"a 403 must re-mint exactly once before retrying; mints={fake.mints}"
    # Case 2: still 403 with a fresh badge -> a genuine per-file refusal, counted, not fatal.
    c, fake = _conn(FakeSharePoint(restricted={p}))
    items, _ = c.list_changes(None)
    item = next(i for i in items if i["title"] == "handbook.pdf")
    try:
        c.fetch_content(item)
        raise AssertionError("a persistent 403 returned content")
    except ItemUnreadable as e:
        assert "fresh" in str(e).lower() or "re-minted" in str(e).lower(), e
    assert fake.mints == 2, f"the persistent 403 was not re-tried with a fresh badge; mints={fake.mints}"
    print("  PASS  a 403 re-mints once: expired badge -> content; still 403 -> unreadable")


# --------------------------------------------------------------------------------- the ACL
def test_the_acl_is_the_stores_audience_and_no_permission_endpoint_is_ever_called():
    c, fake = _conn(acl=("alice-oid", "bob-oid"))
    items, _ = c.list_changes(None)
    for i in items:
        c.fetch_content(i)
        docs = c.to_documents(i)
        assert len(docs) == 1
        assert sorted(p.oid for p in docs[0].acl) == ["alice-oid", "bob-oid"], docs[0].acl
        assert docs[0].source_id == "sharepoint_link", docs[0].source_id
        assert docs[0].content_hash, "no content hash - every crawl would re-ingest every doc"
        assert c.external_ids(i) == [docs[0].external_id]
    for (u, _) in fake.calls:
        assert "RoleAssignments" not in u and "permissions" not in u.lower() and "Sharing" not in u, (
            f"a permission endpoint was called: {u}")
    print("  PASS  documents take the store's audience; no permission endpoint is called")


# ---------------------------------------------------------------- the factory and the kind
def test_the_factory_refuses_an_empty_audience_and_the_kind_is_registered_everywhere():
    from dbsearch.router.providers.connector import sharepoint_link_connector_factory
    try:
        sharepoint_link_connector_factory({"id": "sp-1", "link": LINK, "acl": []})
        raise AssertionError("an empty acl composed - every document would be visible to nobody")
    except ValueError as e:
        assert "audience" in str(e), e
    c = sharepoint_link_connector_factory({"id": "sp-1", "link": LINK, "acl": ["me"]})
    assert isinstance(c, SharePointLinkConnector)
    assert '"sharepoint_link"' in ROUTER_API.split("PLANNED_KINDS = (")[1].split(")")[0], (
        "sharepoint_link is not a PLANNED_KIND, so /router/kinds will not advertise it")
    assert 'ConnectorStoreProvider("sharepoint_link", r.sharepoint_link_connector_factory' in ROUTER_API, (
        "no provider is registered for kind sharepoint_link - a node would compose as 'skipped'")
    assert "sharepoint_link_connector_factory" in ROUTER_INIT
    assert '"sharepoint_link": "SharePoint"' in ORIGINS, (
        "origins.SYSTEM has no label, so a citation would read 'Sharepoint_link'")
    print("  PASS  the factory refuses an empty acl; the kind is registered in all four homes")


# ------------------------------------------------------------------------------ the canvas
def _kinds_entry():
    m = re.search(r"^\s*sharepoint_link:\s*\{(.*)$", CANVAS_TEXT, re.M)
    assert m, "canvas.js KINDS has no sharepoint_link entry"
    return m.group(0)


def test_the_canvas_offers_it_under_files_and_links_with_no_account_gate():
    entry = _kinds_entry()
    assert "needs:" not in entry, (
        "sharepoint_link carries a `needs` gate. It reads an anonymous link with NO Microsoft "
        "identity, so demanding an account here is the gate lying about its own requirement "
        "(#920's defect, again)")
    assert 'k:"link"' in entry, "the node has no link field to paste into"
    row = re.search(r'label:"Files & Links".*?kinds:\[(.*?)\]', CANVAS_TEXT, re.S)
    assert row and '"sharepoint_link"' in row.group(1), (
        "sharepoint_link is not in the Files & Links row, so no user can reach it")
    # the control: the consent-based SharePoint kind keeps its gate (#920's clause 2)
    assert re.search(r'^\s*sharepoint:\{.*needs:"entra"', CANVAS_TEXT, re.M), (
        "the consent-based `sharepoint` kind lost its entra gate")
    print("  PASS  KINDS.sharepoint_link is ungated, has a link field, sits in Files & Links")


_dom = {}


def _report(scenario):
    import _domgate
    if scenario not in _dom:
        if not _domgate.gate(f"the #924 SharePoint-link DOM check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the SharePoint link tile ({scenario})")
    return _domgate.resolve(_dom[scenario])


def test_an_unlinked_user_can_add_a_sharepoint_link_node_filed_under_themselves():
    """The owner's requirement on a real DOM: signed in, NOTHING linked, click the tile, get
    a node that carries their own oid (#920: private to the adder - the only honest audience
    for a public-link document)."""
    r = _report("unlinked_can_add_sharepoint_link")
    if r is None:
        return
    m = r["filesMenu"]
    assert "sharepoint_link" in m["addable"], (
        f"the SharePoint link tile is gated for a caller with no Microsoft account: {m}")
    assert "sharepoint" in m["gatedKinds"], (
        f"the control failed: the consent-based SharePoint tile is no longer gated: {m}")
    assert r["nodeAdded"], "clicking the tile added no node"
    assert r["aclCarriesAdder"], (
        f"the new node does not carry the adder's own oid: pills={r.get('pills')}")
    print("  PASS  an unlinked user adds a SharePoint link node filed under themselves")


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {e}")
            except Exception as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
