"""Phase G (#130) — ConnectionTest orchestrator + HealthVerdict self-test.
Run: python3 tests/selftest_router_health.py

Covers the mode-agnostic orchestration: probe -> build -> exercise -> teardown(finally)
-> verdict, and the three-tier status mapping (healthy | degraded | failed). Strategies are
faked here; the real per-mode strategies get their own exercises.
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.evidence import CHUNK, Evidence  # noqa: E402
from dbsearch.router.health import (  # noqa: E402
    ConnectionTest, HealthVerdict, StageResult, default_strategies,
)
from dbsearch.router.providers.local import LocalIndexProvider  # noqa: E402
from dbsearch.router.structured import FederatedSqlStore, SqliteEngine  # noqa: E402
from dbsearch.router.provider import ProviderRegistry, StoreProviderPort  # noqa: E402
from dbsearch.router.store import AccessContext, INDEXED, StoreProfile  # noqa: E402

ENTRY = {"kind": "fake", "mode": "index", "id": "s", "config": {}}


class _FakeStore:
    def __init__(self) -> None:
        self.torn_down = False

    def authorize(self, user_oid: str) -> AccessContext:
        return AccessContext(user_oid=user_oid, principals=["p"])

    def retrieve(self, access, question, top_k=5):
        return [Evidence(store_id="s", business_unit="", kind=CHUNK, content="hit")]


class _FakeProvider(StoreProviderPort):
    kind = "fake"
    modes = ("index",)

    def probe(self, config):
        return StoreProfile(store_id="s", title="s", description="", kind=INDEXED)

    def build(self, config):
        return _FakeStore()


class _UnreachableProvider(_FakeProvider):
    def probe(self, config):
        raise ConnectionError("host down")


def _registry(provider):
    reg = ProviderRegistry()
    reg.register(provider)
    return reg


class _HealthyStrategy:
    def exercise(self, store, profile, access):
        return StageResult(name="exercise", ok=True, ms=1, detail="retrieved 1")

    def teardown(self, store):
        store.torn_down = True
        return StageResult(name="teardown", ok=True, ms=0, detail="clean")


class _RoundTripBlockedStrategy:
    """Reachable, but the round-trip could not complete under the caller's identity."""
    def exercise(self, store, profile, access):
        return StageResult(name="exercise", ok=False, ms=1, detail="no readable rows")

    def teardown(self, store):
        return None


class _ThrowingStrategy:
    def __init__(self) -> None:
        self.torn_down = False

    def exercise(self, store, profile, access):
        raise RuntimeError("boom mid-exercise")

    def teardown(self, store):
        self.torn_down = True
        return StageResult(name="teardown", ok=True, ms=0, detail="clean")


def test_index_exercise_uses_existence_check_not_a_title_relevance_probe():
    """#304: a document source's health 'exercise' probed with a query derived from the store's
    title/description. A SharePoint node's title is just its id ('sharepoint-1') and its
    description is empty, so the probe matched NO document text and — through the #53 relevance
    floor — retrieved 0, falsely reporting 'connected, but no content was retrieved (not indexed
    yet)' for a fully-indexed, authorized source (the same docs answered a real query with
    citations). When the store offers an existence check, exercise must use it: does the caller
    have VISIBLE content, independent of a relevance query."""
    canary = default_strategies()["index"]
    prof = StoreProfile(store_id="sharepoint-1", title="sharepoint-1", description="", kind=INDEXED)
    acc = AccessContext(user_oid="alice", principals=["alice"])

    class _EmptyRetrieveButHasContent:
        def __init__(self, has): self._has = has
        def has_content(self, access): return self._has            # content EXISTS…
        def retrieve(self, access, question, top_k=5): return []    # …but a title probe finds nothing

    ok = canary.exercise(_EmptyRetrieveButHasContent(True), prof, acc)
    assert ok.ok, f"a source with visible content must NOT report 'not indexed': {ok.detail}"
    bad = canary.exercise(_EmptyRetrieveButHasContent(False), prof, acc)
    assert not bad.ok and "not indexed" in bad.detail, bad


def test_index_exercise_falls_back_to_retrieve_without_an_existence_check():
    """Backward-compat: a store that does NOT offer has_content still exercises via the relevance
    retrieve (unchanged behaviour for stores whose title/description is a real content signal)."""
    canary = default_strategies()["index"]
    prof = StoreProfile(store_id="s", title="s", description="stuff", kind=INDEXED)
    acc = AccessContext(user_oid="alice", principals=["alice"])

    class _RetrieveOnly:
        def retrieve(self, access, question, top_k=5):
            return [Evidence(store_id="s", business_unit="", kind=CHUNK, content="hit")]

    r = canary.exercise(_RetrieveOnly(), prof, acc)
    assert r.ok and "retrieved" in r.detail, r


def test_healthy_roundtrip():
    ct = ConnectionTest(_registry(_FakeProvider()), {"index": _HealthyStrategy()})
    v = ct.run(ENTRY, user_oid="alice")
    assert isinstance(v, HealthVerdict) and v.status == "healthy", v
    assert [s.name for s in v.stages] == ["probe", "exercise", "teardown"], v.stages
    assert all(s.ok for s in v.stages), v.stages


