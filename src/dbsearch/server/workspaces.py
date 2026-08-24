"""#368: WorkspacePool - one composed-catalog `_State` per workspace key.

The key is decided by the caller (router_api's `_workspace_key`): the signed-in user's
oid on real-login deployments, SHARED_KEY on dev-header rigs, so the alice/bob demo and
e2edbs keep today's compose-as-one-query-as-another semantics (spec revision 2026-07-28).

Eviction needs no coordination with in-flight requests: a request holds a reference to
its state/service, so popping the dict entry cannot pull the catalog out from under a
running query - refcounting keeps it alive until the request completes. It IS conditional
on a manifest store being configured, though: eviction is a cache decision, and without a
store to rebuild from it would be a deletion. See `_enforce_cap_locked`.

Two entry points, deliberately: `get` for every READER (lazy create, rebuild from the stored
manifest on a cold key), and `get_for_replace` for the ONE writer that is about to replace the
catalog wholesale (compose), which must not pay for a rebuild it is about to discard.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from dbsearch.server.manifest_store import ManifestStoreUnavailable  # noqa: F401 (re-export)

SHARED_KEY = "__shared__"


def _set_workspace_key(state, key: str) -> None:
    """Tell a freshly made workspace which key it is (#565).

    The pool is the only thing that knows, and the state needs it because a DURABLE ingest
    job store is one table shared by every workspace: `resumable(partition, source_id)` would
    otherwise hand one user's crawl to another user who happened to name a store the same
    thing, and a resume skips the documents that job recorded. Optional-capability rather
    than a factory argument, matching `set_doc_tenant` - the factory is supplied by callers
    and test rigs that have no reason to know about job partitions."""
    setter = getattr(state, "set_workspace_key", None)
    if setter is not None:
        setter(key)


class WorkspacePool:
    def __init__(self, make_state, manifest_store=None, rebuild=None, cap: int = 64) -> None:
        self._make_state = make_state
        self._store = manifest_store
        self._rebuild = rebuild
        self._cap = cap
        self._lock = threading.Lock()
        self._states: "OrderedDict[str, object]" = OrderedDict()

    def get_if_warm(self, key: str):
        """The workspace for `key` ONLY if it is already live - None on a miss, and never
        a rebuild. #731's delete endpoint edits a warm workspace's catalog in place, but a
        COLD key converges from the stored row on its next `get()` anyway, and rebuilding
        one here would re-fire every connector crawl to serve an operation whose whole
        contract is being cheap."""
        with self._lock:
            state = self._states.get(key)
            if state is not None:
                self._states.move_to_end(key)
            return state

    def warm_keys(self) -> list:
        """The keys currently live in the pool - introspection for tests (#731 pins that a
        delete on a cold key materializes nothing)."""
        with self._lock:
            return list(self._states.keys())

    def get(self, key: str):
        """The workspace for `key`, creating (and rebuilding from the manifest store) on
        first touch. Raises ManifestStoreUnavailable when the store is configured but
        broken - the router maps that to a 503, because an empty workspace served in
        place of a failing lookup reads as "your stores are gone" (#200)."""
        with self._lock:
            if key in self._states:
                self._states.move_to_end(key)
                return self._states[key]

        # Store lookup happens OUTSIDE the lock (it is a network call); losing a race
        # just means two threads each rebuild independently, and whichever registers
        # first under the lock below wins - the loser's freshly-built state is simply
        # discarded. Both are valid rebuilds of the same manifest, so this is harmless
        # and idempotent, just not "last write wins".
        manifest = self._store.get(key) if self._store is not None else None
        state = self._make_state()
        _set_workspace_key(state, key)
        if manifest is not None and self._rebuild is not None:
            try:
                self._rebuild(state, manifest, key)
            except ManifestStoreUnavailable:
                # A store outage during rebuild must propagate exactly like a store
                # outage during the lookup above - never swallowed into an empty
                # workspace, which would be indistinguishable from "your stores are
                # gone" (#200). Only the broader except below is allowed to degrade.
                raise
            except Exception:
                # Per-store failures are already non-fatal inside compose (skipped list);
                # this catches manifest-level rebuild errors. The workspace stays usable
                # (empty) and the next successful compose overwrites the stored manifest.
                logging.getLogger("dbsearch").warning(
                    "workspace %s: stored manifest failed to rebuild", key, exc_info=True)
        with self._lock:
            if key not in self._states:            # racing creator may have won
                self._states[key] = state
            self._states.move_to_end(key)
            self._enforce_cap_locked()
            # Deliberately re-read self._states[key] rather than `return state`: this is
            # what makes every racing caller converge on the SAME object. When this
            # thread loses the registration race above, `state` is its own locally-built
            # (and discarded) copy - returning it would hand two different callers two
            # different states for the same key, i.e. split-brain per user. This is the
            # single most important invariant in this file; do not "simplify" it to
            # `return state`.
            return self._states[key]

    def get_for_replace(self, key: str):
        """(state, adopt) for the ONE caller that is about to replace a workspace's catalog
        outright: compose. No manifest-store read, no rebuild.

        `get` on a cold key rebuilds the whole catalog from the stored row, and compose then
        immediately threw that away and composed again - two `_compose_manifest` calls per
        cold compose, which on the real box means the first canvas load after a restart
        connects and probes every one of the owner's cloud databases twice and re-runs a
        connector-rail store's initial full crawl twice (double-firing `sources_synced`), with
        a real chance of simply timing out at the proxy.

        When the key is already warm, `state` IS the live workspace and `adopt` is a no-op:
        compose mutates it in place, exactly as before. When it is COLD, `state` is a fresh
        DETACHED workspace and nothing is registered until the caller calls `adopt()` - so a
        compose that fails (a manifest error, a foreign secret handle, an unpersistable write)
        leaves the pool untouched and the owner's stored stores still rebuild on their next
        read, instead of being replaced by the empty workspace a failed compose just made.

        `adopt()` OVERWRITES rather than yielding to a racing creator (the opposite of `get`):
        a compose is a deliberate write and must win, matching the last-write-wins semantics
        of the manifest-store row it is paired with. A concurrent reader keeps a reference to
        the state it already has, so nothing is pulled out from under an in-flight query.
        """
        with self._lock:
            if key in self._states:
                self._states.move_to_end(key)
                return self._states[key], (lambda: None)
        state = self._make_state()
        _set_workspace_key(state, key)
        return state, (lambda: self._adopt_replacement(key, state))

    def _adopt_replacement(self, key: str, state) -> None:
        with self._lock:
            self._states[key] = state
            self._states.move_to_end(key)
            self._enforce_cap_locked()

    def _enforce_cap_locked(self) -> None:
        """The cap, applied after any registration. Caller MUST already hold self._lock.

        Eviction is only safe where a REBUILD is available. With no manifest store an
        evicted workspace can never come back: its owner's very next request answers
        "no catalog composed yet - POST /router/compose first", which is precisely the
        "your stores are gone" shape this card exists to remove (#200). The memory-only
        lifecycle being replaced here held its one state forever, so growing past the
        cap is strictly closer to that than silently dropping a composed catalog. With a
        store configured, eviction stays exactly as before - the next get() rebuilds.
        """
        while self._store is not None and len(self._states) > self._cap:
            evicted, _ = self._states.popitem(last=False)
            logging.getLogger("dbsearch").info("workspace %s evicted (LRU)", evicted)
        if self._store is None and len(self._states) == self._cap + 1:
            # Fires exactly once, on the crossing: without eviction the dict only grows.
            logging.getLogger("dbsearch").warning(
                "workspace pool grew past its cap of %d and is NOT evicting: no manifest "
                "store is configured, so an evicted workspace could never be rebuilt. "
                "Configure one to bound memory.", self._cap)
