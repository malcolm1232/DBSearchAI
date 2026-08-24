"""#368: one workspace per owner - lazy, LRU-evicted, rebuilt from the manifest store.

    PYTHONPATH=src python3 tests/selftest_workspaces.py
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.server.manifest_store import (  # noqa: E402
    InMemoryManifestStore, ManifestStoreUnavailable,
)
from dbsearch.server.workspaces import WorkspacePool  # noqa: E402


class FakeState:
    def __init__(self):
        self.catalog = None
        self.manifest = None


def test_lazy_create_and_isolation():
    made = []
    pool = WorkspacePool(lambda: (made.append(1), FakeState())[1])
    a, b = pool.get("oid-a"), pool.get("oid-b")
    assert a is not b, "two owners must never share a state"
    a.catalog = "A-CATALOG"
    assert pool.get("oid-b").catalog is None, "#368: A's compose must not touch B"
    assert pool.get("oid-a") is a, "same owner gets the same workspace back"
    assert len(made) == 2
    print("  PASS  lazy create, per-owner isolation, stable identity")


def test_lru_eviction():
    # A manifest store is what makes eviction a CACHE decision rather than a deletion, so
    # the LRU behavior is asserted with one configured (see the pair of tests below).
    pool = WorkspacePool(FakeState, manifest_store=InMemoryManifestStore(), cap=2)
    a = pool.get("a"); pool.get("b")
    pool.get("a")                    # touch a: b is now least-recent
    pool.get("c")                    # evicts b
    assert pool.get("a") is a, "a was touched, must survive"
    b2 = pool.get("b")               # recreated
    assert b2.catalog is None
    print("  PASS  LRU eviction beyond cap, touched entries survive")


def test_no_manifest_store_never_evicts():
    """#368 review finding 1. Eviction without a manifest store is UNRECOVERABLE: nothing
    can rebuild the dropped workspace, so its owner's next request answers "no catalog
    composed yet" - the exact "your stores are gone" shape (#200) this card removes. The
    memory-only lifecycle being replaced held its one state forever, so growing past the
    cap is strictly closer to it than dropping a composed catalog on the floor."""
    pool = WorkspacePool(FakeState, cap=2)          # no manifest store
    a = pool.get("a")
    a.catalog = "A-CATALOG"
    pool.get("b")
    pool.get("c")                                    # would evict `a` under a plain LRU
    pool.get("d")
    assert pool.get("a") is a, (
        "with no manifest store an evicted workspace could never be rebuilt, so the pool "
        "must NOT evict - the owner would silently lose their composed catalog")
    assert pool.get("a").catalog == "A-CATALOG", "the composed catalog must survive intact"
    assert len(pool._states) == 4, f"the pool must grow instead, got {len(pool._states)}"
    print("  PASS  no manifest store -> the pool grows instead of evicting (unrecoverable)")


def test_manifest_store_configured_still_evicts():
    """The other half: with a store, eviction is a cache decision and stays exactly as
    before - the dropped workspace rebuilds from its stored row on the next touch."""
    store = InMemoryManifestStore()
    store.put("a", {"tenant": "acme", "stores": [{"id": "s1"}]})
    pool = WorkspacePool(FakeState, manifest_store=store,
                         rebuild=lambda st, m, o: setattr(st, "catalog", "REBUILT"), cap=2)
    a = pool.get("a")
    assert a.catalog == "REBUILT"
    pool.get("b")
    pool.get("c")                                    # evicts `a` (least-recently used)
    assert len(pool._states) == 2, f"cap must still bind with a store, got {len(pool._states)}"
    a2 = pool.get("a")
    assert a2 is not a, "a really was evicted"
    assert a2.catalog == "REBUILT", "and it came back from the stored manifest"
    print("  PASS  manifest store configured -> LRU eviction still bounds the pool")


def test_rebuild_on_miss_from_stored_manifest():
    store = InMemoryManifestStore()
    store.put("oid-a", {"tenant": "acme", "stores": [{"id": "s1"}]})
    rebuilt = []

    def rebuild(state, manifest, owner):
        rebuilt.append((owner, manifest["stores"][0]["id"]))
        state.catalog = "REBUILT"

    pool = WorkspacePool(FakeState, manifest_store=store, rebuild=rebuild)
    assert pool.get("oid-a").catalog == "REBUILT"
    assert rebuilt == [("oid-a", "s1")]
    pool.get("oid-a")
    assert len(rebuilt) == 1, "rebuild runs once per workspace creation, not per get"
    assert pool.get("oid-b").catalog is None, "no stored manifest -> empty workspace"
    print("  PASS  rebuild-on-miss from stored manifest, once, absent -> empty")


def test_store_unavailable_fails_closed():
    class BrokenStore:
        def get(self, owner):
            raise ManifestStoreUnavailable("pg down")

    pool = WorkspacePool(FakeState, manifest_store=BrokenStore(),
                         rebuild=lambda s, m, o: None)
    try:
        pool.get("oid-a")
        raise AssertionError("a broken store must fail closed, not serve empty")
    except ManifestStoreUnavailable:
        pass
    print("  PASS  broken manifest store raises, never a silently-empty workspace (#200)")


def test_rebuild_failure_still_yields_workspace():
    store = InMemoryManifestStore()
    store.put("oid-a", {"tenant": "acme", "stores": [{"id": "s1"}]})

    def rebuild(state, manifest, owner):
        raise RuntimeError("store engine down")

    pool = WorkspacePool(FakeState, manifest_store=store, rebuild=rebuild)
    ws = pool.get("oid-a")
    assert ws.catalog is None, "failed rebuild -> empty-but-usable workspace"
    print("  PASS  a failing rebuild degrades to an empty workspace, never a crash")


def test_get_for_replace_does_not_read_or_rebuild_on_a_cold_key():
    """#368 final review (IMPORTANT 3a): compose is about to replace the catalog, so it must
    not pay for a rebuild of the one it is discarding."""
    reads, rebuilds = [], []

    class CountingStore(InMemoryManifestStore):
        def get(self, owner):
            reads.append(owner)
            return super().get(owner)

    store = CountingStore()
    store.put("a", {"tenant": "acme", "stores": [{"id": "s1"}]})
    pool = WorkspacePool(FakeState, manifest_store=store,
                         rebuild=lambda st, m, o: rebuilds.append(o))
    state, adopt = pool.get_for_replace("a")
    assert reads == [] and rebuilds == [], (reads, rebuilds)
    assert "a" not in pool._states, "a compose that has not succeeded must not be registered"
    state.catalog = "COMPOSED"
    adopt()
    assert pool.get("a") is state and pool.get("a").catalog == "COMPOSED"
    assert reads == [] and rebuilds == [], "adopt must not trigger a rebuild either"
    print("  PASS  get_for_replace skips the store read and the rebuild on a cold key")


def test_get_for_replace_leaves_the_pool_untouched_when_the_caller_never_adopts():
    """A compose that 400s/403s/503s must not blank a workspace that has a stored manifest."""
    store = InMemoryManifestStore()
    store.put("a", {"tenant": "acme", "stores": [{"id": "s1"}]})
    pool = WorkspacePool(FakeState, manifest_store=store,
                         rebuild=lambda st, m, o: setattr(st, "catalog", "REBUILT"))
    _state, _adopt = pool.get_for_replace("a")       # deliberately never adopted
    assert pool.get("a").catalog == "REBUILT", \
        "the abandoned compose replaced the owner's rebuildable workspace with an empty one"
    print("  PASS  an un-adopted get_for_replace leaves the stored workspace rebuildable")


def test_get_for_replace_on_a_warm_key_is_the_live_state():
    pool = WorkspacePool(FakeState, manifest_store=InMemoryManifestStore())
    live = pool.get("a")
    state, adopt = pool.get_for_replace("a")
    assert state is live, "a warm compose must mutate the live workspace in place, as before"
    adopt()                                   # no-op
    assert pool.get("a") is live
    print("  PASS  get_for_replace on a warm key hands back the live workspace")


def test_adopt_wins_over_a_racing_creator_and_still_respects_the_cap():
    """A compose is a deliberate write, so it must not be discarded in favour of a state some
    reader created while it was composing (the opposite of `get`'s first-wins rule)."""
    store = InMemoryManifestStore()
    pool = WorkspacePool(FakeState, manifest_store=store, cap=2)
    state, adopt = pool.get_for_replace("a")
    state.catalog = "COMPOSED"
    racer = pool.get("a")                     # a reader gets there first
    assert racer is not state
    adopt()
    assert pool.get("a") is state and pool.get("a").catalog == "COMPOSED"
    pool.get("b")
    pool.get("c")                             # cap=2 -> `a` evicted
    assert len(pool._states) == 2, f"the cap must bind after an adopt too: {len(pool._states)}"
    print("  PASS  adopt overwrites a racing creator and the cap still binds")


# The two tests below assert real invariants of WorkspacePool under real threads (one
# object per key; cap never exceeded), but they do NOT prove thread-safety and should
# not be read as such. CPython's GIL makes the tail commit section of get() (the dict
# read/write/popitem sequence under the lock) effectively atomic at default scheduling,
# so both tests were verified to still pass with self._lock monkeypatched to a no-op -
# only a pathological sys.setswitchinterval() (far tighter than any CI would run)
# reproduces a failure, and at that point the unlocked version also throws bare
# KeyError crashes from OrderedDict mutation racing itself, which is a stronger signal
# than these tests would catch anyway. What they DO catch: an implementation that
# returns the locally-built `state` instead of re-reading `self._states[key]` (which
# would let a racing loser hand back a different object than the winner), or one that
# forgets to enforce `cap` under concurrent creates. What they do NOT catch: the actual
# presence or absence of the lock. The lock stays in the implementation on the strength
# of that KeyError evidence, not because these two tests would fail without it.
def test_concurrent_get_same_key_returns_one_object():
    def slow_make_state():
        time.sleep(0.05)          # widen the window so a real race is likely
        return FakeState()

    pool = WorkspacePool(slow_make_state)
    with ThreadPoolExecutor(max_workers=20) as pool_exec:
        results = list(pool_exec.map(lambda _: pool.get("same-key"), range(20)))

    assert len({id(o) for o in results}) == 1, (
        "20 concurrent get() calls on the same cold key must all return the same object")
    print("  PASS  concurrent get() on the same cold key: exactly one object created")


def test_concurrent_creates_never_exceed_cap():
    # With a manifest store configured, so the cap is actually enforced (see
    # test_no_manifest_store_never_evicts for why it deliberately is not without one).
    pool = WorkspacePool(FakeState, manifest_store=InMemoryManifestStore(), cap=10)
    keys = [f"key-{i}" for i in range(50)]

    with ThreadPoolExecutor(max_workers=25) as pool_exec:
        list(pool_exec.map(pool.get, keys))

    assert len(pool._states) == 10, (
        f"cap must never be exceeded under concurrent creates, got {len(pool._states)}")
    print("  PASS  concurrent creates across many keys never exceed the cap")


if __name__ == "__main__":
    test_lazy_create_and_isolation()
    test_lru_eviction()
    test_no_manifest_store_never_evicts()
    test_manifest_store_configured_still_evicts()
    test_rebuild_on_miss_from_stored_manifest()
    test_store_unavailable_fails_closed()
    test_rebuild_failure_still_yields_workspace()
    test_get_for_replace_does_not_read_or_rebuild_on_a_cold_key()
    test_get_for_replace_leaves_the_pool_untouched_when_the_caller_never_adopts()
    test_get_for_replace_on_a_warm_key_is_the_live_state()
    test_adopt_wins_over_a_racing_creator_and_still_respects_the_cap()
    test_concurrent_get_same_key_returns_one_object()
    test_concurrent_creates_never_exceed_cap()
    print("OK selftest_workspaces")
