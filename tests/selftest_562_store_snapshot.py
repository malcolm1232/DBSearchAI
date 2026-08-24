"""#562 - Admin must be able to see the DATABASES, not just the documents.

The Admin console reported on the document plane (sources, per-doc ACLs, segment previews)
and had no notion of the composed stores at all, so every Azure SQL / Postgres / Cosmos node
on the canvas was invisible there. "What is actually in my deployment" had no answer surface.

This adds ONE read endpoint - GET /router/stores/{id}/schema - and pins the two things that
make it safe to exist:

  1. VISIBILITY. It resolves the store through the caller's own visible_stores(), so a store
     the caller cannot enumerate answers 404 - the same answer a store that does not exist
     gets. Distinguishing the two is an existence probe, which is the exact leak the catalog's
     hereditary trim (gate #1) was built to close.
  2. HONEST COUNTS. Row counts come from an engine that can actually count. An engine that
     cannot reports null, NOT zero. #392's rule: unknown is not the same as empty, and a
     fabricated count on a store an operator is using to decide something is worse than no
     count at all. NL2SQL "how many records" was rejected for this - it answers for one table
     and guesses which.

    PYTHONPATH=src python3 tests/selftest_562_store_snapshot.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("SELFHOST_BACKEND", "memory")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.structured import SqliteEngine  # noqa: E402


def _engine():
    return SqliteEngine.from_tables({
        "employees": {"columns": ["id", "name", "team"],
                      "rows": [[1, "Ada", "eng"], [2, "Grace", "eng"], [3, "Alan", "ops"]]},
        "empty_table": {"columns": ["id"], "rows": []},
    })


def test_an_engine_that_can_count_reports_exact_counts():
    counts = _engine().row_counts()
    assert counts == {"employees": 3, "empty_table": 0}, counts


def test_an_empty_table_counts_zero_rather_than_going_missing():
    """0 is a real answer here and must not be confused with 'unknown' below."""
    assert _engine().row_counts()["empty_table"] == 0


def test_an_engine_that_cannot_count_says_unknown_not_zero():
    """The default. A provider with no row_counts() must not silently report an empty database
    - an operator reading '0 rows' on a full warehouse makes a wrong decision confidently."""
    from dbsearch.router.structured import SqlEnginePort

    class Blind(SqlEnginePort):
        def schema(self):
            return [{"table": "t", "columns": [{"name": "c", "type": "TEXT"}]}]

        def execute(self, sql, credential=None, principal=None):
            return [], []

    assert Blind().row_counts() is None, \
        "an engine with no counting capability must return None (unknown), never {} or 0"


def test_the_schema_endpoint_404s_a_store_the_caller_cannot_see():
    """The visibility gate, with TWO identities - one identity cannot tell a working trim
    from an absent one."""
    from dbsearch.router.catalog import (BUSINESS_UNIT, STORE, TENANT, CatalogNode,
                                         StoreCatalog)

    cat = StoreCatalog()
    cat.register(CatalogNode("acme", TENANT, None, acl=["alice", "bob"]))
    cat.register(CatalogNode("hr", BUSINESS_UNIT, "acme", acl=["alice"]))
    cat.register(CatalogNode("hr-db", STORE, "hr", acl=["alice"]))

    alice = [n.id for n in cat.visible_stores(["alice"])]
    bob = [n.id for n in cat.visible_stores(["bob"])]
    assert alice == ["hr-db"], alice
    assert bob == [], f"bob can enumerate a store under a business unit he cannot see: {bob}"


def test_the_endpoint_exists_and_is_scoped():
    from dbsearch.server.app import app  # noqa: E402

    # #696: the router API is mounted with include_router(), which FastAPI resolves lazily,
    # so a flat walk of app.routes no longer sees /router/* at all.
    from _route_walk import route_paths
    routes = route_paths(app.routes)
    assert "/router/stores/{store_id}/schema" in routes, \
        "the store-schema endpoint is not mounted"
    src = (ROOT / "src/dbsearch/server/router_api.py").read_text()
    body = src.split('@api.get("/stores/{store_id}/schema")', 1)[1].split("@api.", 1)[0]
    assert "Depends(scoped)" in body, (
        "the schema endpoint does not take a RequestScope - #340 is the precedent: an endpoint "
        "that picks its own catalog can pair the demo catalog with the live identity")
    assert "visible_stores" in body, \
        "the endpoint does not resolve the store through the caller's visible set"
    assert "404" in body, "a store the caller cannot see must 404, not 403 - 403 confirms it exists"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
