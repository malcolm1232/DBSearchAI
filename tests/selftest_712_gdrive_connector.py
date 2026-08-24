"""#712 - Google Drive as a document source (slice 1: public share links).

The design decision this pins: slice 1 reads ONLY "Anyone with the link" content with a
deployment API key, and every document takes the STORE's audience (#673's rule) - a public
folder has no per-user audience to read, so the store's own acl is the only honest answer.
These tests stop that promise drifting, and pin that permissions.list is never called.

    PYTHONPATH=src python3 tests/selftest_712_gdrive_connector.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.connectors.gdrive import (  # noqa: E402
    GDriveConnector, _EXPORT_CAP, _MAX_BYTES, _folder_id_from_link)

CANVAS = (ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js").read_text()
ROUTER_API = (ROOT / "src/dbsearch/server/router_api.py").read_text()


class _Resp:
    def __init__(self, status, payload=None, content=b""):
        self.status_code = status
        self._payload = payload or {}
        self.content = content
    def json(self):
        return self._payload


class FakeDrive:
    """A fake transport that pages like the real API. Folders: {folder_id: [file dicts]}.
    Records every request so tests can assert on params (key, supportsAllDrives) and on
    which endpoints were NEVER hit (permissions.list)."""

    def __init__(self, folders, contents=None, restricted=(), page_size=None,
                 empty_page_first=(), force_status=None):
        self._folders = folders
        self._contents = contents or {}
        self._restricted = set(restricted)
        self._page = page_size            # force pagination when set
        # folder ids that must first hand back a page with an EMPTY files array that
        # still carries a nextPageToken - the real Drive API can do this, and a crawl
        # that stops on "no files" rather than "no token" would silently drop the rest.
        self._empty_page_first = set(empty_page_first)
        # {fid: status_code} - forces an arbitrary status (429, 500, ...) on a per-file
        # fetch, independent of `restricted`/`contents`, so systemic/transient failures
        # can be modeled distinctly from a genuine per-file 403/404 refusal.
        self._force_status = dict(force_status or {})
        self.calls = []                   # (url, params) tuples

    def get(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params))
        if url.endswith("/files") and "q" in params:
            fid = params["q"].split("'")[1]
            files = self._folders.get(fid, [])
            token = params.get("pageToken")
            if fid in self._empty_page_first and token is None:
                return _Resp(200, {"files": [], "nextPageToken": "resume"})
            start = 0 if not token or token == "resume" else int(token)
            end = start + (self._page or len(files) or 1)
            page = {"files": files[start:end]}
            if end < len(files):
                page["nextPageToken"] = str(end)
            return _Resp(200, page)
        fid = url.rstrip("/").split("/")[-2] if url.endswith("/export") else url.rstrip("/").split("/")[-1]
        if fid in self._force_status:
            forced = self._force_status[fid]
            # A forced entry may be a whole response, not just a status: #767 turns on what
            # the BODY of a 403 says, and one of the two shapes Google really sends has no
            # JSON body at all. A status-only fake cannot express that case.
            return forced if isinstance(forced, _Resp) else \
                _Resp(forced, {"error": {"message": "forced"}})
        if fid in self._restricted:
            return _Resp(403, {"error": {"message": "insufficientFilePermissions"}})
        if url.endswith("/export"):
            body = self._contents.get(fid)
            # NOTE: no size check here. Modeling "export exceeds the cap" as a fake 403
            # would make the connector's own post-200 size check (len(resp.content) >
            # _EXPORT_CAP) untestable dead code - the real API returns 200 with the
            # oversized body; the cap is enforced by the CALLER, not the transport. A
            # test wanting the cap path puts an over-cap body directly in `contents`.
            if body is None:
                return _Resp(403, {"error": {"message": "exportSizeLimitExceeded"}})
            return _Resp(200, content=body)
        if params.get("alt") == "media":
            return _Resp(200, content=self._contents.get(fid, b""))
        if fid in self._folders or fid in self._contents:
            return _Resp(200, {"id": fid, "name": "x"})
        return _Resp(404, {"error": {"message": "notFound"}})


def _f(fid, name, mime, modified, size=100, md5="m5", version="1"):
    return {"id": fid, "name": name, "mimeType": mime, "modifiedTime": modified,
            "size": str(size), "md5Checksum": md5, "version": version,
            "webViewLink": f"https://drive.google.com/file/d/{fid}/view"}


TREE = {
    "root": [
        _f("f1", "report.pdf", "application/pdf", "2026-08-01T00:00:00Z"),
        # md5="" deliberately: native Google Docs have NO md5Checksum - that absence is what
        # the version:modifiedTime hash fallback exists for, so the fixture must model it.
        _f("f2", "notes", "application/vnd.google-apps.document", "2026-08-03T00:00:00Z",
           size=0, md5=""),
        _f("f3", "logo.png", "image/png", "2026-08-02T00:00:00Z"),          # unparseable
        {"id": "sub", "name": "sub", "mimeType": "application/vnd.google-apps.folder",
         "modifiedTime": "2026-08-01T00:00:00Z"},
    ],
    "sub": [_f("f4", "deep.txt", "text/plain", "2026-08-05T00:00:00Z")],
}


def _conn(fake=None, **kw):
    fake = fake or FakeDrive(TREE)
    c = GDriveConnector("acme", "root", ["alice-oid"], api_key="AIza-test",
                        http_factory=lambda: fake, **kw)
    return c, fake


def test_the_link_parser_accepts_urls_and_bare_ids_and_rejects_junk():
    fid = "1qGNIw6vMXl4uVVRtXBJb03408rdI3Di6"
    assert _folder_id_from_link(
        f"https://drive.google.com/drive/folders/{fid}?usp=sharing") == fid
    assert _folder_id_from_link(
        f"https://drive.google.com/drive/u/0/folders/{fid}") == fid
    assert _folder_id_from_link(fid) == fid          # a bare id pastes fine too
    for junk in ("https://drive.google.com/file/d/xyz/view", "not a link",
                 "https://docs.google.com/document/d/abc/edit"):
        try:
            _folder_id_from_link(junk)
            raise AssertionError(f"accepted junk link: {junk!r}")
        except ValueError as e:
            assert "drive.google.com/drive/folders/" in str(e), e
    print("  PASS  the link parser accepts folder URLs and bare ids, rejects junk with help")


def test_an_EMPTY_link_says_so_instead_of_accusing_the_reader():
    """#918. The empty string used to sit in the junk list above, which meant this parser
    told a reader who had typed NOTHING that what they typed did not look like a Drive link.

    Measured on the owner's live canvas: gdrive-1 carried `link: ""` and a red node note
    reading "that does not look like a Drive folder link". The field had never been filled.
    That is the launch gate's own standard failing - a node whose stated reason is not true.

    Empty and malformed are different situations and a reader acts differently in each, so
    they get different sentences (the same rule provenanceNote follows for its four states).
    """
    for blank in ("", "   ", None):
        try:
            _folder_id_from_link(blank)
            raise AssertionError(f"accepted a blank link: {blank!r}")
        except ValueError as e:
            msg = str(e)
            assert "no folder link yet" in msg, (
                f"a blank link does not say it is blank: {msg!r}")
            assert "does not look like" not in msg, (
                "a reader who typed nothing is still being told their input is wrong: "
                f"{msg!r}")
    # ...and the control: a REAL malformed link must still get the specific help, or this
    # fix would have bought honesty by making every message vaguer.
    try:
        _folder_id_from_link("not a link")
        raise AssertionError("accepted junk")
    except ValueError as e:
        assert "drive.google.com/drive/folders/" in str(e), e
    print("  PASS  an empty link says it is empty; a malformed one still gets real help")


def test_one_unreadable_item_does_not_abort_the_crawl_and_is_counted():
    """runner.py used to call fetch_content UNWRAPPED, so one restricted file killed the
    whole crawl - contradicting its own LAW 3 comment ('one connector/item failing never
    blocks the rest'). This pins the fix: skip, count, continue; strict=True propagates."""
    from dbsearch.adapters.local import (HashingEmbedding, InMemoryIndex,
                                         InMemoryObjectStore, InMemoryQueue,
                                         LocalRichExtractor)
    from dbsearch.core.models import Document, Principal
    from dbsearch.pipeline.runner import run_ingestion
    from dbsearch.ports.base import ConnectorPort, ItemUnreadable

    class OneBadApple(ConnectorPort):
        def authenticate(self, config):
            return None
        def list_changes(self, cursor):
            return [{"external_id": "good.txt"}, {"external_id": "restricted.txt"},
                    {"external_id": "also-good.txt"}], "cursor-1"
        def fetch_content(self, item):
            if item["external_id"] == "restricted.txt":
                raise ItemUnreadable("403 on an individually-restricted file")
            return b"readable text content", "text/plain"
        def fetch_acl(self, item):
            return [Principal(oid="alice", kind="user")]
        def to_documents(self, item):
            return [Document(tenant_id="t", source_id="x", external_id=item["external_id"],
                             content_ref="", acl=self.fetch_acl(item),
                             title=item["external_id"], uri="", content_hash="h",
                             source_meta={"mime": "text/plain"})]

    obj = InMemoryObjectStore()
    result = run_ingestion(OneBadApple(), InMemoryQueue(), obj,
                           LocalRichExtractor(), HashingEmbedding(), InMemoryIndex(obj))
    assert result.doc_count == 2, result
    assert result.unreadable == 1, result
    assert result.cursor == "cursor-1", result
    try:
        obj2 = InMemoryObjectStore()
        run_ingestion(OneBadApple(), InMemoryQueue(), obj2,
                      LocalRichExtractor(), HashingEmbedding(), InMemoryIndex(obj2),
                      strict=True)
        raise AssertionError("strict=True swallowed ItemUnreadable")
    except ItemUnreadable:
        pass
    print("  PASS  an unreadable item is skipped and counted; strict propagates")


def test_the_edition_rail_also_carries_the_unreadable_count_through():
    """Pins the SECOND call site of SourceRegistry.record_sync -
    src/dbsearch/server/edition.py Edition._crawl._commit (the self-host SharePoint/local-folder
    rail via build_edition(), exercised by tests/selftest_admin_sources.py) - not just
    router/providers/connector.py's _commit. record_sync sets d.unreadable UNCONDITIONALLY
    (defaulting to 0), so a fix that only updated ONE call site would let the other silently
    reset the count to 0 on every sync: exactly the 'store that looks full and is not' failure
    the field exists to prevent. Drives the real Edition path (build_edition ->
    resync_source_blocking) rather than calling record_sync directly, so a regression at the
    actual call site is what makes this fail."""
    import os
    os.environ.setdefault("SELFHOST_BACKEND", "memory")
    from dbsearch.connectors.registry import SourceDescriptor
    from dbsearch.core.models import Document, Principal
    from dbsearch.ports.base import ConnectorPort, ItemUnreadable
    from dbsearch.server.edition import build_edition

    class OneBadApple(ConnectorPort):
        def authenticate(self, config):
            return None
        def list_changes(self, cursor):
            return [{"external_id": "good.txt"}, {"external_id": "restricted.txt"}], None
        def fetch_content(self, item):
            if item["external_id"] == "restricted.txt":
                raise ItemUnreadable("403 on an individually-restricted file")
            return b"readable text content", "text/plain"
        def fetch_acl(self, item):
            return [Principal(oid="alice", kind="user")]
        def to_documents(self, item):
            return [Document(tenant_id="t", source_id="x", external_id=item["external_id"],
                             content_ref="", acl=self.fetch_acl(item),
                             title=item["external_id"], uri="", content_hash="h",
                             source_meta={"mime": "text/plain"})]

    ed = build_edition()
    ed.source_registry.register(SourceDescriptor(
        source_id="flaky", kind="local", display_name="Flaky", connector=OneBadApple()))
    s = ed.resync_source_blocking("flaky")
    assert s.doc_count == 1, s
    assert s.unreadable == 1, s
    print("  PASS  the Edition rail's record_sync call site also carries unreadable through")


def test_the_crawl_recurses_pages_to_the_end_and_filters_at_listing_time():
    c, fake = _conn(FakeDrive(TREE, page_size=1))     # 1 item per page: forces pagination
    items, cursor = c.list_changes(None)
    ids = sorted(i["external_id"] for i in items)
    assert ids == ["f1", "f2", "f4"], ids              # png skipped, subfolder recursed
    assert cursor == "2026-08-05T00:00:00Z", cursor    # newest modifiedTime seen
    fetches = [u for u, p in fake.calls if p.get("alt") == "media" or u.endswith("/export")]
    assert not fetches, "listing downloaded content"
    for url, params in fake.calls:
        assert params.get("supportsAllDrives") == "true", (url, params)
        assert params.get("key") == "AIza-test", "the API key did not ride the request"
    print("  PASS  recursion + full pagination + listing-time filtering + shared-drive params")


def test_the_cursor_makes_a_second_crawl_incremental():
    """Pins the tie boundary (controller ruling on Finding 1): the spec says a modifiedTime
    TIE with the cursor RE-INGESTS, never skips. external_id is the stable file id, so a
    tie costs one idempotent re-ingest; skipping a tie would make that document invisible to
    every future crawl forever, since the cursor never regresses below it."""
    c, _ = _conn()
    items, cursor = c.list_changes(None)
    assert len(items) == 3, items
    assert cursor == "2026-08-05T00:00:00Z", cursor        # f4's own modifiedTime

    # re-crawl AT the cursor: the tie (f4, modifiedTime == cursor) must be RE-INGESTED, not
    # skipped - not everything (f1/f2 are genuinely older, must not reappear) and not
    # nothing (the bug this pins: f4 would vanish from every future crawl, forever).
    again, cursor2 = _conn()[0].list_changes(cursor)
    assert [i["external_id"] for i in again] == ["f4"], again
    assert cursor2 == cursor, cursor2

    # a NEW file whose modifiedTime EQUALS the cursor must also be returned, not silently
    # dropped - this is exactly the case the original `<=` bug would have hidden.
    tied_tree = {"root": TREE["root"] + [_f("f5", "boundary.txt", "text/plain", cursor)],
                 "sub": TREE["sub"]}
    landed, _ = _conn(FakeDrive(tied_tree))[0].list_changes(cursor)
    assert sorted(i["external_id"] for i in landed) == ["f4", "f5"], landed

    # and a genuinely newer file (past the cursor) still lands, as always.
    newer_tree = {"root": TREE["root"] + [_f("f6", "newer.txt", "text/plain",
                                              "2026-08-09T00:00:00Z")],
                  "sub": TREE["sub"]}
    newer_landed, cursor3 = _conn(FakeDrive(newer_tree))[0].list_changes(cursor)
    assert sorted(i["external_id"] for i in newer_landed) == ["f4", "f6"], newer_landed
    assert cursor3 == "2026-08-09T00:00:00Z", cursor3
    print("  PASS  a modifiedTime tie with the cursor re-ingests, never skips; newer files still land")


def test_an_empty_page_that_still_carries_a_token_does_not_stop_the_crawl():
    """The real Drive API can hand back a page with an EMPTY files array that still
    carries a nextPageToken. A crawl that stops on 'no files this page' rather than 'no
    token' would silently drop everything after it - the loop must exit on `not token`
    alone, never on an empty files list."""
    tree = {"root": [_f("h1", "after-empty-page.txt", "text/plain", "2026-08-01T00:00:00Z")]}
    c, fake = _conn(FakeDrive(tree, empty_page_first=("root",)))
    items, _ = c.list_changes(None)
    assert [i["external_id"] for i in items] == ["h1"], items
    file_calls = [p for u, p in fake.calls if u.endswith("/files")]
    assert len(file_calls) == 2, file_calls   # the empty+token page, then the real page
    print("  PASS  an empty page carrying a token does not stop the crawl")


def test_external_id_is_the_file_id_so_reingest_replaces():
    c, _ = _conn()
    items, _ = c.list_changes(None)
    item = next(i for i in items if i["external_id"] == "f1")
    assert c.external_ids(item) == ["f1"]
    print("  PASS  external_id is the stable file id")


def test_native_docs_export_and_binaries_download():
    fake = FakeDrive(TREE, contents={"f1": b"%PDF-1.4 report bytes",
                                     "f2": b"the notes doc as plain text"})
    c, _ = _conn(fake)
    items, _ = c.list_changes(None)
    by_id = {i["external_id"]: i for i in items}
    raw, mime = c.fetch_content(by_id["f1"])
    assert raw.startswith(b"%PDF") and mime == "application/pdf"
    raw, mime = c.fetch_content(by_id["f2"])
    assert raw == b"the notes doc as plain text" and mime == "text/plain"
    exports = [u for u, _ in fake.calls if u.endswith("/export")]
    medias = [p for _, p in fake.calls if p.get("alt") == "media"]
    assert len(exports) == 1 and len(medias) == 1, (exports, medias)
    print("  PASS  native docs go through files.export, binaries through alt=media")


def test_an_unreadable_file_raises_ItemUnreadable_not_a_crash():
    """Covers BOTH branches: f1 is a restricted BINARY (alt=media 403) and f2 is a
    restricted native doc (files.export 403) - a prior version of this test only
    exercised the native-doc branch, so the binary branch's status check had zero
    coverage (a restricted binary could have silently returned empty bytes and every
    test would still have passed)."""
    from dbsearch.ports.base import ItemUnreadable
    fake = FakeDrive(TREE, restricted={"f1", "f2"})
    c, _ = _conn(fake)
    items, _ = c.list_changes(None)
    by_id = {i["external_id"]: i for i in items}
    for bad in ("f1", "f2"):
        try:
            c.fetch_content(by_id[bad])
            raise AssertionError(f"a 403'd file ({bad}) did not raise ItemUnreadable")
        except ItemUnreadable:
            pass
    print("  PASS  per-file 403 surfaces as ItemUnreadable on BOTH the binary and export branches")


def test_export_over_the_cap_raises_ItemUnreadable():
    """A 200 response whose exported body exceeds _EXPORT_CAP must be refused, not
    silently accepted - this is the cap enforced by the CALLER (files.export itself
    has no such client-visible cap in this fixture), so the fake must be able to
    return a 200 with an over-cap body, distinct from a 403 restriction."""
    from dbsearch.ports.base import ItemUnreadable
    fake = FakeDrive(TREE, contents={"f2": b"x" * (_EXPORT_CAP + 1)})
    c, _ = _conn(fake)
    items, _ = c.list_changes(None)
    by_id = {i["external_id"]: i for i in items}
    try:
        c.fetch_content(by_id["f2"])
        raise AssertionError("an over-cap export did not raise ItemUnreadable")
    except ItemUnreadable:
        pass
    print("  PASS  an export body over _EXPORT_CAP raises ItemUnreadable")


def test_rate_limit_and_server_errors_fail_the_crawl_not_count_as_unreadable():
    """THE CURSOR INTERACTION (the reason this matters). list_changes computes
    next_cursor from modifiedTime seen at LISTING time, independent of whether the
    later fetch succeeds. If a transient 429/5xx were treated as ItemUnreadable, the
    runner would skip-and-count it, the NEXT incremental crawl would see
    stamp < cursor ("already ingested by a prior crawl") and never re-list it - a
    transient failure would permanently and silently drop a document, while the sync
    reports success. So 429/5xx (and any other unexpected non-200) must raise
    RuntimeError instead, failing the whole crawl loudly - exactly like list_changes
    already does for a listing-call failure - so the cursor is never advanced past
    the item and it is retried on the next crawl."""
    from dbsearch.ports.base import ItemUnreadable
    for status in (429, 500, 503):
        # the alt=media (binary) branch
        fake = FakeDrive(TREE, contents={"f1": b"x"}, force_status={"f1": status})
        c, _ = _conn(fake)
        items, _ = c.list_changes(None)
        by_id = {i["external_id"]: i for i in items}
        try:
            c.fetch_content(by_id["f1"])
            raise AssertionError(f"status {status} did not fail the crawl")
        except ItemUnreadable:
            raise AssertionError(f"status {status} was counted as unreadable, not a crawl failure")
        except RuntimeError:
            pass

        # the files.export (native doc) branch
        fake2 = FakeDrive(TREE, contents={"f2": b"x"}, force_status={"f2": status})
        c2, _ = _conn(fake2)
        items2, _ = c2.list_changes(None)
        by_id2 = {i["external_id"]: i for i in items2}
        try:
            c2.fetch_content(by_id2["f2"])
            raise AssertionError(f"status {status} on export did not fail the crawl")
        except ItemUnreadable:
            raise AssertionError(f"status {status} on export was counted as unreadable")
        except RuntimeError:
            pass
    print("  PASS  429/5xx on either branch fail the whole crawl (RuntimeError), never counted unreadable")


class _HtmlResp(_Resp):
    """Google's anti-automation interstitial: HTTP 403 whose body is an HTML page, so
    `.json()` RAISES rather than returning an error dict. Observed live on 260817 - the
    literal text is "your computer or network may be sending automated queries". A JSON-only
    body check would sail straight past the shape that actually fired."""
    def json(self):
        raise ValueError("not JSON: text/html")


def test_a_throttled_403_fails_the_crawl_and_is_never_counted_unreadable():
    """#767 - THE 403 HALF OF THE SAME CURSOR TRAP.

    `test_rate_limit_and_server_errors_...` above states the argument in full and then
    enumerates transient as 429/5xx. Google's rate limiter answers **403**: Drive documents
    userRateLimitExceeded, rateLimitExceeded, dailyLimitExceeded and sharingRateLimitExceeded
    all as 403, all retryable, and under load it also returns a bare HTML interstitial with
    no `reason` at all. Classifying those as "an individually-restricted file" skips-and-counts
    the document, and because the cursor advanced at LISTING time it is never re-listed - one
    transient throttle deletes a document from the store forever while the sync reports
    success and the store answers "the source is there and readable" about data it dropped.

    Found by driving live Drive, not by reading the code: a folder whose only file WAS
    readable answered empty for every identity, and the log said "individually-restricted".

    A genuine permission refusal must STILL be unreadable - that is the case the connector
    exists to tolerate - so this pins both directions."""
    from dbsearch.ports.base import ItemUnreadable

    throttles = [
        ("documented quota JSON",
         _Resp(403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}],
                               "message": "User Rate Limit Exceeded"}})),
        ("bare HTML interstitial (observed live)", _HtmlResp(403, None, b"<html>Sorry...</html>")),
    ]
    for label, resp in throttles:
        for branch, fid in (("alt=media", "f1"), ("files.export", "f2")):
            fake = FakeDrive(TREE, contents={fid: b"x"}, force_status={fid: resp})
            c, _ = _conn(fake)
            items, _ = c.list_changes(None)
            item = {i["external_id"]: i for i in items}[fid]
            try:
                c.fetch_content(item)
                raise AssertionError(f"{label} on {branch} did not fail the crawl")
            except ItemUnreadable as exc:
                raise AssertionError(
                    f"{label} on {branch} was counted UNREADABLE ({exc}) - the cursor has "
                    "already advanced past this item, so the document is now permanently "
                    "invisible to every future incremental crawl")
            except RuntimeError:
                pass

    # The other direction: a real per-file permission refusal is still skip-and-count, or
    # the fix would have traded silent data loss for a crawl that no restricted file survives.
    fake = FakeDrive(TREE, contents={"f1": b"x"}, restricted={"f1"})
    c, _ = _conn(fake)
    items, _ = c.list_changes(None)
    try:
        c.fetch_content({i["external_id"]: i for i in items}["f1"])
        raise AssertionError("a genuinely restricted file did not raise at all")
    except ItemUnreadable:
        pass
    except RuntimeError as exc:
        # Caught deliberately, and reported rather than allowed to propagate: a bare
        # RuntimeError escaping here crashes the runner with a traceback, which inside a
        # 263-file suite reads like the harness broke rather than like this guard doing its
        # job. Mutation-tested - narrowing _is_permission_refusal to False lands exactly here.
        raise AssertionError(
            "a genuine permission refusal now FAILS THE WHOLE CRAWL instead of being "
            f"skipped and counted ({exc}) - the #767 fix has been over-tightened, and no "
            "restricted file in a folder can be tolerated any more") from None
    print("  PASS  a throttled 403 (quota JSON or HTML) fails the crawl; a real refusal is "
          "still unreadable")


