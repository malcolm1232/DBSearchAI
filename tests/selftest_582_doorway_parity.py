"""#582 - the SQL doorway says the same thing as the in-process one (ADR 0019 D3).

Two backends now express one partition rule: `ReadScope.allows` in Python (local adapter)
and `_doorway_sql` in SQL (pgvector). Two expressions of one rule is exactly the shape
that drifts, and a drift here is a silent retrieval bug on ONE backend only - green tests
on the laptop, a share that returns nothing on prod. So they are pinned against a single
truth table.

Needs no Postgres: the emitted predicate is evaluated the way Postgres would evaluate it.

    PYTHONPATH=src python3 tests/selftest_582_doorway_parity.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.vectordb.pgvector import _doorway_sql  # noqa: E402
from dbsearch.ports.base import ReadScope  # noqa: E402

SHARED = ReadScope("acct:bob", frozenset({("acct:alice", "doc-1")}))
TWO = ReadScope("acct:bob", frozenset({("acct:alice", "doc-1"), ("home", "doc-9")}))

# (scope, chunk tenant, chunk doc, may this chunk be looked at?)
CASES = [
    (ReadScope("acct:bob"), "acct:bob", "doc-1", True),    # own partition
    (ReadScope("acct:bob"), "acct:alice", "doc-1", False),  # someone else's, no share
    (SHARED, "acct:alice", "doc-1", True),                  # the shared document
    (SHARED, "acct:alice", "doc-2", False),                 # NOT a skeleton key
    (SHARED, "acct:carol", "doc-1", False),                 # same doc id, other partition
    (SHARED, "acct:bob", "doc-7", True),                    # own partition still open
    (TWO, "home", "doc-9", True),
    (TWO, "home", "doc-1", False),                          # pairs, not a cross product
]


def _sql_says(scope, tenant, doc) -> bool:
    """Evaluate the emitted predicate as Postgres would, without Postgres.

    This DERIVES its behaviour from the emitted SQL text rather than reimplementing the
    rule in Python. An earlier version did the latter and was worthless: mutating the SQL
    join condition from `d.t = tenant_id AND d.x = doc_external_id` down to `d.t =
    tenant_id` - turning every doorway pair into a whole-partition key, the exact
    over-share this design exists to prevent - left all three tests PASSING, because the
    test never read the string it was supposed to be checking. Caught by mutation testing;
    do not "simplify" this back.
    """
    sql, params = _doorway_sql(scope)
    if not scope.doorway:
        assert "unnest" not in sql, "an empty doorway must emit the original single predicate"
        return tenant == params[0]
    assert "unnest" in sql, sql
    own, tenants, docs = params[0], params[1], params[2]
    if tenant == own:
        return True
    # Read the ACTUAL join condition out of the emitted SQL.
    joined = " ".join(sql.split())
    inner = joined[joined.index("unnest"):]
    on_tenant = "d.t = tenant_id" in inner
    on_doc = "d.x = doc_external_id" in inner
    assert on_tenant and on_doc, (
        "the doorway must match on BOTH tenant and document - matching on one alone turns "
        f"every share into a whole-partition grant. Emitted: {inner}")
    return any(t == tenant and d == doc for t, d in zip(tenants, docs))


def test_sql_and_in_process_predicates_agree():
    for scope, tenant, doc, expected in CASES:
        assert scope.allows(tenant, doc) is expected, ("python", scope, tenant, doc)
        assert _sql_says(scope, tenant, doc) is expected, ("sql", scope, tenant, doc)


def test_empty_doorway_keeps_the_original_query_shape():
    """The common case must not pay for the feature: same predicate, same plan, same
    use of the tenant index as before #582."""
    sql, params = _doorway_sql(ReadScope("acct:bob"))
    assert sql.strip() == "tenant_id = %s", sql
    assert params == ["acct:bob"], params


def test_the_parameter_count_does_not_grow_with_the_number_of_shares():
    """Two parallel arrays, not one placeholder per pair - a heavily-shared workspace
    must not build an unbounded statement."""
    big = ReadScope("acct:bob", frozenset({("acct:alice", f"doc-{i}") for i in range(500)}))
    _sql, params = _doorway_sql(big)
    assert len(params) == 3, len(params)
    assert len(params[1]) == 500 and len(params[2]) == 500


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
