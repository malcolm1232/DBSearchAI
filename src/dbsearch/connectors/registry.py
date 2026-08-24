"""SourceRegistry — the data-plane catalogue of connected sources + their sync-state.

Owns connector instances and per-source mutable state (cursor, last_sync_at, doc_count,
status). It deliberately knows NOTHING about the queue/store/embedder — the Edition
orchestrates the crawl and reports the result back here (keeps the registry portable; the
Azure edition backs this with a metadata table behind the same interface). LAW 5: a
registry instance is per-tenant (the Edition owns one).
"""
from __future__ import annotations

from dataclasses import dataclass

from dbsearch.ports.base import ConnectorPort


@dataclass
class SourceSummary:
    """Metadata-only view of a source (LAW 1) — safe to return over /admin/sources."""
    source_id: str
    kind: str
    display_name: str
    last_sync_at: str | None
    doc_count: int
    status: str
    unreadable: int = 0


@dataclass
class SourceDescriptor:
    source_id: str
    kind: str                       # "sharepoint" | "local"
    display_name: str
    connector: ConnectorPort
    cursor: str | None = None       # persisted change token (resumable incremental sync)
    last_sync_at: str | None = None
    doc_count: int = 0
    unreadable: int = 0
    status: str = "idle"            # "idle" | "error" ("syncing" reserved for async slice)
    # #577: the WORKSPACE this source's ingest jobs belong to (#565). It is recorded HERE, at
    # register time, because a resume looks a job up by (job_tenant, source_id) - and a
    # re-crawl that computed a different job_tenant than the first crawl used simply finds
    # nothing resumable and silently re-pays for the whole library. That is exactly what
    # happened when #569 first landed: connect recorded jobs under the request's partition
    # and resync looked under the deployment constant.
    job_tenant: str = ""
    # #883: the connector BUILD recipe (az_tenant_id, drive_id, folder_path, owner_oid) for the
    # sources whose connector is not reconstructible from env alone. It is deliberately NOT in
    # summary(): /admin/sources is a metadata-only view (LAW 1) and this is internal plumbing.
    # Never a credential - see source_sync_store's module docstring.
    config: "dict | None" = None

    def summary(self) -> SourceSummary:
        return SourceSummary(
            source_id=self.source_id, kind=self.kind, display_name=self.display_name,
            last_sync_at=self.last_sync_at, doc_count=self.doc_count, status=self.status,
            unreadable=self.unreadable,
        )