def test_export_size_limit_exceeded_is_diagnosed_correctly():
    """Google returns HTTP 403 with `exportSizeLimitExceeded` in the error body for a native
    doc whose export is over the 10MB cap - a REAL size problem, not a permissions one. The
    old code never read the body, so it routed this through _raise_for_fetch_failure and
    reported "an individually-restricted or deleted file", leaving an operator to check Drive,
    find the doc IS shared publicly, and get no explanation. FakeDrive's default 403 for an
    export with no configured `contents` entry is exactly this body (see FakeDrive.get's
    comment) - this test is the first to reach that branch; every other export test supplies
    `contents` precisely to avoid it."""
    from dbsearch.ports.base import ItemUnreadable
    fake = FakeDrive(TREE)                 # no `contents` for f2: hits the size-limit body
    c, _ = _conn(fake)
    items, _ = c.list_changes(None)
    by_id = {i["external_id"]: i for i in items}
    try:
        c.fetch_content(by_id["f2"])
        raise AssertionError("an over-cap export (403 exportSizeLimitExceeded) did not raise")
    except ItemUnreadable as e:
        msg = str(e).lower()
        assert "cap" in msg or "10mb" in msg, e
        assert "restrict" not in msg, ("misdiagnosed a size-cap 403 as a restriction", e)
        assert "f2" in str(e), e
    print("  PASS  a 403 exportSizeLimitExceeded body is diagnosed as a size cap, not a restriction")


