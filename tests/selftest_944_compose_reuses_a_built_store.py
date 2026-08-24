"""#944 - composing an UNCHANGED store must not throw its index away and re-crawl.

FOUND ON PROD 260823. The owner noticed the gdrive node flash red on every trip from /ask to
Connectors and asked whether the backend was actually doing anything. It was: the canvas calls
composeUp() on every mount, and compose called build_as, which built a brand-new empty
InMemoryIndex and submitted `start_sync(force_new=True)`. Measured on the live box - the job
compose submitted reported `docs_done 2, docs_total 2, docs_skipped 0`. Every file re-listed,
re-downloaded from the Drive API, re-extracted, re-embedded, re-indexed. Per page visit.

The old comment on that line ("a rebuilt store needs its OWN crawl") was CORRECT, and that is
why the fix is not to drop the crawl: an empty index with a delta crawl is a store that answers
nothing. The fix is to stop rebuilding a store whose recipe has not changed, so there is no
empty index to fill.

WHAT MUST STILL REBUILD, and these are the tests that make this safe rather than merely fast:
  - a store whose config CHANGED (a new folder link is a different folder)
  - a store whose acl changed (the audience is stamped into every chunk at ingest)
  - a store whose last crawl FAILED - reusing that strands it empty forever
  - a store this process has never built (a restart: `_stores` is empty, so this is automatic)

  PYTHONPATH=src python3 tests/selftest_944_compose_reuses_a_built_store.py
"""
import os
import sys
import time
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tempfile  # noqa: E402

from dbsearch.router import ConnectorStoreProvider, folder_connector_factory  # noqa: E402

ROOT = None


def _folder(n=2):
    global ROOT
    ROOT = Path(tempfile.mkdtemp(prefix="dbse-944-"))
    (ROOT / "all-staff").mkdir()
    for i in range(n):
        (ROOT / "all-staff" / f"doc{i}.txt").write_text(f"document number {i} about ospreys")
    return ROOT


def _provider():
    return ConnectorStoreProvider("folder", folder_connector_factory)


def _cfg(path, acl=("all-staff",), sid="legal-archive"):
    return {"id": sid, "kind": "folder", "mode": "index", "business_unit": "legal",
            "acl": list(acl), "title": "Legal archive", "description": "archive",
            "config": {"path": str(path)}, "path": str(path)}


def _await(provider, sid, timeout=20):
    """Wait for the store's crawl to settle, so a count is a settled count."""
    end = time.time() + timeout
    while time.time() < end:
        d = provider.sources.get(sid)
        if d.status not in ("syncing",):
            return d
        time.sleep(0.05)
    return provider.sources.get(sid)


def _docs_seen(provider, sid):
    """How many documents this provider's crawl has processed for `sid`, cumulatively."""
    return provider.sources.get(sid).doc_count


def test_a_second_compose_of_the_same_store_does_not_recrawl():
    """THE DEFECT. Two composes, identical config, and the second must not re-pay for the
    library. Asserted on the STORE OBJECT: build_as returning the same instance is what
    proves the index was not thrown away, and the index is the thing whose loss forced the
    re-crawl."""
    p = _provider()
    path = _folder()
    first = p.build_as(_cfg(path))
    _await(p, "legal-archive")
    second = p.build_as(_cfg(path))
    assert second is first, (
        "compose rebuilt an unchanged store, which discards its index and forces a full "
        "re-crawl - the prod defect (docs_skipped 0 on every page visit)")


def test_the_reused_store_still_answers():
    """A reuse that returned a store which cannot answer would be a worse bug than the one it
    fixes. The point of not rebuilding is that the CONTENT survives."""
    p = _provider()
    path = _folder()
    p.build_as(_cfg(path))
    _await(p, "legal-archive")
    store = p.build_as(_cfg(path))
    ev = store.retrieve(store.authorize("all-staff"), "ospreys", top_k=5)
    assert ev, "the reused store retrieved nothing - its index did not survive"


def test_a_changed_config_still_rebuilds():
    """CONTROL. A different folder is a different source, and reusing the old store would
    serve the OLD folder's content under the new configuration - silently."""
    p = _provider()
    a, b = _folder(), _folder(3)
    first = p.build_as(_cfg(a))
    _await(p, "legal-archive")
    second = p.build_as(_cfg(b))
    assert second is not first, "a store pointed at a new folder was not rebuilt"


def test_a_changed_acl_still_rebuilds():
    """CONTROL, and the one that matters for LAW 2. The audience is stamped into every chunk
    at ingest time, so an acl change that does not re-crawl leaves the OLD audience on the
    stored chunks - a permission change the product reports as applied and has not applied."""
    p = _provider()
    path = _folder()
    first = p.build_as(_cfg(path, acl=("all-staff",)))
    _await(p, "legal-archive")
    second = p.build_as(_cfg(path, acl=("deal-team",)))
    assert second is not first, (
        "an acl change did not rebuild, so the old audience stays stamped on every chunk")


def test_a_store_whose_crawl_failed_is_rebuilt():
    """CONTROL. Reusing a store whose crawl errored strands it empty forever - the user's
    retry (recompose) would become a no-op, which is the #941 family again."""
    p = _provider()
    path = _folder()
    first = p.build_as(_cfg(path))
    _await(p, "legal-archive")
    p.sources.get("legal-archive").status = "error"
    second = p.build_as(_cfg(path))
    assert second is not first, "a failed store was reused, so a recompose cannot recover it"


if __name__ == "__main__":
    fails = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                fails.append(name)
                print(f"FAIL {name}\n     {exc}")
            except Exception as exc:
                fails.append(name)
                print(f"FAIL {name}\n     {type(exc).__name__}: {exc}")
    print(f"\n{'FAILED' if fails else 'PASSED'}: {len(fails)} failure(s)")
    sys.exit(1 if fails else 0)
