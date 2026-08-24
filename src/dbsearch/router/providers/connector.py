"""ConnectorStoreProvider — the document-connector rail plugged into the compose layer (#111).

Before this, the repo had TWO onboarding rails: ConnectorPort (document ingestion — SourceRegistry,
delta cursors, run_ingestion) and StoreProviderPort (Phase E compose). The user's mental model is
ONE pool — ".doc, .ppt, .csv, sharepoints, BigQuery" all plug into the same Lego board. This
provider is the seam that makes that honest for `mode: index` (ADR 0008): a stores.yml entry like

    - id: legal-archive
      kind: folder            # or sharepoint
      mode: index             # no queryable surface -> derived in-tenant index
      config: { path: ${LEGAL_SHARE} }

DRIVES the connector rail: build() creates the ConnectorPort, registers a SourceDescriptor in a
real SourceRegistry (same class the admin rail uses), runs the initial crawl through the queue-
driven run_ingestion pipeline (LAW 4), and returns an IndexedStore over the result — the same
LAW-2 trim byte-for-byte. sync() re-crawls INCREMENTALLY off the persisted delta cursor (LAW 3)
and the store's profile reports honest freshness (`ingested@ts`), which routing (E2) can surface.

Each built store gets its own in-memory index/object-store (per-store isolation, mirroring
LocalIndexProvider; the pgvector edition swaps these seams, not this provider). probe() is
side-effect-free — it verifies the source is reachable and reports current freshness, never
ingests (the canvas Test-connection calls it).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Callable

from dbsearch.adapters.local import (
    ExtractiveLlm, HashingEmbedding, InMemoryIdentity, InMemoryIndex,
    InMemoryObjectStore, InMemoryQueue, LocalRichExtractor,
)
from dbsearch.connectors.folder import FolderConnector
from dbsearch.connectors.registry import SourceDescriptor, SourceRegistry, SourceSummary
from dbsearch.connectors.sharepoint import SharePointConnector
from dbsearch.pipeline.jobs import IngestJob, InMemoryJobStore
from dbsearch.pipeline.runner import run_ingestion
from dbsearch.pipeline.worker import IngestJobRunner
from dbsearch.ports.base import ConnectorPort, IdentityPort
from dbsearch.query import QueryService
from dbsearch.router.indexed_store import IndexedStore
from dbsearch.router.provider import StoreProviderPort
from dbsearch.router.store import INDEXED, SEMANTIC, StoreProfile

# How long a Test-connection will wait for a throwaway ingest before reporting on what it
# has. Bounded on purpose: this runs inside a user's request (see build_isolated).
_HEALTH_INGEST_TIMEOUT = 30.0

ConnectorFactory = Callable[[dict], ConnectorPort]
SyncHook = Callable[[SourceSummary], None]


def _build_connector(factory: ConnectorFactory, config: dict,
                     credential: "str | None" = None) -> ConnectorPort:
    """Call a connector factory, handing it the caller's delegated credential IF it takes one.

    #673. The connector rail has never had a delegated-credential path - `folder` reads a
    server path and `sharepoint` carries its own OAuth - so S3 is the first connector whose
    content belongs to the CALLER's cloud account rather than to the deployment. Rather than
    change every factory's signature, this uses the same optional-capability idiom ADR 0022
    used for `probe_as`: inspect for a `credential` parameter and pass it only where one is
    declared.

    Bound by PARAMETER NAME, deliberately, exactly as identity_broker._for_idp is. An arity
    check would hand a credential to a factory whose second parameter means something else -
    and a credential delivered to the wrong place is a leak, not a failed build.
    """
    if credential is None:
        return factory(config)
    import inspect

    try:
        takes = "credential" in inspect.signature(factory).parameters
    except (TypeError, ValueError):      # builtins / C callables: shape unknowable
        takes = False
    return factory(config, credential=credential) if takes else factory(config)


class IngestFailed(Exception):
    """A blocking `sync()` whose background job ended in `failed`.

    Raised so the blocking form keeps the contract its callers were written against — it used
    to propagate the crawl's own exception. The message carries the job's `error`, which is an
    exception CLASS NAME by construction (jobs.JobCheckpoint.failed), never a driver or
    connector message that could quote document content or a credential (LAW 1)."""


def folder_connector_factory(config: dict) -> FolderConnector:
    """`kind: folder` — on-prem file share / air-gapped directory. config: path, default_acl?

    #551: `default_acl` falls back to the STORE's own acl. The connector reads a document's
    audience from its immediate sub-directory name and skips anything sitting loose in the
    root (default-deny — never index content whose audience is unknown). That is the right
    rule, but it meant pointing the connector at an ordinary flat folder of HR documents
    ingested zero files and still reported a healthy store: a silently empty index, which is
    the worst failure shape there is because nothing anywhere says the word "empty".

    Inheriting the store's acl cannot widen anything — the store's own acl already gates who
    may query it at all, so a document can never become visible to someone the store itself
    would refuse. An explicit `default_acl` still wins, and a folder that DOES use
    sub-directories as groups is unaffected: the fallback only applies to loose files."""
    return FolderConnector(config["id"], config["path"],
                           config.get("default_acl") or config.get("acl"))


def s3_connector_factory(config: dict, credential: "str | None" = None) -> "S3Connector":
    """`kind: s3` - an Amazon S3 bucket (or prefix) as a document source (#673, ADR 0024).

    `credential` is the caller's own STS triple, minted at compose/probe from their vaulted
    AWS keys. Absent, the connector falls back to the box's ambient AWS identity, which is
    right for a self-host operator and absent on a hosted deployment - where the honest
    outcome is a boto3 failure, not a silent read of somebody else's bucket.

    THE AUDIENCE IS TAKEN FROM THE STORE, NOT INFERRED. `owner_principal` is the store's own
    acl (its first entry), so every document lands visible to exactly the identity that
    already gates the store. S3 has no per-object principal list to read - see
    connectors/s3.py - so this connector never claims to know one, and an empty acl is refused
    outright rather than defaulting to something wider.
    """
    from dbsearch.connectors.s3 import S3Connector

    acl = config.get("acl") or []
    if not acl:
        # Default-deny, loudly. A store with no audience would otherwise ingest documents
        # nobody can see (#200's silently-invisible store) or, if this defaulted to something
        # broader, documents the wrong people can see. Neither is acceptable for a bucket.
        raise ValueError(
            "an s3 store needs an audience: no acl is set, so every ingested document would "
            "be visible to nobody. Set 'Who can see this store' before composing")
    return S3Connector(config["id"], config["bucket"], owner_principal=acl[0],
                       prefix=config.get("prefix", ""), region=config.get("region"),
                       credential=credential)


def gdrive_connector_factory(config: dict, credential: "str | None" = None) -> "GDriveConnector":
    """`kind: gdrive` - a PUBLIC Google Drive folder as a document source (#712).

    Slice 1: no `credential` - the deployment's GOOGLE_API_KEY reads "Anyone with the link"
    content. An API key is a quota token, not an identity: it cannot read any private Drive,
    so a missing key or a private folder fails loudly at probe, never silently.

    `credential` (slice 2, ADR-gated - see the spec) is the caller's own Google access
    token; the parameter exists NOW so _build_connector's by-name binding is already wired.

    THE AUDIENCE IS THE STORE'S OWN, refused if empty - #673's rule verbatim: a store with
    no audience would ingest documents nobody can see (#200), and defaulting wider is a
    disclosure. A public folder has no per-user audience to read (see connectors/gdrive.py).
    """
    from dbsearch.connectors.gdrive import GDriveConnector, _folder_id_from_link

    acl = config.get("acl") or []
    if not acl:
        raise ValueError(
            "a gdrive store needs an audience: no acl is set, so every ingested document "
            "would be visible to nobody. Set 'Who can see this store' before composing")
    folder_id = _folder_id_from_link(config.get("link") or config.get("folder_id") or "")
    return GDriveConnector(config["id"], folder_id, acl,
                           api_key=os.environ.get("GOOGLE_API_KEY", ""),
                           credential=credential)


def sharepoint_link_connector_factory(config: dict) -> "SharePointLinkConnector":
    """`kind: sharepoint_link` - a SharePoint / OneDrive FOLDER shared as "Anyone with the
    link" as a document source (#924). No credential of any kind - not the deployment's, not
    the caller's: the link mints its own anonymous badge (see connectors/sharepoint_link.py).

    THE AUDIENCE IS THE STORE'S OWN, refused if empty - #673's rule verbatim, as for gdrive:
    an anyone-with-the-link share has no per-user audience to read, and a store with no
    audience would ingest documents nobody can see (#200).

    No `credential` parameter, deliberately: _build_connector binds one by name only when the
    factory declares it, and this connector has nothing to do with a delegated token."""
    from dbsearch.connectors.sharepoint_link import SharePointLinkConnector

    acl = config.get("acl") or []
    if not acl:
        raise ValueError(
            "a sharepoint_link store needs an audience: no acl is set, so every ingested "
            "document would be visible to nobody. Set 'Who can see this store' before composing")
    return SharePointLinkConnector(config["id"], config.get("link") or "", acl)


def sharepoint_connector_factory(config: dict) -> SharePointConnector:
    """`kind: sharepoint, mode: index` — full-fidelity ingested SharePoint (vs `graph_search`
    = mode:native). Seed-backed until the Graph delta connector lands (Phase 3); config: seed?"""
    return SharePointConnector(tenant_id=config["id"], seed=config.get("seed"))


def _recipe(kind: str, config: dict, credential: "str | None") -> str:
    """#944: a stable fingerprint of everything a built store depends on.

    The WHOLE entry, deliberately - see build_as. `credential` is reduced to whether one was
    present rather than carried: this string is held in memory for the life of the process and
    a delegated token has no business sitting in it (LAW 1), and its presence is the only part
    that changes what gets built anyway."""
    import json
    return json.dumps({"kind": kind, "entry": config, "cred": bool(credential)},
                      sort_keys=True, default=str)


def _crawl_failed(sources, store_id: str) -> bool:
    """Did this store's last crawl end in error? Unknown counts as NOT failed - a source with
    no descriptor has never crawled, and treating that as a failure would rebuild forever."""
    try:
        return getattr(sources.get(store_id), "status", "") == "error"
    except Exception:
        return False


def _freshness(desc: SourceDescriptor | None) -> str:
    """What this store can honestly say about its own content.

    #454 made ingest asynchronous, which created a state that could not exist before: a store
    that has been composed but whose documents are not in yet. Reporting that as
    `never-synced` would be a lie of the #371 kind (the UI contradicting the data), and
    reporting it as `ingested@` would be worse. Worse still is the failed case: before this,
    a bad connector config raised inside `build()` and compose reported it in `skipped`; now
    the crawl fails on a worker, so if freshness did not carry it, a broken source would
    compose cleanly and sit there empty and silent - which is exactly the failure shape #551
    called the worst available. Note the ordering: a RUNNING re-sync of a store that already
    has content still says `syncing`, and its previous `last_sync_at` is untouched underneath
    (registry.mark_syncing), so nothing pretends the old content vanished."""
    if desc is None:
        return "never-synced"
    if desc.status == "syncing":
        return f"syncing@{desc.last_sync_at}" if desc.last_sync_at else "syncing"
    if desc.status == "error":
        return f"sync-failed@{desc.last_sync_at}" if desc.last_sync_at else "sync-failed"
    return f"ingested@{desc.last_sync_at}" if desc.last_sync_at else "never-synced"


class ConnectorBackedStore(IndexedStore):
    """IndexedStore whose profile reports the live sync-state of its backing connector."""

    def __init__(self, *args, descriptor: "SourceDescriptor | None",
                 unavailable: "BaseException | None" = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._descriptor = descriptor
        self._unavailable = unavailable
        self._on_ingest_complete: list = []
        self._ingest_completions = 0          # >0 => a late subscriber has already missed one

    def profile(self) -> StoreProfile:
        p = super().profile()
        p.freshness = _freshness(self._descriptor)
        return p

    @property
    def descriptor(self):
        """#939: the LIVE sync descriptor - doc_count, unreadable, status, last_sync_at.

        Public because the canvas needs the CURRENT state, and every other route to it is a
        snapshot: `provisioning` copies `profile()` into the catalog node at compose time, so a
        crawl that finished afterwards is invisible to anything reading the node. That is the
        prod defect this exposes - catalog `ingested@08:58:31`, badge still `syncing`."""
        return self._descriptor

    def freshness(self) -> str:
        """This store's freshness AS OF NOW, not as of the compose that built it. Same
        `_freshness` mapping the profile uses, so the two can never disagree."""
        return _freshness(self._descriptor)

    def authorize(self, user_oid: str):
        """#676. The connector rail's ONLY refusal point, and the reason it needs one.

        The SQL rails get this for free: their store's `authorize` runs through the
        IdentityBroker, so an unlinked caller is refused again at query time and the executor
        turns that into "connect Amazon to query this source". This rail never reaches the
        broker - its credential is spent at CRAWL time, not query time - so without this an
        unlinked caller's store answers "matched nothing", which is a sentence about their
        question when the truth is a sentence about their account.

        Only ever set by `build_unavailable`, i.e. the store declared a delegation and the
        caller could not satisfy it. A store built normally carries None here and this is a
        straight pass-through, so the document ACL trim below is untouched."""
        if self._unavailable is not None:
            raise self._unavailable
        return super().authorize(user_oid)

    def on_ingest_complete(self, callback) -> None:
        """Subscribe to "this store's content changed".

        #454 made the initial crawl asynchronous, and that quietly broke something the
        compose layer depends on: `provisioning` snapshots `store.profile()` into the catalog
        node at compose time, and the ROUTER ranks on that snapshot (`profiles.py` even
        memoizes its embedding). Taken while the crawl is still queued, the snapshot describes
        an EMPTY index — so a freshly connected document store would be invisible to routing
        until someone recomposed, which is #306/#453's failure ("an undescribed doc store fell
        below unrelated stores") reintroduced by the back door. The catalog node subscribes
        here and re-derives its profile when the content actually lands.

        The event is STICKY. `build()` submits the crawl and the caller subscribes after it
        returns, so a small source can finish before anyone is listening — subscribe-after-fire,
        and the profile would then never be refreshed at all. It reproduced as a ~1-in-3 flake
        in e2e_router_api the moment the folder had a single file, which is also the shape of
        the first thing a real user connects. A late subscriber is therefore called at once;
        the callback re-derives a profile and is idempotent by construction."""
        self._on_ingest_complete.append(callback)
        if self._ingest_completions:
            self._fire(callback)

    def ingest_completed(self) -> None:
        self._ingest_completions += 1
        for cb in list(self._on_ingest_complete):
            self._fire(cb)

    def _fire(self, cb) -> None:
        try:
            cb()
        except Exception:                     # noqa: BLE001
            # A subscriber that throws must not fail the crawl that just succeeded: the
            # documents ARE indexed, and reporting the job as failed would send a resume
            # after work that is genuinely done.
            logging.getLogger("dbsearch").warning(
                "ingest-complete subscriber failed for %s", self._store_id, exc_info=True)


class ConnectorStoreProvider(StoreProviderPort):
    modes = ("index",)          # ADR 0008: derived in-tenant index over connector content

    def __init__(self, kind: str, connector_factory: ConnectorFactory, *,
                 identity: IdentityPort | None = None,
                 now: Callable[[], str] | None = None,
                 on_sync: SyncHook | None = None,
                 shared_query_service=None,
                 doc_tenant: "str | None" = None,
                 job_store=None,
                 job_partition: str = "",
                 runner: "IngestJobRunner | None" = None) -> None:
        self.kind = kind
        self._factory = connector_factory
        self._identity = identity
        self._now = now or (lambda: datetime.now(timezone.utc).isoformat())
        self._on_sync = on_sync
        self.sources = SourceRegistry()                       # the connector rail, for real
        self._pipes: dict[str, tuple] = {}                    # store_id -> (obj, embedder, index)
        # #944: store_id -> the recipe the CURRENT build was made from. Parallel to _pipes on
        # purpose: a pipe without a recipe (or a recipe whose entry changed) must rebuild.
        self._recipes: dict[str, str] = {}
        self._stores: dict[str, ConnectorBackedStore] = {}    # store_id -> the built store
        self._jobs: dict[str, str] = {}                       # store_id -> latest job id
        # Bumped every time a store id is rebuilt. A crawl commits only if its generation is
        # still current — see _crawl. Without it, a crawl launched by an earlier build can
        # write its cursor and doc_count onto the descriptor of a store whose index it never
        # filled, which reads as "synced, 12 documents" over an empty index.
        self._generation: dict[str, int] = {}
        # #454: crawls run HERE, off the request thread. The job store is injectable so a
        # deployment with Postgres gets restart-resume (#327) and a rig gets the same
        # behaviour minus durability — one code path, two levels of persistence, rather than
        # a synchronous mode that tests would exercise and production would not.
        self.jobs = job_store if job_store is not None else InMemoryJobStore()
        # #565: which partition this provider's job records belong to. It is the WORKSPACE,
        # not the Entra tid: a durable job store is one table shared by every workspace, and
        # `resumable(tenant, source_id)` looks a job up by that pair. Two users in the same
        # tenant who both connect a store called "hr-docs" would then be handed each other's
        # job - and a resume skips the documents that job recorded, into an index that never
        # saw them. Silent loss, in the one mechanism built to prevent it.
        self.job_partition = job_partition
        self._runner = runner if runner is not None else IngestJobRunner(self.jobs)
        # #304/#306: when set, build() wraps the EDITION's already-ingested index instead of
        # crawling a throwaway seed pipe — so the router's document store sees the REAL docs that
        # edition.connect_sharepoint ingested (health has_content + routing content_titles were
        # otherwise computed over an empty seed index, disconnected from where the docs live).
        self._shared_qs = shared_query_service
        # #439: the ADR 0012 partition a SHARED-index store must read. The shared QueryService
        # belongs to the edition and carries the deployment constant, so a foreign owner's
        # store would query the home partition and find nothing - #389 lifted the ingest gate
        # but retrieval was still pointed at the wrong tenant. Set per workspace (one owner,
        # one partition) via `set_doc_tenant`, from the caller's SERVER-VERIFIED tid; never
        # from anything the client sends.
        self.doc_tenant = doc_tenant

    def set_doc_tenant(self, tenant_id: "str | None") -> None:
        self.doc_tenant = tenant_id or None

    # ------------------------------------------------------------- provider port

    def probe(self, config: dict) -> StoreProfile:
        return self.probe_as(config)

    def probe_as(self, config: dict, credential: "str | None" = None) -> StoreProfile:
        """#673: reachability AS THE CALLER when the store declares a delegation (ADR 0022's
        rule, now on the connector rail). Test-connection on an S3 store must list the
        caller's own bucket - probing with the box's ambient identity would either fail on a
        hosted deployment or, worse, succeed against a bucket that is not theirs."""
        connector = _build_connector(self._factory, config, credential)
        connector.authenticate(config)
        connector.list_changes(None)      # reachability: listing only, nothing ingested
        try:
            desc = self.sources.get(config["id"])
        except KeyError:
            desc = None
        return StoreProfile(
            store_id=config["id"],
            title=config.get("title", config["id"]),
            description=config.get("description", ""),
            kind=INDEXED,
            capabilities={SEMANTIC},
            business_unit=config.get("business_unit", ""),
            freshness=_freshness(desc),
        )

    def build(self, config: dict) -> ConnectorBackedStore:
        return self.build_as(config)

    def build_unavailable(self, config: dict,
                          reason: BaseException) -> ConnectorBackedStore:
        """#676: the store DECLARED a delegation and the caller cannot satisfy it.

        Optional capability, discovered by `getattr` in provisioning exactly as `build_as`
        and `introspect_as` are, so the SQL providers keep their current behaviour: the broker
        already refuses them at query time and they need nothing here.

        Two things must both be true, and doing only one of them is a defect on its own:

        1. NO CONNECTOR IS BUILT, so no crawl runs. Building one would hand the factory
           `credential=None`, which `s3_connector_factory` reads as "use the box's ambient
           AWS identity". That fallback is correct for the self-host topology where the
           operator IS the credential owner and no delegation was declared (ADR 0024
           consequence 5) - and here it would crawl the OPERATOR's bucket on behalf of a user
           who never linked an identity.
        2. THE STORE STILL COMPOSES, into a state that can explain itself. Skipping it
           removes it from the catalog and the user is told "No data sources are connected
           yet" - a true sentence about the catalog and a useless one about their problem,
           which is the trade-off `load_manifest`'s own comment already refuses.

        No descriptor is registered: `sources.get` misses, freshness reads `never-synced`,
        and a sync cannot be started against a source there is no way to authenticate to.
        That is the honest shape - the store is empty, and it stays empty until the link
        exists."""
        identity = self._identity or InMemoryIdentity(config.get("user_groups", {}))
        sid = config["id"]
        obj = InMemoryObjectStore()
        index = InMemoryIndex(obj)
        qs = QueryService(index, identity, HashingEmbedding(), ExtractiveLlm(), obj,
                          tenant_id=sid)
        return ConnectorBackedStore(sid, config.get("business_unit", ""),
                                    config.get("title", sid),
                                    config.get("description", ""), qs, identity,
                                    descriptor=None, unavailable=reason)

    def build_as(self, config: dict, credential: "str | None" = None) -> ConnectorBackedStore:
        """#673, and #665's lesson carried onto this rail: probe and build make SEPARATE
        connectors, and the CRAWL runs off the built one. Credentialing only the probe would
        let Test-connection list the caller's bucket and then ingest nothing (or ingest as the
        wrong identity), which is the exact shape of the BigQuery gap #665 closed."""
        sid = config["id"]
        # #944: REUSE AN UNCHANGED STORE INSTEAD OF REBUILDING IT.
        #
        # Everything below builds a brand-new empty index and then submits a full crawl to
        # fill it - correct in itself, and catastrophic as a per-call cost, because the canvas
        # composes on every mount. Measured on prod 260823: a trip from /ask to Connectors
        # re-listed, re-downloaded, re-extracted, re-embedded and re-indexed every file in the
        # store (`docs_done 2, docs_total 2, docs_skipped 0`), spending Drive API quota and
        # re-opening the empty-index window on every page visit.
        #
        # The old comment further down - "a rebuilt store needs its OWN crawl" - is RIGHT, and
        # that is exactly why the crawl is not what gets dropped here: an empty index plus a
        # delta crawl is a store that answers nothing. What gets dropped is the rebuild.
        #
        # The recipe is the WHOLE entry, not a chosen subset. A subset is a list that has to be
        # updated every time somebody adds a field a factory reads, and the failure mode of
        # forgetting is silent: a store keeps serving the previous configuration. The cost of
        # being broad is that renaming a store re-crawls it once, which is rare and honest.
        recipe = _recipe(self.kind, config, credential)
        built = self._stores.get(sid)
        if (built is not None and sid in self._pipes
                and self._recipes.get(sid) == recipe
                and not _crawl_failed(self.sources, sid)):
            # A store whose last crawl FAILED is deliberately excluded: reusing it would make
            # the user's only recovery gesture - recompose - a no-op, and strand it empty
            # forever. That is the #941 family (a gesture that looks like it worked).
            return built
        connector = _build_connector(self._factory, config, credential)
        desc = SourceDescriptor(source_id=sid, kind=self.kind,
                                display_name=config.get("title", sid),
                                connector=connector)
        self.sources.register(desc)
        identity = self._identity or InMemoryIdentity(config.get("user_groups", {}))
        if self._shared_qs is not None:
            # #304/#306: wrap the edition's ingested index. The docs were already ingested by
            # edition.connect_sharepoint, so there is nothing to crawl here — the store simply
            # reads the shared QueryService, and its profile (doc titles) + has_content reflect
            # the real content. The descriptor carries freshness; sync() is a no-op (below).
            return ConnectorBackedStore(sid, config.get("business_unit", ""),
                                        config.get("title", sid),
                                        config.get("description", ""), self._shared_qs, identity,
                                        tenant_id=self.doc_tenant,   # #439
                                        descriptor=desc)
        obj = InMemoryObjectStore()
        index = InMemoryIndex(obj)
        embedder = HashingEmbedding()
        self._pipes[sid] = (obj, embedder, index)
        qs = QueryService(index, identity, embedder, ExtractiveLlm(), obj,
                          tenant_id=config["id"])  # both factories stamp config["id"] at ingest
        store = ConnectorBackedStore(sid, config.get("business_unit", ""),
                                     config.get("title", sid),
                                     config.get("description", ""), qs, identity,
                                     descriptor=desc)
        self._stores[sid] = store
        self._recipes[sid] = recipe      # #944: what this build was FOR, so the next compose
                                         # of the same thing can reuse it instead of re-crawling
        self._generation[sid] = self._generation.get(sid, 0) + 1
        # The initial full crawl is SUBMITTED, not run (ADR 0016 §1). It used to run inline
        # here, which is why composing a real library could not complete: `_build` was inside
        # the compose request, and #536's 40.2MB pack exceeded its 3600s timeout. The store is
        # returned immediately and is queryable throughout — empty at first, then filling.
        self.start_sync(sid, force_new=True)   # a rebuilt store needs its OWN crawl
        return store

    # ---------------------------------------------------------- the sync surface

    def owns(self, store_id: str) -> bool:
        try:
            self.sources.get(store_id)
            return True
        except KeyError:
            return False

    def purge(self, store_id: str) -> bool:
        """#947: DESTRUCTIVELY remove a connector store's ingested content and built state.

        #731 keeps delete non-destructive so Undo restores a store wholesale, but for a
        connector that leaves two things behind: the ingested chunks sit in the index (ACL'd
        to the deleter - "deleted" that isn't, a privacy gap), and a re-add REUSES the built
        store (#944), so a folder that changed while the source was gone serves stale content.
        Both are safe to fix here for one reason a connector has and an upload (#923) does not:
        the source of truth is the EXTERNAL folder, so a re-add re-crawls and nothing is lost.

        Deletes every document's chunks from the store's own index (the in-memory index the
        connector rail uses today, and any durable one #940 moves it to - `index.delete` is on
        the port), forgets the built store so a re-add rebuilds, bumps the generation so an
        in-flight crawl for the old build cannot commit onto the new one, and drops the
        descriptor + delta cursor so freshness resets to never-synced.

        Returns whether anything was held. Idempotent: purging an unknown id is False.
        """
        pipe = self._pipes.get(store_id)
        if pipe is not None:
            _obj, _emb, index = pipe
            try:
                # tenant_id == store_id for a connector store (build_as stamps config["id"]
                # as the tenant at ingest). list_doc_acls is the UNTRIMMED admin listing - the
                # right one here, because a purge removes the store's WHOLE content, not one
                # caller's view of it.
                from dbsearch.ports.base import as_read_scope
                lister = getattr(index, "list_doc_acls", None)
                if callable(lister):
                    for row in list(lister(as_read_scope(store_id))):
                        index.delete(store_id, row.doc_external_id)
            except Exception:
                logging.getLogger("dbsearch").warning(
                    "purge: could not enumerate/delete docs for %s", store_id, exc_info=True)
        held = (store_id in self._pipes or store_id in self._stores
                or self.owns(store_id))
        self._pipes.pop(store_id, None)
        self._stores.pop(store_id, None)
        self._recipes.pop(store_id, None)
        self._jobs.pop(store_id, None)
        self._generation[store_id] = self._generation.get(store_id, 0) + 1
        self.sources.remove(store_id)
        return held

    def summary(self, store_id: str) -> SourceSummary:
        return self.sources.get(store_id).summary()

    def _job_tenant(self) -> str:
        """The partition a job record belongs to (see `job_partition`). Falls back to the
        ADR 0012 doc tenant, then to "" - a self-host rig has exactly one workspace and no
        tid, and "" is a truthful name for it rather than a guess."""
        return self.job_partition or self.doc_tenant or ""

    def start_sync(self, store_id: str, *, force_new: bool = False) -> "IngestJob | None":
        """Submit the crawl and RETURN — the #454 fix. `None` means there was nothing to
        submit (a shared-index store, whose ingestion the edition owns).

        Callers get a job id, not a result. That is the point: on the #536 acceptance pack
        the result is more than an hour away, and an HTTP request cannot hold it."""
        desc = self.sources.get(store_id)                     # KeyError -> caller's 404
        if store_id not in self._pipes:
            # #304/#306: a shared-index store has no seed pipe here — the edition owns ingestion
            # (edition.connect_sharepoint / resync_source), so a router-side sync is a no-op.
            return None
        self.sources.mark_syncing(store_id)
        gen = self._generation.get(store_id, 0)
        job = self._runner.submit(
            tenant_id=self._job_tenant(), source_id=store_id, cursor=desc.cursor,
            force_new=force_new,
            work=lambda cp, cursor: self._crawl(store_id, gen, cp, cursor))
        self._jobs[store_id] = job.job_id
        return job

    def _crawl(self, store_id: str, generation: int, checkpoint, cursor: "str | None") -> None:
        """The crawl itself, on the worker thread. Everything that persists sync-state lives
        here so the runner stays connector-agnostic."""
        desc = self.sources.get(store_id)
        obj, embedder, index = self._pipes[store_id]

        def _commit(result) -> None:
            if self._generation.get(store_id, 0) != generation:
                # This store was rebuilt while the crawl ran: `desc`, `index` and the pipe
                # above all belong to a discarded generation. Committing would stamp this
                # crawl's cursor and doc_count onto the CURRENT descriptor while the current
                # index knows nothing about those documents — a store reporting content it
                # cannot return.
                logging.getLogger("dbsearch").info(
                    "discarding ingest result for %s: store rebuilt mid-crawl", store_id)
                return
            # Runs BEFORE the job is published as succeeded (jobs.JobCheckpoint.complete).
            # Only now — the batch completed, so the source's next cursor is safe to persist.
            # A crawl that raised leaves desc.cursor exactly where it was, which is what makes
            # the resume re-list the same batch instead of skipping its unprocessed remainder.
            summary = self.sources.record_sync(store_id, cursor=result.cursor,
                                               doc_count=result.doc_count, at=self._now(),
                                               unreadable=result.unreadable,
                                               full_crawl=result.full_crawl,
                                               created=result.created,
                                               deleted=result.deleted)
            store = self._stores.get(store_id)
            if store is not None:
                store.ingest_completed()   # routing profile now reflects real content
            if self._on_sync is not None:
                self._on_sync(summary)     # LAW 8: metadata-only telemetry hook

        checkpoint.on_commit = _commit
        try:
            run_ingestion(desc.connector, InMemoryQueue(), obj,
                          LocalRichExtractor(), embedder, index,
                          cursor=cursor, checkpoint=checkpoint)
        except Exception:
            self.sources.record_error(store_id)
            raise

    def sync(self, store_id: str) -> SourceSummary:
        """Blocking re-crawl: submit, then wait. Kept for rigs, the CLI and tests — NOT for a
        request handler, which is what `start_sync` is for. The crawl runs on the same worker
        and through the same job record either way, so this is the async path with a wait on
        the end, never a second code path that could drift from it."""
        job = self.start_sync(store_id)
        if job is None:
            return self.sources.get(store_id).summary()
        self._runner.wait(job.job_id)
        final = self._runner.job(job.job_id)
        if final is not None and final.status == "failed":
            raise IngestFailed(f"ingest of {store_id!r} failed: {final.error}")
        return self.sources.get(store_id).summary()

    def build_isolated(self, config: dict) -> ConnectorBackedStore:
        """A throwaway store for a health check, built without touching live state.

        `build()` is a MUTATING operation on this provider: it re-registers the source
        descriptor, replaces the store's index pipe, and submits a crawl. `health.py` calls
        build() to get something it can exercise, which was merely wasteful while ingest was
        inline - the throwaway build re-crawled into its own index and the shared descriptor
        ended up looking synced either way. With #454 it became destructive: the throwaway
        build superseded the LIVE store mid-crawl, so the real crawl discarded its own result
        and the composed store sat on `syncing` forever with an empty index. It reproduced
        every time through the conversational setup flow, which health-checks what it builds.

        A private clone shares nothing - own registry, own pipes, own job runner - so the
        exercise is honest and the live store is untouched. The wait is BOUNDED because this
        runs inside a user's Test-connection request; a library too large to finish in the
        window exercises as empty, which the strategy already reports as degraded rather than
        healthy."""
        # No job_store: the clone gets its own in-memory one. A health check must not write
        # job rows into the deployment's durable table, where `resumable()` would later find
        # them and try to continue a throwaway crawl.
        clone = ConnectorStoreProvider(self.kind, self._factory, identity=self._identity,
                                       now=self._now,
                                       shared_query_service=self._shared_qs,
                                       doc_tenant=self.doc_tenant)
        store = clone.build(config)
        clone.wait_for_ingest(config["id"], timeout=_HEALTH_INGEST_TIMEOUT)
        clone._runner.shutdown()
        return store

    def active_jobs(self) -> dict[str, str]:
        """store_id -> the id of the crawl most recently submitted for it (#454), so the
        compose response can hand the caller something to poll."""
        return dict(self._jobs)

    def wait_for_ingest(self, store_id: str, timeout: float | None = None):
        """Block until this store's in-flight crawl finishes. For tests and rigs that compose
        and then immediately ask a question."""
        job_id = self._jobs.get(store_id)
        return self._runner.wait(job_id, timeout=timeout) if job_id else None