def test_an_oversized_binary_is_dropped_at_listing_and_logged():
    """A binary over _MAX_BYTES is dropped at LISTING time (it is never fetched, so it never
    reaches the runner's per-item ItemUnreadable/unreadable-count path) - but that drop must
    not be SILENT. An oversized PDF is a document the user expects in the store, unlike the
    mime filter above it (a folder of images is legitimately free), so this pins that the drop
    is at least logged with the file's name."""
    import logging

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.messages = []
        def emit(self, record):
            self.messages.append(record.getMessage())

    big_tree = {"root": TREE["root"] + [
        _f("huge1", "huge.pdf", "application/pdf", "2026-08-01T00:00:00Z",
           size=_MAX_BYTES + 1)]}
    logger = logging.getLogger("dbsearch.ingest")
    capture = _Capture()
    logger.addHandler(capture)
    try:
        c, _ = _conn(FakeDrive(big_tree))
        items, _ = c.list_changes(None)
    finally:
        logger.removeHandler(capture)
    ids = [i["external_id"] for i in items]
    assert "huge1" not in ids, ids
    assert any("huge.pdf" in m for m in capture.messages), capture.messages
    print("  PASS  an oversized binary is excluded at listing and the drop is logged")


def test_authenticate_names_the_missing_key_not_the_folder_sharing():
    """A missing GOOGLE_API_KEY used to be misreported as "this folder isn't shared as
    'Anyone with the link'" - sending an operator to fix the wrong thing (the user's Drive
    sharing) for what is actually a deployment config gap. authenticate() must check for a
    missing key/credential FIRST and name it; the sharing message stays for the case a key IS
    present and Drive still refuses."""
    fake = FakeDrive(TREE)
    no_key = GDriveConnector("acme", "root", ["alice-oid"], http_factory=lambda: fake)
    try:
        no_key.authenticate({})
        raise AssertionError("authenticate() with no key and no credential did not raise")
    except RuntimeError as e:
        assert "GOOGLE_API_KEY" in str(e), e
        assert "shared as" not in str(e), e
    assert not fake.calls, "authenticate() made a network call before checking for a key"

    bad_folder = GDriveConnector("acme", "does-not-exist", ["alice-oid"],
                                 api_key="AIza-test", http_factory=lambda: fake)
    try:
        bad_folder.authenticate({})
        raise AssertionError("authenticate() against a missing folder did not raise")
    except RuntimeError as e:
        assert "shared as" in str(e), e
        assert "GOOGLE_API_KEY" not in str(e), e
    print("  PASS  a missing key names itself; a present key + refused folder keeps the sharing message")


