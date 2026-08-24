"""#575 (second half of ADR 0017): grants survive a deploy.

ADR 0017 s4 named the in-memory grant registry as a known gap - grants did not survive a
restart, the same position `ApiKeyRegistry` held at the time. This is the fix: an optional
write-through Postgres store behind `GrantRegistry`, with the in-memory dict remaining the
ONLY thing per-request principal expansion reads (grants.py, grant_store.py).

What these tests pin, beyond "it saves to Postgres":

  - the restart story: a fresh registry over the same store still expands a live grant
  - revoke reaches the store, so a rebuilt registry does not resurrect a revoked grant
  - drop_for_documents (Task 6's retention sweep) clears memory AND the store, and counts
  - a store that cannot be reached at construction never blocks boot - starts empty
  - a store that cannot be reached on CREATE never turns a working share into an error
    (create is best-effort, on purpose - see grants.py's module + `create` docstrings)
  - a store that cannot be reached on REVOKE fails closed: the caller learns the revoke did
    not take effect, and the grant stays expandable in this process too (#575 review, Finding
    1 - this is the opposite direction from `create`, and deliberately so: a resurrected
    grant principal IS the authorization decision, unlike a resurrected refresh token, which
    is still filtered by a live LAW 2 group check on every request)
  - a revoke that DID succeed against the store must never be resurrected by a later restart
  - drop_for_documents finishes and clears memory for every match even when the store call
    raises for one of them (#575 review, Finding 2) - reconciled against Finding 1 in the
    method's own docstring: unlike revoke, this only ever runs over documents that are
    themselves being deleted, so a row it fails to purge is an orphan, not a live exposure
  - a naive datetime coming off a row still round-trips through Grant.is_live() without
    raising - grants.py's `_now()` is timezone-AWARE, so a naive `expires_at` would blow up
    the very first comparison a live request makes
  - grant_document's (server/app.py) rollback of a share that never took effect always
    clears local state and never masks the request's real error behind a store failure
    (#575 review, Finding A - `revoke`'s new fail-closed stance is right for reversing a
    share that DID work, and wrong for cleaning up one that never did)
  - the existing no-store default (tests/selftest_538_document_grants.py) is untouched

    PYTHONPATH=src python3 tests/selftest_575_durable_grants.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.api.grants import Grant, GrantRegistry  # noqa: E402
from dbsearch.server.grant_store import (  # noqa: E402
    InMemoryGrantStore, PgGrantStore, _row_to_grant,
)


def test_default_registry_still_works_with_no_store():
    """The #538 shape, untouched: no `store` argument at all."""
    r = GrantRegistry()
    g = r.create(doc_external_id="doc-1", tenant_id="t", grantee_oid="bob", granted_by="alice")
    assert g.principal in r.live_principals_for("bob")


def test_grants_survive_a_registry_rebuild():
    """The restart story: a new registry over the same store still expands the grant."""
    store = InMemoryGrantStore()
    r1 = GrantRegistry(store=store)
    g = r1.create(doc_external_id="doc-1", tenant_id="t", grantee_oid="bob", granted_by="alice")
    r2 = GrantRegistry(store=store)          # "after the deploy"
    assert g.principal in r2.live_principals_for("bob")


def test_revoke_reaches_the_store():
    store = InMemoryGrantStore()
    r1 = GrantRegistry(store=store)
    g = r1.create(doc_external_id="doc-1", tenant_id="t", grantee_oid="bob", granted_by="alice")
    r1.revoke(g.grant_id, "alice")
    assert GrantRegistry(store=store).live_principals_for("bob") == []


def test_drop_for_documents_clears_both_layers():
    store = InMemoryGrantStore()
    r = GrantRegistry(store=store)
    r.create(doc_external_id="doc-1", tenant_id="t", grantee_oid="bob", granted_by="alice")
    r.create(doc_external_id="doc-2", tenant_id="t", grantee_oid="bob", granted_by="alice")
    assert r.drop_for_documents({"doc-1"}) == 1
    assert len(GrantRegistry(store=store).live_principals_for("bob")) == 1


def test_drop_for_documents_counts_only_matches_and_is_idempotent():
    store = InMemoryGrantStore()
    r = GrantRegistry(store=store)
    r.create(doc_external_id="doc-1", tenant_id="t", grantee_oid="bob", granted_by="alice")
    assert r.drop_for_documents({"doc-does-not-exist"}) == 0
    assert r.drop_for_documents({"doc-1"}) == 1
    assert r.drop_for_documents({"doc-1"}) == 0, "a second sweep over the same id must not double count"


def test_a_dead_store_never_blocks_boot():
    class Dead:
        def load_all(self):
            raise RuntimeError("down")
    r = GrantRegistry(store=Dead())
    assert r.live_principals_for("bob") == []