class SourceRegistry:
    """#883: optionally backed by a durable store, so sync-state survives a restart.

    `store` is duck-typed (get/put/list/delete keyed by scope + source_id) on purpose: this
    module knows nothing about the server package, and the docstring above has always said the
    Azure edition backs it "with a metadata table behind the same interface". `store=None` is
    the pre-#883 behaviour exactly, which is what every rig and the whole router rail still use
    - the router rebuilds its registries from durable manifests and re-crawls, so rehydrating
    counts there would report a full index while the rebuilt one is empty.
    """

    def __init__(self, store=None, scope: str = "") -> None:
        self._sources: dict[str, SourceDescriptor] = {}
        self._store = store
        self._scope = scope

    def register(self, desc: SourceDescriptor) -> None:
        if self._store is not None:
            self._merge_persisted(desc)
            self._persist(desc)
        self._sources[desc.source_id] = desc

    def _row_of(self, d: SourceDescriptor) -> dict:
        return {"kind": d.kind, "display_name": d.display_name, "cursor": d.cursor,
                "last_sync_at": d.last_sync_at, "doc_count": d.doc_count,
                "unreadable": d.unreadable, "status": d.status,
                "job_tenant": d.job_tenant, "config": d.config}

    def _persist(self, d: SourceDescriptor) -> None:
        self._store.put(self._scope, d.source_id, self._row_of(d))

    def _merge_persisted(self, desc: SourceDescriptor) -> None:
        """Overlay what the last process learned onto a freshly-built descriptor.

        Register is a MERGE rather than the blind overwrite it used to be, and that is the
        whole fix: build_edition re-registers the seeded "sharepoint" source on every single
        boot with a virgin descriptor, so an overwrite would wipe the durable row a moment
        after reading it and the table would faithfully persist a zero forever.
        """
        row = self._store.get(self._scope, desc.source_id)
        if not row:
            return
        # What the last COMPLETED crawl found. Always restored: this is the count the node
        # renders, and it is true about the corpus regardless of who is registering now.
        desc.doc_count = row.get("doc_count") or 0
        desc.unreadable = row.get("unreadable") or 0
        desc.last_sync_at = row.get("last_sync_at")

        # "syncing" cannot survive the process that was doing the syncing. Rehydrating it would
        # leave a node spinning forever on a crawl with no thread behind it; the interrupted
        # work is still resumable through /admin/resync, which is where that story belongs.
        status = row.get("status") or "idle"
        desc.status = "idle" if status == "syncing" else status

        # The cursor is a delta token that belongs to ONE drive. A re-connect pointed at a
        # different library must not inherit the old one: the crawl would resume from a
        # position in somebody else's change feed and skip everything before it. Restore it
        # only when this descriptor describes the same source the token was earned against.
        if desc.config is None or desc.config == row.get("config"):
            desc.cursor = row.get("cursor")

        # #577: connect_sharepoint pins the job workspace explicitly and must win - a resume
        # looks a job up by (job_tenant, source_id). The persisted value is only a fallback for
        # a descriptor that did not state one (the boot seeds).
        if not desc.job_tenant:
            desc.job_tenant = row.get("job_tenant") or ""

    def get(self, source_id: str) -> SourceDescriptor:
        return self._sources[source_id]          # KeyError -> 404 at the route

    def remove(self, source_id: str) -> bool:
        """#947: forget a source entirely - descriptor, status and delta cursor.

        Idempotent: removing an absent id is False, not an error. Used by a destructive
        connector delete so a re-add is a FRESH crawl (a new descriptor, no dead cursor to
        delta off), never a resume of a store whose content was just purged."""
        return self._sources.pop(source_id, None) is not None

    def list_sources(self) -> list[SourceSummary]:
        return [d.summary() for d in self._sources.values()]

    def source_ids(self) -> set[str]:
        """Which sources are registered in THIS process (#883: what rehydration must skip)."""
        return set(self._sources)

    def record_sync(self, source_id: str, *, cursor: str | None, doc_count: int,
                    at: str, unreadable: int = 0, full_crawl: bool = True,
                    created: int = 0, deleted: int = 0) -> SourceSummary:
        """#910: `SourceDescriptor.doc_count` means CORPUS SIZE - it is what the canvas node
        renders as "N docs" - and this is the ONE place that rule lives (both rails call
        here; two copies of the arithmetic is how a rule rots).

        A FULL crawl listed everything, so its per-crawl `doc_count` IS the corpus and is
        written absolutely - which is also what keeps a genuinely emptied folder honest at
        zero. An INCREMENTAL crawl only saw what changed: its per-crawl count (0 for a
        quiet resync) says nothing about corpus size, so the previous count is carried
        forward and adjusted by the crawl's net change. Before this rule, one quiet
        `/admin/resync` durably wrote 0 over a real 5 - and #883 had just made that wrong
        zero survive restarts."""
        d = self._sources[source_id]
        d.cursor = cursor
        d.doc_count = doc_count if full_crawl else max(0, d.doc_count + created - deleted)
        d.unreadable = unreadable
        d.last_sync_at = at
        d.status = "idle"
        # #883 / ADR 0016: this is the ONLY place a cursor becomes durable, and it is reached
        # from the ingest job's _commit - after the batch is written, before the job publishes
        # succeeded. Persisting next_cursor any earlier is the "make it resumable" fix that
        # silently skips every unprocessed item in the batch.
        if self._store is not None:
            self._persist(d)
        return d.summary()

    def mark_syncing(self, source_id: str) -> None:
        """A crawl has been SUBMITTED and is not finished (#454). This is the value the
        descriptor's own comment reserved ("syncing" reserved for async slice) — the state
        machine was anticipated when the field was written and never built, because until now
        a sync could only be in progress inside the request that asked for it.

        Deliberately does NOT touch cursor/doc_count/last_sync_at: those describe the last
        COMPLETED crawl and must keep reading true while the next one runs. A store that
        blanked its freshness the moment a re-sync started would look empty to routing for
        the whole crawl, which is the #391 lesson in a different costume.

        #883: and deliberately does NOT write through either. This runs on the submit path,
        which should not wait on a database to start a crawl, and the value it would persist
        is one no restart can leave true - _merge_persisted coerces a stored "syncing" back to
        "idle" for exactly that reason. Writing it would only create the lie it then repairs."""
        self._sources[source_id].status = "syncing"

    def record_error(self, source_id: str) -> None:
        d = self._sources[source_id]
        d.status = "error"
        # A failed crawl IS durable state: the node must keep saying "sync-failed" after a
        # restart rather than reverting to a hopeful "idle" that nobody asked for.
        if self._store is not None:
            self._persist(d)