def test_every_document_takes_the_stores_audience_and_never_wider():
    """THE PROMISE (#673's shape). A public folder has no per-user audience to read, so the
    store's own acl is the only honest answer - and it cannot widen anything, because the
    store's acl already gates who may query the store at all."""
    fake = FakeDrive(TREE, contents={"f1": b"x", "f2": b"y"})
    c, _ = _conn(fake)
    items, _ = c.list_changes(None)
    for item in items:
        assert [p.oid for p in c.fetch_acl(item)] == ["alice-oid"]
        assert [p.kind for p in c.fetch_acl(item)] == ["user"]
    docs = c.to_documents(next(i for i in items if i["external_id"] == "f1"))
    assert [p.oid for p in docs[0].acl] == ["alice-oid"]
    assert docs[0].source_id == "gdrive"
    assert docs[0].content_hash == "m5"                  # binary: md5Checksum
    ndoc = c.to_documents(next(i for i in items if i["external_id"] == "f2"))[0]
    assert ndoc.content_hash == "1:2026-08-03T00:00:00Z"  # native: version:modifiedTime
    print("  PASS  documents carry the store's audience; hashes are honest per type")


def test_permissions_list_is_never_called_in_slice_1():
    """Calling it and discarding the result would be dead code whose explanatory comment
    goes stale within one slice - and the day it IS called, that must be slice 2's ADR-gated
    change, not an accident."""
    fake = FakeDrive(TREE, contents={"f1": b"x", "f2": b"y"})
    c, _ = _conn(fake)
    items, _ = c.list_changes(None)
    for item in items:
        c.fetch_acl(item)
        c.to_documents(item)
    assert not [u for u, _ in fake.calls if "/permissions" in u], "permissions.list was called"
    print("  PASS  permissions.list is never called in slice 1")