def test_create_never_blocks_on_a_dead_store():
    """A store that cannot be reached on write must not turn a working share into an error -
    same stance TokenVault takes on a down secrets store."""
    class DeadWrite:
        def load_all(self):
            return []

        def save(self, g):
            raise RuntimeError("down")
    r = GrantRegistry(store=DeadWrite())
    g = r.create(doc_external_id="doc-1", tenant_id="t", grantee_oid="bob", granted_by="alice")
    assert g.principal in r.live_principals_for("bob"), "the in-process share must still work"


def test_revoke_fails_closed_when_the_store_delete_fails():
    """Finding 1 (CRITICAL): revoke does NOT take create's best-effort stance. A store
    outage on revoke must surface to the caller (raise) and must NOT clear memory - clearing
    memory anyway would leave the row alive in the store, ready to be resurrected as a live,
    expandable principal by the next restart's hydration. That is the exact bug this fixes:
    the old code deleted memory unconditionally and swallowed the store failure, so a revoke
    during a transient outage LOOKED like it worked (200 OK, memory clear) while silently
    restoring the grantee's access on the next deploy."""
    class DeadDeleteStore:
        def load_all(self):
            return []

        def save(self, g):
            pass

        def delete(self, grant_id):
            raise RuntimeError("down")
    r = GrantRegistry(store=DeadDeleteStore())
    g = r.create(doc_external_id="doc-1", tenant_id="t", grantee_oid="bob", granted_by="alice")
    try:
        r.revoke(g.grant_id, "alice")
        raise AssertionError("revoke must surface the store failure, not swallow it")
    except RuntimeError:
        pass
    assert g.principal in r.live_principals_for("bob"), \
        "a revoke that failed against the store must leave the grant live in this process too"


def test_a_successful_revoke_is_never_resurrected_by_a_later_restart():
    """The positive case for Finding 1: once revoke reports success, the store delete really
    happened, so a registry rebuilt over the same store (the restart story) must not see the
    grant again."""
    store = InMemoryGrantStore()
    r1 = GrantRegistry(store=store)
    g = r1.create(doc_external_id="doc-1", tenant_id="t", grantee_oid="bob", granted_by="alice")
    r1.revoke(g.grant_id, "alice")                # no exception - the store delete succeeded
    r2 = GrantRegistry(store=store)                # "after the deploy"
    assert r2.live_principals_for("bob") == [], "a genuinely revoked grant must not come back"


def test_drop_for_documents_finishes_and_clears_memory_past_one_store_failure():
    """Finding 2: a sweep is unattended, so one bad row must not abort the batch - and unlike
    revoke (Finding 1), a grant on a document that is itself being deleted is not a live
    authorization exposure if its store row fails to purge, so memory clears for every match
    regardless (see drop_for_documents's own docstring for the full reasoning)."""
    class FlakyDeleteStore:
        def __init__(self):
            self.rows = {}
            self.fail_id = None

        def load_all(self):
            return list(self.rows.values())

        def save(self, g):
            self.rows[g.grant_id] = g

        def delete(self, grant_id):
            if grant_id == self.fail_id:
                raise RuntimeError("down")
            self.rows.pop(grant_id, None)

    store = FlakyDeleteStore()
    r = GrantRegistry(store=store)
    g1 = r.create(doc_external_id="doc-1", tenant_id="t", grantee_oid="bob", granted_by="alice")
    g2 = r.create(doc_external_id="doc-2", tenant_id="t", grantee_oid="carol", granted_by="alice")
    g3 = r.create(doc_external_id="doc-3", tenant_id="t", grantee_oid="dave", granted_by="alice")
    store.fail_id = g2.grant_id                    # the middle item's store delete will raise

    removed = r.drop_for_documents({"doc-1", "doc-2", "doc-3"})

    assert removed == 3, "the loop must finish and count every matching grant, store failure or not"
    assert g1.principal not in r.live_principals_for("bob")
    assert g2.principal not in r.live_principals_for("carol"), \
        "memory must clear for the failed item too - it is an orphaned store row, not a live grant"
    assert g3.principal not in r.live_principals_for("dave")


def test_expires_at_none_round_trips():
    """A grant with no expiry must not turn into one that mysteriously expires."""
    store = InMemoryGrantStore()
    r1 = GrantRegistry(store=store)
    g = r1.create(doc_external_id="doc-1", tenant_id="t", grantee_oid="bob", granted_by="alice")
    assert g.expires_at is None
    r2 = GrantRegistry(store=store)
    assert g.principal in r2.live_principals_for("bob")


