"""#582 - the doorway is built server-side from live grants only (ADR 0019 D3).

`live_principals_for` already existed for the ACL side of a grant. The doorway needs the
other half of the same record - which partition the document is in and which document it
is - so this is its sibling, with the SAME per-request expiry evaluation. That shared
freshness is what keeps revocation instantaneous and keeps expiry sweeper-free: the
doorway cannot outlive the grant that opened it.

    PYTHONPATH=src python3 tests/selftest_582_request_scope.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.api.grants import GrantRegistry  # noqa: E402


def _pairs(registry, oid):
    return {(g.tenant_id, g.doc_external_id) for g in registry.live_grants_for(oid)}


def test_live_grants_for_returns_only_this_persons_grants():
    r = GrantRegistry()
    r.create(doc_external_id="doc-1", tenant_id="acct:alice",
             grantee_oid="bob", granted_by="alice")
    r.create(doc_external_id="doc-2", tenant_id="acct:alice",
             grantee_oid="carol", granted_by="alice")
    assert _pairs(r, "bob") == {("acct:alice", "doc-1")}
    assert _pairs(r, "carol") == {("acct:alice", "doc-2")}


def test_a_revoked_grant_leaves_the_doorway_immediately():
    """No sweep, no window: the next request simply builds a doorway without it."""
    r = GrantRegistry()
    g = r.create(doc_external_id="doc-1", tenant_id="acct:alice",
                 grantee_oid="bob", granted_by="alice")
    assert _pairs(r, "bob") == {("acct:alice", "doc-1")}
    r.revoke(g.grant_id, "alice")
    assert r.live_grants_for("bob") == []


def test_an_expired_grant_is_not_in_the_doorway():
    r = GrantRegistry()
    r.create(doc_external_id="doc-1", tenant_id="acct:alice", grantee_oid="bob",
             granted_by="alice", expires_in_days=-1)
    assert r.live_grants_for("bob") == []


def test_the_doorway_carries_the_grantors_partition_not_the_grantees():
    """The pair must name where the DOCUMENT lives. Naming the grantee's own partition
    would make the doorway a no-op, which is the bug it exists to fix."""
    r = GrantRegistry()
    r.create(doc_external_id="doc-1", tenant_id="acct:alice",
             grantee_oid="bob", granted_by="alice")
    (tenant, _doc), = _pairs(r, "bob")
    assert tenant == "acct:alice", tenant


def test_nobody_gets_a_doorway_from_an_empty_oid():
    r = GrantRegistry()
    r.create(doc_external_id="doc-1", tenant_id="acct:alice",
             grantee_oid="bob", granted_by="alice")
    assert r.live_grants_for("") == []


def test_live_grants_for_agrees_with_live_principals_for():
    """Two views of one record set. If they ever disagree, a caller would hold a grant
    principal with no doorway (reads nothing) or a doorway with no principal (looks at a
    document the ACL then refuses) - both silent."""
    r = GrantRegistry()
    r.create(doc_external_id="doc-1", tenant_id="acct:alice",
             grantee_oid="bob", granted_by="alice")
    r.create(doc_external_id="doc-2", tenant_id="home", grantee_oid="bob",
             granted_by="dave", expires_in_days=-1)          # expired: in neither view
    assert len(r.live_grants_for("bob")) == len(r.live_principals_for("bob")) == 1


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
