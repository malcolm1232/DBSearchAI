"""Phase E E3 — fan-out executor self-test.
Run: python3 tests/selftest_router_executor.py
"""
import os
import sys
import time
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.catalog import STORE, StoreCatalog, CatalogNode  # noqa: E402
from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402
from dbsearch.router.evidence import CHUNK, Evidence  # noqa: E402
from dbsearch.router.executor import (  # noqa: E402
    EMPTY, ERROR, OK, TIMEOUT, DispatchReport, StoreOutcome, execute,
)
from dbsearch.router.store import (  # noqa: E402
    AccessContext, INDEXED, SEMANTIC, StorePort, StoreProfile,
)


class FakeStore(StorePort):
    """Configurable StorePort: returns canned evidence, or raises, or sleeps."""

    def __init__(self, store_id, bu, evidence=None, raise_exc=None, delay=0.0):
        self._id, self._bu = store_id, bu
        self._evidence = evidence or []
        self._raise = raise_exc
        self._delay = delay
        self.authorized_as = None            # spy: proves authorize() ran per store

    def profile(self):
        return StoreProfile(store_id=self._id, title=self._id, description="",
                            kind=INDEXED, capabilities={SEMANTIC}, business_unit=self._bu)

    def authorize(self, user_oid):
        self.authorized_as = user_oid
        return AccessContext(user_oid=user_oid, principals=["p"])

    def retrieve(self, access, question, top_k=5):
        if self._delay:
            time.sleep(self._delay)
        if self._raise:
            raise self._raise
        return self._evidence[:top_k]


def _ev(store_id, bu, text):
    return Evidence(store_id=store_id, business_unit=bu, kind=CHUNK, content=text,
                    provenance={"doc": text, "title": text, "uri": "u", "locator": {}},
                    score=0.9)


def _catalog(stores):
    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind="tenant", parent_id=None, acl=["p"]))
    for s in stores:
        cat.register(CatalogNode(id=s._id, kind=STORE, parent_id="t", acl=["p"],
                                 profile=s.profile(), store=s))
    return cat


def _decision(*stores):
    routed = [RoutedStore(store_id=s._id, business_unit=s._bu, score=1.0) for s in stores]
    return RoutingDecision(query_type="semantic", stores=routed, candidates=routed)


def test_dispatch_two_stores_collects_both():
    a = FakeStore("hr", "hr", evidence=[_ev("hr", "hr", "leave policy")])
    b = FakeStore("fin", "finance", evidence=[_ev("fin", "finance", "q3 revenue")])
    rep = execute(_catalog([a, b]), _decision(a, b), "carol", "question")
    assert isinstance(rep, DispatchReport), rep
    assert set(rep.evidence_by_store) == {"hr", "fin"}, rep.evidence_by_store
    assert [o.status for o in rep.outcomes] == [OK, OK], rep.outcomes
    assert a.authorized_as == "carol" and b.authorized_as == "carol", "authorize per store"


def test_error_store_dropped_not_fatal():
    good = FakeStore("hr", "hr", evidence=[_ev("hr", "hr", "leave policy")])
    bad = FakeStore("fin", "finance", raise_exc=RuntimeError("connection refused"))
    rep = execute(_catalog([good, bad]), _decision(good, bad), "carol", "q")
    assert rep.evidence_by_store.get("hr"), rep.evidence_by_store
    assert "fin" not in rep.evidence_by_store, rep.evidence_by_store
    by_id = {o.store_id: o for o in rep.outcomes}
    assert by_id["fin"].status == ERROR and "connection refused" in by_id["fin"].error, by_id
    assert by_id["hr"].status == OK, by_id


def test_empty_store_is_empty_status():
    a = FakeStore("hr", "hr", evidence=[])
    rep = execute(_catalog([a]), _decision(a), "carol", "q")
    assert rep.outcomes[0].status == EMPTY, rep.outcomes[0]
    assert rep.evidence_by_store == {}, rep.evidence_by_store


def test_no_selected_stores_is_noop():
    rep = execute(StoreCatalog(), RoutingDecision(query_type="semantic"), "carol", "q")
    assert rep.evidence_by_store == {} and rep.outcomes == [], rep


def test_top_k_forwarded():
    evs = [_ev("hr", "hr", "doc%d" % i) for i in range(9)]
    a = FakeStore("hr", "hr", evidence=evs)
    rep = execute(_catalog([a]), _decision(a), "carol", "q", top_k=3)
    assert len(rep.evidence_by_store["hr"]) == 3, rep.evidence_by_store


def test_slow_store_times_out_and_is_dropped():
    fast = FakeStore("hr", "hr", evidence=[_ev("hr", "hr", "leave policy")])
    slow = FakeStore("fin", "finance", evidence=[_ev("fin", "finance", "x")], delay=0.5)
    t0 = time.monotonic()
    rep = execute(_catalog([fast, slow]), _decision(fast, slow), "carol", "q", timeout_s=0.05)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.4, "executor must not wait for the slow store (%.2fs)" % elapsed
    by_id = {o.store_id: o for o in rep.outcomes}
    assert by_id["fin"].status == TIMEOUT, by_id
    assert by_id["hr"].status == OK, by_id
    assert "fin" not in rep.evidence_by_store, rep.evidence_by_store


def main():
    print("Phase E E3 executor self-test:")
    test_dispatch_two_stores_collects_both()
    test_error_store_dropped_not_fatal()
    test_empty_store_is_empty_status()
    test_no_selected_stores_is_noop()
    test_top_k_forwarded()
    test_slow_store_times_out_and_is_dropped()
    print("  PASS  two-store dispatch / error drop / empty status / noop / top_k / timeout drop")
    print("\nE3 EXECUTOR SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