def test_load_all_reconstructs_every_field():
    store = InMemoryGrantStore()
    r1 = GrantRegistry(store=store)
    g = r1.create(doc_external_id="doc-9", tenant_id="tenant-9", grantee_oid="bob",
                  granted_by="alice", expires_in_days=7)
    [reloaded] = store.load_all()
    assert reloaded.grant_id == g.grant_id
    assert reloaded.doc_external_id == "doc-9"
    assert reloaded.tenant_id == "tenant-9"
    assert reloaded.grantee_oid == "bob"
    assert reloaded.granted_by == "alice"
    assert reloaded.expires_at == g.expires_at


class _FakeSqlTable:
    """Column-name-aware stand-in for a Postgres connection, built to expose exactly the bug
    review Finding A named: every existing PgGrantStore test used `InMemoryGrantStore`,
    which stores `Grant` objects directly and never goes anywhere near `save`'s param tuple
    or `load_all`'s SELECT text - so a column swap between `created_at` and `conv_id` in
    either of those (grant_store.py) would leave the whole suite green while a real deploy
    booted with zero grants hydrated (`_aware()` called on a string raises AttributeError,
    `GrantRegistry.__init__` swallows it and starts empty - the exact silent failure this
    guards).

    This does NOT reimplement grant_store.py's logic - it reads the REAL SQL text
    `PgGrantStore` emits (the actual `INSERT INTO ... (col, col, ...)` and
    `SELECT col, col, ... FROM` strings) and binds `params` to those column names
    positionally, the same job Postgres itself would do. A transposition of two columns in
    either the emitted column list or the params tuple shows up here exactly as it would
    against a live database - no live database required."""

    def __init__(self, rows: dict) -> None:
        self._rows = rows           # shared across every _FakeConn built from this table

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("CREATE TABLE") or s.startswith("ALTER TABLE"):
            return self
        if s.startswith("INSERT INTO"):
            cols = [c.strip() for c in s[s.index("(") + 1:s.index(")")].split(",")]
            assert len(cols) == len(params), (cols, params)
            self._rows[dict(zip(cols, params))["grant_id"]] = dict(zip(cols, params))
            return self
        if s.startswith("SELECT"):
            names = [c.strip() for c in s[len("SELECT"):s.index("FROM")].split(",")]
            self._fetched = [tuple(row[n] for n in names) for row in self._rows.values()]
            return self
        if s.startswith("DELETE"):
            self._rows.pop(params[0], None)
            return self
        raise AssertionError(f"_FakeSqlTable does not understand: {s}")

    def fetchall(self):
        return self._fetched


def test_pg_store_round_trips_a_real_non_null_conv_id_via_the_actual_sql_text():
    """#600 review Finding A. A NON-NULL, distinctive `conv_id` ("conv-c1-distinct") going
    through PgGrantStore's real save()/load_all() SQL - not InMemoryGrantStore, which every
    other test in this file uses and which cannot catch a column-order bug because it never
    touches a column list at all."""
    rows: dict = {}
    store = PgGrantStore("postgresql://unused/fake")
    store._conn = lambda: _FakeSqlTable(rows)          # stand in for a live Postgres

    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    g = Grant(grant_id="g-600", doc_external_id="doc-1", tenant_id="t",
              grantee_oid="bob", granted_by="alice", expires_at=None,
              created_at=created, conv_id="conv-c1-distinct")
    store.save(g)
    [reloaded] = store.load_all()
    assert reloaded.conv_id == "conv-c1-distinct", (
        "conv_id did not round-trip through the real INSERT/SELECT column lists")
    assert reloaded.created_at == created, (
        "created_at came back wrong - conv_id and created_at may have swapped columns")
    assert reloaded.grant_id == "g-600" and reloaded.doc_external_id == "doc-1"


def test_a_naive_expires_at_from_a_row_does_not_raise_on_is_live():
    """The exact failure this brief calls out: `Grant.is_live()` (grants.py) compares
    `expires_at` against an AWARE `datetime.now(timezone.utc)`. A naive datetime coming back
    off a row - e.g. a driver/environment that does not preserve tzinfo - would raise
    TypeError on that comparison, turning a live grant's next request into a 500 instead of a
    read. `_row_to_grant` must normalize a naive value to UTC before it ever reaches Grant."""
    future_naive = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    past_naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    created_naive = datetime.now(timezone.utc).replace(tzinfo=None)

    live = _row_to_grant(("g1", "doc-1", "t", "bob", "alice", future_naive, created_naive, None))
    assert live.expires_at.tzinfo is not None, "a naive expires_at must come back aware"
    assert live.created_at.tzinfo is not None, "a naive created_at must come back aware"
    assert live.is_live() is True                # must not raise, and must be correct

    expired = _row_to_grant(("g2", "doc-1", "t", "bob", "alice", past_naive, created_naive, None))
    assert expired.is_live() is False