def test_the_factory_refuses_an_empty_audience_and_parses_the_link():
    """The production guard is `config.get("acl") or []`, which refuses THREE shapes of "no
    audience": an empty list, the key missing entirely, and an explicit None. `.get(key,
    default)` only substitutes its default when the key is ABSENT, so a later "tidy-up" to
    `config.get("acl", [])` would NOT let `acl: None` slip through - that call still returns
    None when the key is present but explicitly None, and `or []` still catches it. The real
    risk is weakening the check itself, e.g. to `if acl == []:`, which would let `acl: None`
    (and any other falsy-but-not-`[]` value) through unrefused - so all three shapes are
    exercised here to guard against that regression, not a `.get()` default misreading."""
    from dbsearch.router.providers.connector import gdrive_connector_factory
    link = "https://drive.google.com/drive/folders/1abcdefghijkl"
    no_audience = (
        {"id": "g", "link": link, "acl": []},
        {"id": "g", "link": link},                # acl key absent entirely
        {"id": "g", "link": link, "acl": None},    # acl explicitly None
    )
    for config in no_audience:
        try:
            gdrive_connector_factory(config)
            raise AssertionError(f"a gdrive store with no acl was accepted: {config}")
        except ValueError as e:
            assert "audience" in str(e), e
    c = gdrive_connector_factory({"id": "g",
                                  "link": "https://drive.google.com/drive/folders/1abcdefghijkl?usp=sharing",
                                  "acl": ["bob-oid", "carol-oid"]})
    assert c._folder_id == "1abcdefghijkl"
    assert [p.oid for p in c.fetch_acl({})] == ["bob-oid", "carol-oid"]
    print("  PASS  empty/absent/None audience all refused; link parsed; audience is the store's own")