def test_reachable_but_blocked_is_degraded():
    ct = ConnectionTest(_registry(_FakeProvider()), {"index": _RoundTripBlockedStrategy()})
    v = ct.run(ENTRY, user_oid="alice")
    assert v.status == "degraded", v
    assert v.remediation, "degraded verdict must carry a remediation hint"


def test_unreachable_is_failed_not_exception():
    ct = ConnectionTest(_registry(_UnreachableProvider()), {"index": _HealthyStrategy()})
    v = ct.run(ENTRY, user_oid="alice")
    assert v.status == "failed", v
    assert v.stages and v.stages[0].name == "probe" and not v.stages[0].ok, v.stages


def test_teardown_runs_even_when_exercise_throws():
    strat = _ThrowingStrategy()
    ct = ConnectionTest(_registry(_FakeProvider()), {"index": strat})
    v = ct.run(ENTRY, user_oid="alice")
    assert strat.torn_down, "teardown MUST run in finally even when exercise raises"
    assert v.status == "degraded", v  # reachable (probe+build ok), round-trip failed


# --- slice 2: the real read-only retrieve canary strategies ---

def _sql_store(rows):
    eng = SqliteEngine.from_tables(
        {"orders": {"columns": ["id", "region", "amount"], "rows": rows}})
    return FederatedSqlStore("orders", "sales", "Orders", "q3 orders", eng)


def test_pushdown_canary_healthy_on_rows():
    store = _sql_store([[1, "EMEA", 100]])
    strat = default_strategies()["pushdown"]
    r = strat.exercise(store, store.profile(), store.authorize("alice"))
    assert r.name == "exercise" and r.ok, r
    assert strat.teardown(store) is None, "read-only canary tears down nothing"


def test_pushdown_canary_degraded_on_empty_table():
    store = _sql_store([])
    r = default_strategies()["pushdown"].exercise(
        store, store.profile(), store.authorize("alice"))
    assert not r.ok, r  # reachable, but the table is empty (count=0)


def test_pushdown_canary_is_dialect_universal_count():
    """Bug (real Azure SQL): 'SELECT * ... LIMIT k' is invalid T-SQL. The canary must probe
    with COUNT(*), which every SQL dialect supports."""
    store = _sql_store([[1, "EMEA", 100]])
    default_strategies()["pushdown"].exercise(
        store, store.profile(), store.authorize("alice"))
    sql = store.audit_trail[-1]["sql"].upper()
    assert "COUNT(" in sql, sql
    assert "LIMIT" not in sql, f"LIMIT is invalid on SQL Server: {sql}"


def test_pushdown_canary_never_writes():
    """LAW: the read-only canary must issue only a SELECT (§12 read-only)."""
    store = _sql_store([[1, "EMEA", 100]])
    default_strategies()["pushdown"].exercise(
        store, store.profile(), store.authorize("alice"))
    assert store.audit_trail, "the SQL must be audited (§8)"
    for rec in store.audit_trail:
        assert rec["sql"].lstrip().upper().startswith("SELECT"), rec["sql"]


def _index_store(seed):
    cfg = {"id": "hr", "business_unit": "hr", "title": "HR",
           "description": "parental leave policy", "seed": seed,
           "user_groups": {"alice": ["hr-staff"]}}
    return LocalIndexProvider().build(cfg)


def test_index_canary_healthy_on_matching_content():
    store = _index_store([{"external_id": "hb", "title": "Handbook", "uri": "u",
                           "acl": ["hr-staff"], "text": "parental leave policy holidays"}])
    r = default_strategies()["index"].exercise(
        store, store.profile(), store.authorize("alice"))
    assert r.ok, r


def test_index_canary_degraded_on_empty_source():
    store = _index_store([])
    r = default_strategies()["index"].exercise(
        store, store.profile(), store.authorize("alice"))
    assert not r.ok, r  # connected, but nothing indexed to retrieve


def main():
    print("Phase G ConnectionTest self-test:")
    test_healthy_roundtrip()
    test_reachable_but_blocked_is_degraded()
    test_unreachable_is_failed_not_exception()
    test_teardown_runs_even_when_exercise_throws()
    print("  PASS  orchestrator: healthy / degraded / failed / teardown-in-finally")
    test_pushdown_canary_healthy_on_rows()
    test_pushdown_canary_degraded_on_empty_table()
    test_pushdown_canary_is_dialect_universal_count()
    test_pushdown_canary_never_writes()
    test_index_canary_healthy_on_matching_content()
    test_index_canary_degraded_on_empty_source()
    test_index_exercise_uses_existence_check_not_a_title_relevance_probe()
    test_index_exercise_falls_back_to_retrieve_without_an_existence_check()
    print("  PASS  canary: pushdown healthy/empty/read-only + index healthy/empty")
    print("  PASS  #304  a document source's exercise uses an existence check (not a title-relevance "
          "probe), so an indexed, authorized source is never falsely 'not indexed yet'")
    print("\nPHASE G CONNECTIONTEST SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