def test_an_aware_row_is_passed_through_unchanged():
    dt = datetime.now(timezone.utc) + timedelta(days=1)
    g = _row_to_grant(("g3", "doc-1", "t", "bob", "alice", dt, dt, None))
    assert g.expires_at == dt
    assert g.expires_at.tzinfo is timezone.utc or g.expires_at.utcoffset() == timedelta(0)


def test_no_expiry_row_stays_none():
    dt = datetime.now(timezone.utc)
    g = _row_to_grant(("g4", "doc-1", "t", "bob", "alice", None, dt, None))
    assert g.expires_at is None
    assert g.is_live() is True


def test_store_type_is_never_a_read_path():
    """Sanity that the design brief pins in words: after construction, `live_principals_for`
    must work with NO store attached (or one that would raise on every call), because the
    in-memory dict is the only thing a live request touches."""
    class ExplodesOnEveryCall:
        def load_all(self):
            return []

        def save(self, g):
            pass

        def delete(self, grant_id):
            pass

        def __getattr__(self, name):
            raise AssertionError(f"unexpected store call: {name}")
    r = GrantRegistry(store=ExplodesOnEveryCall())
    g = r.create(doc_external_id="doc-1", tenant_id="t", grantee_oid="bob", granted_by="alice")
    for _ in range(50):
        assert g.principal in r.live_principals_for("bob")


def test_failed_share_rollback_never_masks_the_real_error_or_leaks_a_grant():
    """#575 review, Finding A. `grant_document` (server/app.py) creates a grant, then adds
    its principal to the target document's ACL; if that add touches zero chunks - the TOCTOU
    window where the document is gone by the time the write runs, even though `_may_share`
    just confirmed it exists - it rolls the grant back and reports the honest error, 404 "no
    such document". Forced here deterministically by monkeypatching `add_doc_principals` to
    return 0, since the real race is not reproducible on demand.

    `revoke`'s new fail-closed stance (Finding 1) must not leak into that rollback: a broken
    store during the rollback must not turn the 404 into a 500 (masking the actionable
    error), and must not leave the dead grant sitting in memory, expandable by the grantee,
    for the rest of the process's life."""
    import time as _time

    os.environ.setdefault("SELFHOST_BACKEND", "memory")
    from fastapi.testclient import TestClient  # local import - keep this file import-light

    from dbsearch.server import user_auth as _user_auth
    from dbsearch.server.app import _edition as _ed
    from dbsearch.server.app import app as _app

    client = TestClient(_app)
    auth_vars = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")
    saved_env = {k: os.environ.get(k) for k in auth_vars}
    for k in auth_vars:
        os.environ.pop(k, None)
    os.environ.update({"AUTH_TENANT_ID": "tid-1", "AUTH_CLIENT_ID": "cid",
                       "AUTH_CLIENT_SECRET": "sec"})

    alice, bob = "aaaaaaaa-0000-0000-0000-000000000575", "bbbbbbbb-0000-0000-0000-000000000575"

    def cookie(oid):
        return {_user_auth.COOKIE: _user_auth.sign_session(
            {"oid": oid, "tid": "tid-1", "exp": int(_time.time()) + 3600})}

    class DownDeleteStore:
        """A store that is reachable for everything except delete - the rollback's failure
        mode this finding is about."""
        def load_all(self):
            return []

        def save(self, g):
            pass

        def delete(self, grant_id):
            raise RuntimeError("store down")

    doc_id = "doc-575-rollback"
    old_store = _ed.grant_registry._store
    old_add = _ed.index.add_doc_principals
    try:
        r = client.post("/ingest", cookies=cookie(alice), json={
            "external_id": doc_id, "title": "t", "text": "secret content",
            "acl": [alice], "uri": f"upload://{doc_id}.txt"})
        assert r.status_code == 200, r.text[:200]

        _ed.grant_registry._store = DownDeleteStore()
        _ed.index.add_doc_principals = lambda *a, **kw: 0     # force the TOCTOU branch

        resp = client.post(f"/documents/{doc_id}/grants", cookies=cookie(alice),
                           json={"grantee_oid": bob})
        assert resp.status_code == 404, (
            f"a rollback store failure must not mask the real error: "
            f"got {resp.status_code} {resp.text[:200]}")
    finally:
        _ed.index.add_doc_principals = old_add
        _ed.grant_registry._store = old_store
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    assert _ed.grant_registry.live_principals_for(bob) == [], \
        "the failed share must not leave a dead grant expandable for its would-be grantee"


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