def test_the_credential_seam_reaches_the_factory_by_name():
    """Slice 2 enters HERE: _build_connector hands a delegated credential only to factories
    that declare a `credential` parameter. The name, not the arity, is the contract."""
    import inspect
    from dbsearch.router.providers.connector import gdrive_connector_factory
    assert "credential" in inspect.signature(gdrive_connector_factory).parameters
    c = gdrive_connector_factory({"id": "g", "link": "1abcdefghijklm", "acl": ["a"]},
                                 credential="ya29.token")
    assert c._credential == "ya29.token"
    print("  PASS  the credential parameter exists by NAME and reaches the connector")


def test_gdrive_is_wired_into_the_rail_and_the_palette():
    """A text-only check (`'ConnectorStoreProvider("gdrive"' in ROUTER_API`) would still pass
    if that call site dropped or typo'd a required kwarg (e.g. `job_partition`) - the two
    substrings survive unchanged while `_State()` raises TypeError at construction. So this
    builds the REAL state the server builds and pulls whatever got registered under the
    "gdrive" kind out of the actual registry, proving the registration call succeeds and
    wires the real factory - not a copy, not a decoy - under the right kind. The PLANNED_KINDS
    text check stays too: it is a distinct fact (the canvas is told this kind is real) that
    registry construction alone does not prove.

    What this does NOT catch: a wrong VALUE bound to a correctly-named kwarg (e.g.
    job_partition="the-wrong-workspace") - only that the call succeeds and the right kind
    resolves to the right factory."""
    assert '"gdrive"' in ROUTER_API, "gdrive missing from PLANNED_KINDS"
    from dbsearch.router.providers.connector import gdrive_connector_factory
    from dbsearch.server.router_api import _State
    state = _State()
    provider = state.registry.get("gdrive")
    assert provider.kind == "gdrive", provider.kind
    assert provider._factory is gdrive_connector_factory, provider._factory
    print("  PASS  gdrive is in PLANNED_KINDS and registered in the real registry with the real factory")


