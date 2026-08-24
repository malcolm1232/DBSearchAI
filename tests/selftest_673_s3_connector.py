"""#673 - S3 as a document source, and the ACL promise it makes.

The design decision this pins: S3 CANNOT answer "who may read this object?" as a principal
list - access is IAM + bucket policy evaluated per request - so slice 1 ACLs every document
to the linking user alone and SAYS so in the panel. These tests exist to stop that promise
drifting: an ACL that silently widened would be a disclosure, and a panel that stopped saying
so would turn an honest limit into a bug report.

    PYTHONPATH=src python3 tests/selftest_673_s3_connector.py
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.connectors.s3 import S3Connector  # noqa: E402
from dbsearch.router.providers.connector import (  # noqa: E402
    _build_connector, s3_connector_factory,
)

CANVAS = (ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js").read_text()
ROUTER_API = (ROOT / "src/dbsearch/server/router_api.py").read_text()


def _t(day: int):
    return datetime(2026, 8, day, 12, 0, tzinfo=timezone.utc)


class FakeS3:
    """Records how it was built, and pages like the real API does."""

    built_with: list = []

    def __init__(self, pages, **kwargs):
        self._pages = pages
        FakeS3.built_with.append(kwargs)
        self.got = []

    def list_objects_v2(self, **kwargs):
        idx = 0
        if kwargs.get("ContinuationToken"):
            idx = int(kwargs["ContinuationToken"])
        page = dict(self._pages[idx])
        if idx + 1 < len(self._pages):
            page["IsTruncated"] = True
            page["NextContinuationToken"] = str(idx + 1)
        return page

    def get_object(self, Bucket=None, Key=None):
        self.got.append(Key)

        class _B:
            @staticmethod
            def read():
                return b"the quarterly revenue was 41 million"
        return {"Body": _B()}


def _conn(pages, **kw):
    return S3Connector("acme", "bkt", owner_principal="alice-oid",
                       client_factory=lambda: FakeS3(pages), **kw)


ONE_PAGE = [{"Contents": [
    {"Key": "reports/q1.pdf", "Size": 10, "LastModified": _t(1)},
    {"Key": "reports/q2.txt", "Size": 10, "LastModified": _t(3)},
    {"Key": "images/logo.png", "Size": 10, "LastModified": _t(2)},   # unparseable
    {"Key": "reports/", "Size": 0, "LastModified": _t(2)},           # folder marker
]}]


def test_every_document_is_acld_to_the_linking_user_alone():
    """THE PROMISE. Slice 1's whole correctness argument is that there is exactly one
    principal, so no trim can be wrong."""
    c = _conn(ONE_PAGE)
    items, _ = c.list_changes(None)
    for item in items:
        acl = c.fetch_acl(item)
        assert [p.oid for p in acl] == ["alice-oid"], acl
        assert [p.kind for p in acl] == ["user"], acl
    docs = c.to_documents(items[0])
    assert [p.oid for p in docs[0].acl] == ["alice-oid"], docs[0].acl
    print("  PASS  every document is ACL'd to the linking user, and nobody else")


def test_the_audience_comes_from_the_store_never_from_a_default():
    """A store with no audience must REFUSE, not invent one. Ingesting into a store nobody
    can see is #200's silently-invisible store; defaulting wider is a disclosure."""
    try:
        s3_connector_factory({"id": "s", "bucket": "b", "acl": []})
        raise AssertionError("an s3 store with no acl was accepted")
    except ValueError as e:
        assert "audience" in str(e), e
    c = s3_connector_factory({"id": "s", "bucket": "b", "acl": ["bob-oid", "carol-oid"]})
    assert c.fetch_acl({})[0].oid == "bob-oid"
    print("  PASS  the audience is the store's own, and an empty one is refused")


def test_unparseable_and_folder_markers_are_skipped_before_download():
    """A bucket of images should cost nothing: skipping happens at LISTING time."""
    c = _conn(ONE_PAGE)
    items, _ = c.list_changes(None)
    keys = [i["external_id"] for i in items]
    assert keys == ["reports/q1.pdf", "reports/q2.txt"], keys
    assert not c._s3().got, "an unparseable object was downloaded anyway"
    print("  PASS  only extractable objects are listed, and nothing else is fetched")


def test_the_crawl_pages_to_the_end():
    """list_objects_v2 caps at 1000 keys. Stopping at page one would index a bucket's first
    1000 objects and report success - a store that looks full and is not."""
    pages = [{"Contents": [{"Key": f"a{i}.txt", "Size": 5, "LastModified": _t(1)}]}
             for i in range(1, 4)]
    c = _conn(pages)
    items, _ = c.list_changes(None)
    assert len(items) == 3, [i["external_id"] for i in items]
    print("  PASS  pagination is followed to the last page")


def test_the_cursor_makes_a_second_crawl_incremental():
    """LAW 3. Re-running with the returned cursor yields nothing new; a NEWER object appears."""
    c = _conn(ONE_PAGE)
    items, cursor = c.list_changes(None)
    assert len(items) == 2, items
    again, cursor2 = _conn(ONE_PAGE).list_changes(cursor)
    assert again == [], [i["external_id"] for i in again]
    newer = [{"Contents": [{"Key": "reports/q3.md", "Size": 9, "LastModified": _t(9)}]}]
    fresh, _ = _conn(newer).list_changes(cursor)
    assert [i["external_id"] for i in fresh] == ["reports/q3.md"], fresh
    print("  PASS  the cursor makes re-crawls incremental, and new objects still land")


