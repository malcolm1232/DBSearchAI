"""#815 - a typo'd folder path must fail the probe, not impersonate a healthy empty store.

Found in the #780 prod audit: Test connection on `path: /data/audit-folder-that-does-not-
exist` returned probe "reachable; schema read" and exercise "connected, but no content was
retrieved (the source may be empty or not indexed yet)". The cause is quiet: `rglob` on a
nonexistent root is an EMPTY ITERATOR, so listing a path that is not there is
indistinguishable from listing a directory with nothing in it - and "may be empty" is only
an honest sentence when the directory exists.

The fix lives in FolderConnector.list_changes - the one home that probe, sync and ingest
all pass through - which refuses a root that is not a directory, naming the path. The
control half: an EXISTING empty directory keeps its current verdict (reachable, degraded,
nothing retrieved), because that one really may just be empty.

    PYTHONPATH=src python3 tests/selftest_815_folder_probe_honesty.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
os.environ["DBSEARCH_RATE_LIMIT"] = "0"
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
ALICE = {"X-DBSearch-User": "alice"}


def _health(path: str) -> dict:
    entry = {"id": "audit-folder", "kind": "folder", "mode": "index",
             "business_unit": "hr", "title": "Audit folder", "acl": ["all-staff"],
             "config": {"path": path}}
    r = client.post("/router/health", headers=ALICE, json={"entry": entry})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_nonexistent_path_fails_the_probe_and_names_the_path():
    ghost = "/tmp/dbsearch-815-folder-that-does-not-exist"
    assert not Path(ghost).exists()
    v = _health(ghost)
    assert v["status"] == "failed", (
        f"a typo'd path probed {v['status']!r} - indistinguishable from a healthy empty "
        f"folder: {v}")
    blob = str(v)
    assert ghost in blob, f"the verdict must NAME the missing path so a typo is findable: {v}"


def test_a_file_where_a_directory_should_be_also_fails():
    with tempfile.NamedTemporaryFile(suffix=".txt") as f:
        v = _health(f.name)
        assert v["status"] == "failed", v


def test_an_existing_empty_directory_keeps_its_honest_maybe_empty_verdict():
    """The control: this is the case 'may be empty' was written for, and it must survive."""
    with tempfile.TemporaryDirectory() as d:
        v = _health(d)
        assert v["status"] in ("degraded", "healthy"), (
            f"an existing empty dir must stay reachable (its emptiness is real): {v}")
        probe = [s for s in v["stages"] if s["name"] == "probe"][0]
        assert probe["ok"] is True, v


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