def test_the_canvas_says_the_folder_is_public_out_loud():
    """The honesty line is a FEATURE (#673's pattern): an unspoken limit looks like a broken
    product later. If this assertion fails because someone reworded the note, update BOTH -
    the note must keep saying the content is public.

    Scoped to the gdrive KINDS entry's OWN line, not grepped across the whole file: an
    unscoped `"already public..." in CANVAS` check would still pass if the note text landed
    on a different KINDS entry, or was merely left behind in a comment, while the gdrive
    entry itself said nothing."""
    gdrive_lines = [l for l in CANVAS.splitlines() if l.strip().startswith("gdrive:")]
    assert len(gdrive_lines) == 1, ("gdrive missing (or duplicated) in the canvas KINDS map", gdrive_lines)
    assert "already public on the internet" in gdrive_lines[0], gdrive_lines[0]
    print("  PASS  the canvas card says plainly, on its own KINDS line, that the folder's content is public")


def test_gdrive_sits_in_files_and_links_without_a_delegation():
    """#920 re-homed this. gdrive USED to ride the Google Cloud group, which is what made the
    palette demand a Google account for a folder that needs none - slice 1 reads an "anyone
    with the link" folder with the deployment's own API key. The delegation half of the
    original assertion is unchanged and still load-bearing: no _GCP_KINDS entry."""
    files_row = re.search(r'\{key:"files".*?kinds:\[([^\]]*)\]', CANVAS, re.S)
    assert files_row and '"gdrive"' in files_row.group(1), (
        "gdrive is not offered under Files & Links, so slice 1 has no door in the palette")
    google_row = next(l for l in CANVAS.splitlines() if 'key:"google"' in l)
    assert "gdrive" not in google_row, (
        "gdrive is back under the Google Cloud brand row, where the row gate demands a "
        f"Google account this kind never needed: {google_row}")
    gcp = next(l for l in CANVAS.splitlines() if "_GCP_KINDS" in l and "Set" in l)
    assert "gdrive" not in gcp, "slice 1 must not give gdrive a delegation block"
    print("  PASS  gdrive sits in Files & Links; no Google gate, no delegation in slice 1")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    print("\nFAILED" if fails else "\n#712 GDRIVE CONNECTOR SELF-TEST PASSED.")
    sys.exit(1 if fails else 0)