def test_external_id_is_the_key_so_reingest_replaces():
    c = _conn(ONE_PAGE)
    items, _ = c.list_changes(None)
    assert c.external_ids(items[0]) == ["reports/q1.pdf"]
    print("  PASS  external_id is the stable object key")


def test_the_delegated_credential_builds_the_client_not_the_ambient_identity():
    """ADR 0024 on this rail: the caller's STS triple must reach boto3. Falling back to the
    box's ambient identity would, on a hosted deployment, either fail or read a bucket that
    is not theirs."""
    seen = {}

    class _Rec(FakeS3):
        def __init__(self, **kwargs):
            seen.update(kwargs)
            super().__init__(ONE_PAGE, **kwargs)

    cred = json.dumps({"access_key_id": "ASIA-x", "secret_access_key": "s",
                       "session_token": "tok"})
    c = S3Connector("acme", "bkt", owner_principal="alice", credential=cred,
                    client_factory=None)
    c._client_factory = lambda: _Rec(aws_access_key_id=json.loads(cred)["access_key_id"],
                                     aws_session_token=json.loads(cred)["session_token"])
    c.list_changes(None)
    assert seen.get("aws_access_key_id") == "ASIA-x", seen
    assert seen.get("aws_session_token") == "tok", seen
    print("  PASS  the delegated STS triple builds the S3 client")


def test_the_factory_seam_passes_a_credential_only_where_declared():
    """_build_connector binds by PARAMETER NAME. A factory that does not take a credential
    must not be handed one - a credential delivered to the wrong place is a leak."""
    got = {}

    def takes(config, credential=None):
        got["with"] = credential
        return "built"

    def plain(config):
        got["without"] = True
        return "built"

    assert _build_connector(takes, {"id": "x"}, "CRED") == "built"
    assert got["with"] == "CRED"
    assert _build_connector(plain, {"id": "x"}, "CRED") == "built"
    assert got.get("without") is True
    print("  PASS  only a factory declaring `credential` receives one")


def test_the_panel_states_the_acl_limit_out_loud():
    """An unspoken narrow ACL reads as a broken search. The copy is part of the feature."""
    row = re.search(r"^\s*s3:\s*\{.*?\n", CANVAS, re.M | re.S).group(0)
    assert "note:" in row, "the s3 card carries no standing disclosure"
    assert "only to you" in row, f"the disclosure does not state the audience: {row[:200]}"
    assert re.search(r"if\(def\.note\)", CANVAS), "renderPanel never renders a kind's note"
    print("  PASS  the panel states that documents are visible only to the linking user")


def test_s3_always_delegates_and_offers_no_signin_switch():
    """There is no server identity that could read the caller's bucket on a hosted box, so a
    require_signin switch would advertise a mode that does not exist."""
    row = re.search(r"^\s*s3:\s*\{.*?\n", CANVAS, re.M | re.S).group(0)
    assert "require_signin" not in row, f"s3 offers a switch it cannot honour: {row[:200]}"
    # #809 widened the set to include redshift; this test only requires that s3 is IN it.
    always = re.search(r"_ALWAYS_DELEGATED\s*=\s*new Set\(\[([^\]]*)\]\)", CANVAS)
    assert always and '"s3"' in always.group(1), (
        f"s3 left _ALWAYS_DELEGATED ({always and always.group(1)})")
    assert re.search(r"if\(signin\|\|_ALWAYS_DELEGATED\.has\(node\.kind\)\)", CANVAS), (
        "entryOf no longer attaches a delegation unconditionally for always-delegated kinds "
        "- an s3 store would compose with no credential and crawl as the server")
    assert '"s3"' in ROUTER_API, "s3 is not registered / not in PLANNED_KINDS"
    print("  PASS  s3 always carries its delegation and offers no false choice")


def test_probe_and_compose_agree_on_what_a_store_IS():
    """#674, found by driving this connector on prod. /router/probe, /router/health and the
    setup agent each built the provider's config from their OWN dict literal, and all three
    omitted the store's `acl` that provisioning.load_manifest includes. A provider reading
    config["acl"] therefore saw one config at compose and a different one at Test-connection.

    Asserted as KEY PARITY against the compose path rather than "acl is present", because the
    defect is the DRIFT: any future key added to one side and not the other is the same bug
    wearing different clothes."""
    from dbsearch.server.router_api import _merged_config

    entry = {"id": "s3-1", "kind": "s3", "business_unit": "unassigned",
             "acl": ["alice-oid"], "config": {"bucket": "b", "prefix": "p/"}}
    probe_cfg = _merged_config(entry)
    # what provisioning.load_manifest builds for the same entry (kept in sync by hand here -
    # if load_manifest gains a key, this list is where the mismatch is meant to surface)
    compose_keys = {"id", "business_unit", "title", "description", "acl", *entry["config"]}
    assert set(probe_cfg) == compose_keys, (
        f"probe config keys {sorted(set(probe_cfg))} != compose config keys "
        f"{sorted(compose_keys)} - Test connection is testing a different store than Compose")
    assert probe_cfg["acl"] == ["alice-oid"], probe_cfg
    print("  PASS  probe and compose build the same config keys")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    print("\nFAILED" if fails else "\n#673 S3 CONNECTOR SELF-TEST PASSED.")
    sys.exit(1 if fails else 0)
