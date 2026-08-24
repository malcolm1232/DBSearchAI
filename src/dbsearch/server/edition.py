"""Build the self-host data plane + a single-document ingest helper.

`SELFHOST_BACKEND` selects:
  - "memory"          : in-memory everything (tests/dev, no Docker)
  - "pgvector-ollama" : Postgres+pgvector index + filesystem store + Ollama model (real)

Identity is a static user→groups map (no Entra in self-host): loaded from `USERS_FILE`
(JSON) or a small demo default. Permissions still apply (LAW 2) — the map is the source
of truth for who's in which group, and ingest sets each doc's allowed groups.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from dbsearch.admin.service import AdminService
from dbsearch.agents.draft_session import DraftSessionService, DraftTurn
from dbsearch.agents.proposal import ProposalAgent, ProposalDraft
from dbsearch.api.auth import set_api_key_resolver
from dbsearch.api.grants import GrantAwareIdentity, GrantRegistry
from dbsearch.api.keys import ApiKeyRecord, ApiKeyRegistry
from dbsearch.audit import AuditLogUnavailable, InMemoryAuditLog, PgAuditLog
from dbsearch.connectors.registry import SourceDescriptor, SourceRegistry, SourceSummary
from dbsearch.connectors.sharepoint import SharePointConnector
from dbsearch.controlplane.agent import DataPlaneAgent
from dbsearch.controlplane.plane import ControlPlane
from dbsearch.core.models import CorpusStatus
from dbsearch.pipeline.runner import run_ingestion
from dbsearch.ports.base import (EmbeddingPort, IdentityPort, IndexPort, ObjectStorePort,
                                 ReadScope, as_read_scope, expand_principals)
from dbsearch.query import ConversationService, QueryService
from dbsearch.server.conversation_shares import ConversationShareRegistry, PgConversationShareStore
from dbsearch.server.conversation_store import PgConversationStore
from dbsearch.server.grant_store import PgGrantStore
from dbsearch.server.source_sync_store import PgSourceSyncStore

_DEMO_USERS = {
    "alice": ["all-staff", "deal-team"],
    "bob": ["all-staff"],
}


def _load_users() -> dict:
    path = os.environ.get("USERS_FILE", "")
    if path and os.path.exists(path):
        return json.loads(open(path).read())
    return _DEMO_USERS


@dataclass
class Edition:
    query_service: QueryService
    conversation_service: ConversationService
    proposal_agent: ProposalAgent
    draft_session: "DraftSessionService"   # #57 two-phase conversational proposal draft
    index: IndexPort
    store: ObjectStorePort
    embedder: EmbeddingPort
    identity: IdentityPort    # Fix A: principal expansion for the verify-data preview (LAW 2)
    tenant_id: str
    backend: str
    users: list[str]          # known identities (dev-switcher list; empty when not dev-auth)
    control_plane: ControlPlane
    agent: DataPlaneAgent
    admin_service: AdminService
    embedding_model: str
    source_registry: SourceRegistry
    api_key_registry: ApiKeyRegistry
    grant_registry: GrantRegistry     # ADR 0017 (#538): who may read a shared document
    conversation_shares: ConversationShareRegistry  # #600: who may open a shared conversation
    chat_models: dict          # #43: name -> LlmPort, the selectable generation models
    chat_model_default: str    # the model used when the request doesn't pick one
    audit_log: "InMemoryAuditLog | PgAuditLog"   # #45 audit trail, #623 durable when a DSN is set
    demo_chat_llm: "object"    # #280 (ADR 0009): the DEMO scope's generation model - resolved
    demo_chat_model: str       # separately from the live default so the anon demo never uses a
                               # paid/tenant-crossing key (Groq llama-3.3-70b, else Extractive)
    # #569 / ADR 0016: the EDITION's ingest rail runs off the request too. #454 moved the
    # router rail; this is the same runner and the same job records for the flagship
    # SharePoint path, which was still crawling inline with no checkpoint at all. Defaulted
    # so every existing constructor call keeps working; `build_edition` fills them in.
    ingest_jobs: "object | None" = None
    ingest_runner: "object | None" = None

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ---------------------------------------------------------------- ingest jobs (#569)

    def ingest_job(self, job_id: str):
        """The job record, or None. What a caller polls instead of holding a request open."""
        return self.ingest_runner.job(job_id) if self.ingest_runner is not None else None

    def await_ingest(self, job_id: str, timeout: "float | None" = None):
        """Block until this crawl leaves the pool. For rigs, tests and the CLI - NEVER for a
        request handler, which is the thing #569 exists to get crawls out of. Mirrors
        `ConnectorStoreProvider.sync`: the async path with a wait on the end, never a second
        code path that could drift from it."""
        if self.ingest_runner is None:
            return None
        return self.ingest_runner.wait(job_id, timeout=timeout)

    def _submit_crawl(self, source_id: str, connector, *, cursor: "str | None",
                      job_tenant: str, progress=None, on_done=None,
                      event: str = "ingest.completed", force_new: bool = False):
        """Submit one crawl and return its job record immediately.

        Everything that persists sync-state lives in `_crawl` below, so the runner stays
        connector-agnostic - the same split `ConnectorStoreProvider` uses, deliberately, so
        the two rails cannot drift into two different resume semantics.
        """
        from dbsearch.pipeline.worker import IngestJobRunner

        if self.ingest_runner is None:                 # a rig constructed without a runner
            from dbsearch.pipeline.jobs import InMemoryJobStore
            self.ingest_jobs = self.ingest_jobs or InMemoryJobStore()
            self.ingest_runner = IngestJobRunner(self.ingest_jobs)
        self.source_registry.mark_syncing(source_id)
        return self.ingest_runner.submit(
            tenant_id=job_tenant, source_id=source_id, cursor=cursor, force_new=force_new,
            work=lambda cp, cur: self._crawl(source_id, connector, cp, cur, progress,
                                             on_done, event))

    def _crawl(self, source_id: str, connector, checkpoint, cursor: "str | None",
               progress=None, on_done=None, event: str = "ingest.completed") -> None:
        """The crawl itself, on the worker thread.

        THE TRAP (ADR 0016, recorded on #454 and repeated here because it is the one failure
        that is silent): `ConnectorPort.list_changes` advances its cursor PER BATCH, not per
        item. The source's next cursor is therefore persisted in `_commit` - which
        `JobCheckpoint.complete` runs only once the batch has finished - and NEVER earlier. A
        crawl that raises leaves the descriptor's cursor exactly where it was, so the resume
        re-lists the same batch instead of skipping its unprocessed remainder. Per-DOCUMENT
        progress is what the checkpoint records; the cursor is what stays pinned.
        """
        from dbsearch.adapters.local import InMemoryQueue, LocalRichExtractor

        def _commit(result) -> None:
            # Runs BEFORE the job is published as succeeded, so polling then reading can
            # never see the job and the source disagree.
            ts = self._now()
            self.source_registry.record_sync(source_id, cursor=result.cursor,
                                             doc_count=result.doc_count, at=ts,
                                             unreadable=result.unreadable,
                                             full_crawl=result.full_crawl,
                                             created=result.created,
                                             deleted=result.deleted)
            # The two rails kept DIFFERENT telemetry events before #569 and still must: a
            # first connect is `ingest.completed`, a re-crawl is `source.synced`. Collapsing
            # them into one looked like tidying and is a silent contract change for anything
            # reading the boundary-validated event stream (LAW 8).
            if event == "source.synced":
                self.agent.emit(
                    "source.synced",
                    counts={"sources_synced": 1, "docs_indexed": result.doc_count},
                    health={"last_index_ts": ts},
                    ts=ts,
                )
            else:
                self.agent.emit(
                    "ingest.completed",
                    counts={"docs_indexed": result.doc_count,
                            "chunks_created": result.doc_count},
                    health={"index_ready": True, "last_index_ts": ts},
                    ts=ts,
                )

        checkpoint.on_commit = _commit
        try:
            result = run_ingestion(connector, InMemoryQueue(), self.store,
                                   LocalRichExtractor(), self.embedder, self.index,
                                   cursor=cursor, checkpoint=checkpoint, progress=progress)
        except Exception as exc:
            self.source_registry.record_error(source_id)
            # `on_done` is how a CALLER publishes the terminal state to its own surface -
            # the crawl no longer ends inside the request, so the request can no longer be
            # the thing that reports the outcome. It runs on the worker thread and must not
            # be allowed to mask the real failure (#365: a live phase left in the progress
            # store hangs the canvas forever, which is the failure this replaces).
            if on_done is not None:
                try:
                    on_done(None, exc)
                except Exception:
                    logging.getLogger("dbsearch").exception(
                        "ingest completion hook failed for %s", source_id)
            raise
        if on_done is not None:
            try:
                on_done(result, None)
            except Exception:
                logging.getLogger("dbsearch").exception(
                    "ingest completion hook failed for %s", source_id)

    def resolve_chat_llm(self, model: "str | None"):
        """Pick a generation model by name (#43). Unknown/None -> the default. Never errors,
        so a stale UI selection can't break a query; retrieval/trim is unaffected either way."""
        if model and model in self.chat_models:
            return self.chat_models[model]
        return self.chat_models[self.chat_model_default]

    def ingest_document(self, external_id: str, title: str, text: str, acl: list[str], uri: str = "",
                        tenant_id: "str | None" = None, owner_oid: "str | None" = None) -> None:
        """Ingest one document through the full pipeline (reuses the seed connector for a
        single item, so chunk/embed/index is identical to the batch path). `acl` = the
        group ids allowed to read it. Emits boundary-validated telemetry (LAW 8).
        ADR 0012: `tenant_id` is the CALLER's canonical partition (resolve_tenant) — the
        chunks land there; `owner_oid` records who ingested (attribution only)."""
        from dbsearch.adapters.local import InMemoryQueue, LocalRichExtractor

        tid = tenant_id or self.tenant_id
        # #842: the size rides the seed item so `usage_bytes` can see this document. Text a
        # caller POSTs to /ingest is entrusted content under ADR 0027 rule 3, the same as an
        # upload; before this it indexed with doc_bytes defaulting to 0, so the meter read
        # empty however much was stored. A real SharePoint crawl's items carry no such key
        # and stay 0, which is rule 1: connector content is never metered.
        seed = [{"external_id": external_id, "title": title, "uri": uri, "acl": acl,
                 "text": text, "doc_bytes": len(text.encode("utf-8"))}]
        connector = SharePointConnector(tenant_id=tid, seed=seed, owner_oid=owner_oid)
        # #845, second home (the #799 lesson: fix every home of a rule, not the one you were
        # looking at). This path writes the raw blob the same way, so a raise here strands it
        # the same way - less often, because text almost always parses, but identically.
        try:
            run_ingestion(connector, InMemoryQueue(), self.store, LocalRichExtractor(),
                          self.embedder, self.index)
        except Exception:
            self._reclaim_orphan_blobs(tid, external_id)
            raise

        ts = self._now()
        self.agent.emit(
            "ingest.completed",
            counts={"docs_indexed": 1, "chunks_created": 1 if text.strip() else 0},
            health={"index_ready": True, "last_index_ts": ts},
            ts=ts,
        )

    def _indexed(self, partition: str, doc_id: str) -> bool:
        """Does the index still hold rows for this document? #845 reclaims blobs only when
        the answer is NO, so a reclaim can never take the bytes of a live document."""
        counter = getattr(self.index, "chunk_count_for", None)
        if counter is not None:
            try:
                return bool(counter(partition, doc_id))
            except Exception:
                return True                  # cannot tell -> assume live, keep the blobs
        try:
            return any(d.doc_external_id == doc_id
                       for d in self.index.list_doc_acls(as_read_scope(partition)))
        except Exception:
            return True

    def _reclaim_orphan_blobs(self, partition: str, doc_id: str) -> None:
        """#845: an ingestion that RAISED leaves the raw bytes it already wrote behind.

        `run_ingestion` stores the raw blob before extraction runs, so an unparseable upload
        (415/422) unwinds with `raw/{partition}/{doc_id}` on the volume and NO index row -
        and both blob-deleting paths in the product, `DELETE /documents` and the retention
        sweep, enumerate FROM the index. Nothing could ever reach those bytes again. Observed
        on prod: a rejected upload's raw blob was still on the live disk afterwards, and
        rejected uploads are exactly the ones people retry.

        GUARDED ON ORPHANHOOD, not on the exception: if the index still holds rows for this
        id the bytes belong to a live document and are left alone. That matters because a
        re-ingest of an EXISTING id deletes-before-indexing inside run_ingestion, so a
        failure can land with the id half-written; deleting on the exception alone would then
        destroy a document that is about to be, or already is, readable.
        """
        if self._indexed(partition, doc_id):
            return
        self._reclaim_blobs(partition, doc_id)

    def _reclaim_blobs(self, partition: str, doc_id: str) -> None:
        """#841: erase the blob families of a document this ingest just superseded.

        Superseding removed only the INDEX rows, and every blob-deleting path we have —
        `DELETE /documents` and the retention sweep — enumerates from the index, so a
        superseded version's `raw`/`segments`/`chunk` blobs became unreachable the moment
        its rows went and could never be reclaimed by anything. Every edited re-upload
        leaked its predecessor's bytes onto the volume the #831 headroom guard defends.

        Best-effort on purpose: the index rows are already gone, so the document is
        superseded whatever happens here, and a store that cannot delete a prefix
        (`NotImplementedError`) must not turn a successful upload into a 500.
        """
        from dbsearch.server import retention   # imports back into this package (cycle)

        for prefix in retention.blob_prefixes(partition, doc_id):
            try:
                self.store.delete_prefix(prefix)
            except NotImplementedError:
                return
            except Exception:
                logging.getLogger("dbsearch").error(
                    "supersede: blob delete failed for one prefix")

    def ingest_file(self, external_id: str, title: str, data: bytes, mime: str,
                    acl: list[str], uri: str = "",
                    tenant_id: "str | None" = None, owner_oid: "str | None" = None) -> int:
        """Ingest one uploaded file (raw bytes + mime) through the full pipeline with the
        local rich extractor. `acl` = group ids allowed to read it. Returns chunk count.
        Emits boundary-validated `ingest.completed` telemetry (LAW 8)."""
        from dbsearch.adapters.local import InMemoryQueue, LocalRichExtractor
        from dbsearch.connectors.upload import UploadConnector

        # #90: the external_id is CONTENT-addressed, so an edited re-upload of the same
        # file mints a new id — the uri (upload://filename) is the stable logical
        # identity, and any doc sharing that uri under a different id must be superseded
        # so stale content with a stale (possibly looser) ACL cannot linger invisibly
        # (LAW 2 freshness).
        #
        # #841: the supersede runs AFTER a successful ingestion, never before it. Deleting
        # first meant a re-upload that fails to parse — a scanned image, a corrupt file —
        # raised 415/422 to the user with their PREVIOUS good version already gone: an
        # error and a data loss for one ordinary action. Ordering it after costs a window,
        # inside this one request, where both versions are indexed; that window exposes
        # nothing the old version was not already exposing for as long as it has existed,
        # and it is bounded by a request rather than being permanent.
        tid = tenant_id or self.tenant_id
        connector = UploadConnector(tid, external_id, title, data, mime, acl, uri,
                                    owner_oid=owner_oid)
        # strict: one interactive file, so an unparseable upload must surface (415/422) rather
        # than be skipped as a crawl would — a silent skip 200s back a zero-chunk ingest (#181).
        # #845: strict means this RAISES, and the raw blob is already written by then, so the
        # failure path reclaims what it orphaned instead of leaving it unreachable forever.
        try:
            result = run_ingestion(
                connector, InMemoryQueue(), self.store,
                LocalRichExtractor(), self.embedder, self.index, strict=True,
            )
        except Exception:
            self._reclaim_orphan_blobs(tid, external_id)
            raise
        return self._finish_file_ingest(tid, external_id, uri, owner_oid, result)

    def _finish_file_ingest(self, tid: str, external_id: str, uri: str,
                            owner_oid: "str | None", result) -> int:
        """The post-success tail shared by ingest_file and submit_file_ingest (#917):
        supersede-by-uri, then the LAW 8 telemetry. ONE home, so the sync and async
        paths cannot drift on what a completed upload means."""
        if uri:
            # Ingest is a WRITE path: own partition only, never a doorway (ADR 0019 D3).
            # #791: supersede is OWNER-scoped — a filename is only a stable identity
            # within one owner's uploads. None == None keeps the single-owner path
            # superseding (#90); None vs someone never deletes another's doc (fails safe).
            # ADR 0012 forbids owner-gating retrieval; this is a write/delete gate.
            for doc in self.index.list_doc_acls(as_read_scope(tid)):
                if doc.uri == uri and doc.doc_external_id != external_id \
                        and doc.owner_oid == owner_oid:
                    self.index.delete(tid, doc.doc_external_id)
                    self._reclaim_blobs(tid, doc.doc_external_id)
        # Real chunk count when the index exposes it (#49); fall back to doc_count otherwise.
        _counter = getattr(self.index, "chunk_count_for", None)
        chunk_count = _counter(tid, external_id) if _counter else result.doc_count

        ts = self._now()
        self.agent.emit(
            "ingest.completed",
            counts={"docs_indexed": result.doc_count, "chunks_created": chunk_count},
            health={"index_ready": True, "last_index_ts": ts},
            ts=ts,
        )
        return chunk_count

    def submit_file_ingest(self, external_id: str, title: str, data: bytes, mime: str,
                           acl: list[str], uri: str = "",
                           tenant_id: "str | None" = None,
                           owner_oid: "str | None" = None):
        """#917: the ASYNC sibling of ingest_file - LAW 4 for the upload path.

        Submits the single-file crawl through the SAME job runner a connector crawl uses
        and returns the job record immediately; parse -> chunk -> embed -> index run on
        the worker, and /ingest/jobs/{job_id} reports the runner's real phases
        (extracting/embedding/indexing) - which is what lets the canvas show the
        SharePoint-grade stepper for uploads.

        The SourceRegistry is deliberately NOT involved: an upload is document-plane
        state whose per-user surface is /admin/documents; a registry row would be a
        deployment-wide count on the wrong surface (#550's shape). strict=True keeps the
        #181 contract - an unparseable file FAILS the job (error class in the job body,
        the async home of the old 422) instead of 200ing back a zero-chunk ingest.
        """
        from dbsearch.adapters.local import InMemoryQueue, LocalRichExtractor
        from dbsearch.connectors.upload import UploadConnector
        from dbsearch.pipeline.worker import IngestJobRunner

        tid = tenant_id or self.tenant_id
        connector = UploadConnector(tid, external_id, title, data, mime, acl, uri,
                                    owner_oid=owner_oid)
        if self.ingest_runner is None:                 # a rig constructed without a runner
            from dbsearch.pipeline.jobs import InMemoryJobStore
            self.ingest_jobs = self.ingest_jobs or InMemoryJobStore()
            self.ingest_runner = IngestJobRunner(self.ingest_jobs)

        def _work(cp, cur) -> None:
            try:
                result = run_ingestion(connector, InMemoryQueue(), self.store,
                                       LocalRichExtractor(), self.embedder, self.index,
                                       strict=True, checkpoint=cp)
            except Exception:
                # #845: the raw blob is written before the parse, so the failure path
                # reclaims what it orphaned - same rule as the sync path.
                self._reclaim_orphan_blobs(tid, external_id)
                raise
            self._finish_file_ingest(tid, external_id, uri, owner_oid, result)

        # force_new: every upload mints a content-addressed external_id, so there is no
        # meaningful "resume" of a previous submit to adopt.
        return self.ingest_runner.submit(tenant_id=tid, source_id=f"upload:{external_id}",
                                         cursor=None, force_new=True, work=_work)

    def connect_sharepoint(self, az_tenant_id: str, drive_id: str, connector=None,
                           folder_path: str | None = None, progress=None,
                           tenant_id: "str | None" = None,
                           owner_oid: "str | None" = None, on_done=None) -> dict:
        """S2 (#148): ingest a just-consented customer's SharePoint library into this data
        plane and register it as a queryable source. `connector` is injectable for tests; in
        prod it's a GraphSharePointConnector authenticating with our multi-tenant app against
        the customer tenant. This only ADDS a source — retrieval trim is unchanged (the same
        QueryService + EntraIdentity group expansion enforce LAW 2 at query time)."""
        import os

        from dbsearch.adapters.local import InMemoryQueue, LocalRichExtractor

        # #300: a resolved sharing link scopes the crawl to ONE folder; otherwise fall back
        # to the deployment-wide SP_INGEST_FOLDER env (unchanged default behaviour).
        # #883: resolved OUTSIDE the branch below because it is persisted as part of the
        # connector recipe, and a test that injects a connector must still record the folder
        # the crawl actually used rather than leaving the durable row lying about it.
        folder = folder_path if folder_path is not None else os.environ.get("SP_INGEST_FOLDER", "")

        if connector is None:
            from dbsearch.connectors.sharepoint_graph import GraphSharePointConnector
            cid = os.environ.get("SP_CONNECTOR_CLIENT_ID", "")
            secret = os.environ.get("SP_CONNECTOR_CLIENT_SECRET", "")
            if not (cid and secret):
                raise RuntimeError("SharePoint connector not configured (SP_CONNECTOR_* env)")
            name_contains = os.environ.get("SP_INGEST_FILE_CONTAINS", "")  # optional: scope to matching files
            connector = GraphSharePointConnector(tenant_id or self.tenant_id, drive_id,
                                                 az_tenant_id, cid, secret, folder, name_contains,
                                                 owner_oid=owner_oid)

        # #569 / ADR 0016: the source is REGISTERED FIRST and the crawl is SUBMITTED, not run.
        # It used to run inline here, which is the LAW 4 violation ADR 0016 exists to remove -
        # on #536's 40.2MB / 4884-document pack the crawl is more than an hour, and an HTTP
        # request cannot hold it. Registering before submitting is what lets the canvas find a
        # source that is still filling, rather than a source that does not exist yet.
        source_id = f"sharepoint:{az_tenant_id}"
        self.source_registry.register(SourceDescriptor(
            source_id=source_id, kind="sharepoint",
            display_name=f"SharePoint — {az_tenant_id}", connector=connector,
            # #577: pin the job workspace on the descriptor so a later resync looks a job up
            # under the SAME (tenant, source) pair this crawl records it against.
            job_tenant=tenant_id or self.tenant_id,
            # #883: the recipe for rebuilding this connector after a restart. Without it the
            # source simply ceased to exist on the next deploy - drive_id and the resolved
            # folder arrive on THIS request and were written down nowhere, so the node reverted
            # to "never-synced" over a corpus that was still answering questions.
            config={"az_tenant_id": az_tenant_id, "drive_id": drive_id,
                    "folder_path": folder, "owner_oid": owner_oid},
        ))
        job = self._submit_crawl(
            source_id, connector, cursor=None,
            # #565: the job partition is the WORKSPACE, not the Entra tid. A durable job store
            # is one table shared by every workspace, and `resumable(tenant, source_id)` looks
            # a job up by that pair - two workspaces connecting the same drive would otherwise
            # be handed each other's job, and a resume SKIPS the documents that job recorded,
            # into an index that never saw them.
            job_tenant=tenant_id or self.tenant_id,
            progress=progress,   # #302: live indexing progress for the UI (no-op if None)
            on_done=on_done,     # #569: the worker publishes the terminal state, not the request
            # A fresh connect is a fresh crawl: this drive was just consented to, so a
            # non-terminal job left over from a previous connection is not a continuation of it.
            force_new=True)
        # `docs_indexed` is deliberately GONE from this return rather than reported as 0. A
        # caller that read a zero here would render "0 documents indexed" for a crawl that had
        # merely not started - the "is it broken or is it working?" question this card exists
        # to answer. Poll the job.
        # `job_status`, never a bare `status`: the caller's own payload already uses
        # `status` for the CONNECTION ("connected" / "ingesting"), and two different meanings
        # of one key in one response is a collision waiting to be read wrong - it silently
        # was, the first time this shipped.
        return {"source_id": source_id, "tenant": az_tenant_id,
                "drive_id": drive_id, "job_id": job.job_id, "job_status": job.status}

    def document_segments(self, doc_external_id: str, user_oid: str,
                          scope=None) -> list[dict]:
        """Verify-data read: this document's chunks with locators + text preview, TRIMMED to
        the CALLER's principals (LAW 2) — same rule as search(): a chunk is only visible if
        its allowed_principals intersects the caller's expanded principal set. A caller with
        no matching principals gets an empty list (never a 404 — that would leak the doc's
        existence). LAW 1: the preview is data-plane content shown only to an authenticated
        AND authorized operator, never uplinked."""
        #582: `scope` was missing entirely and this used the DEPLOYMENT CONSTANT, so on a
        # hosted multi-tenant box it read the wrong partition (the #439 bug class, never
        # carried across to this path). It now takes the caller's scope like every other
        # read, which also lets a grantee open the segments of a document shared with them.
        principals = self.identity.expand_groups(user_oid)
        return self.index.list_doc_segments(as_read_scope(scope, self.tenant_id),
                                            doc_external_id, principals)

    def document_bundle(self, doc_external_id: str, user_oid: str,
                        tenant_id=None) -> "dict | None":
        """#562: one document as it was ingested - the original bytes AND the extracted text.

        The extracted text is the half that matters for a snapshot: a PDF that parsed to
        garbage looks perfect in a listing, and the text is what the answer engine can
        actually see. The original rides along so the two can be compared.

        Returns None when the document does not exist OR the caller holds no grant on it.
        Deliberately the same answer for both, so the caller cannot use this to discover that
        a document exists - a title is routinely the whole secret ("Q3 redundancy list").

        NO `unrestricted` parameter, unlike list_documents. #549 gave the OPERATOR an
        untrimmed metadata listing because whoever runs a deployment answers for its
        contents. Content is not metadata. "No document a user can't already open is ever
        retrieved" is the product's central promise, and an operator escape hatch on the
        CONTENT path is precisely the hole it forbids; an operator who needs to read a
        document can be granted it like anyone else (#538, ADR 0017).

        Both object-store keys are the ones the pipeline writes (runner.py `raw/...` and
        `segments/...`), derived rather than looked up - DocACL carries no content_ref.
        """
        scope = as_read_scope(tenant_id, self.tenant_id)
        # #582: the blob keys are `raw/{PARTITION}/{doc}` - and for a document reached
        # through the doorway that partition is the GRANTOR's, not the caller's. So resolve
        # which partition actually holds it before touching the object store; using the
        # caller's would 404 every shared download while the metadata read succeeded.
        # Own partition first, so a caller's own document can never be shadowed by a share.
        candidates = [scope.partition] + sorted(
            t for (t, d) in scope.doorway if d == doc_external_id and t != scope.partition)
        tid, doc = None, None
        for candidate in candidates:
            doc = next((d for d in self.index.list_doc_acls(ReadScope(partition=candidate))
                        if d.doc_external_id == doc_external_id), None)
            if doc is not None:
                tid = candidate
                break
        if doc is None:
            return None
        principals = set(self.identity.expand_groups(user_oid))
        if not (set(doc.allowed_principals or []) & principals):
            return None                       # default-deny, and silent about why

        def _fetch(key):
            try:
                return self.store.get(key)
            except Exception:
                # A bundle missing one half is still worth serving: an older document may
                # predate a key, and "no original retained" is a better answer than a 500.
                return None

        raw = _fetch(f"raw/{tid}/{doc_external_id}")
        segments = _fetch(f"segments/{tid}/{doc_external_id}")
        text = None
        if segments:
            try:
                text = "\n\n".join(s.get("text", "") for s in json.loads(segments.decode()))
            except Exception:
                text = None
        return {"doc_external_id": doc_external_id, "title": doc.title, "uri": doc.uri,
                "original": raw, "text": text}

    def list_documents(self, user_oid: str, tenant_id=None,
                       unrestricted: bool = False) -> list:
        """The document listing, TRIMMED to the caller's own principals (#549).

        Same rule and the same identity port as `document_segments` and `corpus_status`, so
        the listing can never show more than a query would - which is the property #87 fixed
        for a document's BODY and nobody carried across to the listing that names it. A
        title is routinely the whole secret ("Q3 redundancy list"), and `allowed_principals`
        names exactly who may read it, so an untrimmed listing hands an unauthorized reader
        both the existence of the document and the target list (LAW 2).

        `unrestricted` is the OPERATOR's view - whoever runs the deployment answers for its
        contents and needs to see all of it. Not a way to opt out of the trim.

        `tenant_id` overrides the deployment constant, as ADR 0012 does for retrieval:
        passing the constant here is the #439 bug class, and on a hosted multi-tenant
        deployment it would read across tenants."""
        docs = self.index.list_doc_acls(as_read_scope(tenant_id, self.tenant_id))
        if unrestricted:
            return docs
        principals = set(self.identity.expand_groups(user_oid))
        return [d for d in docs if principals & set(getattr(d, "allowed_principals", []) or [])]

    def corpus_status(self, user_oid: str, tenant_id=None) -> "CorpusStatus | None":
        """#392: what the Ask surface may honestly promise this caller before they type.

        Expands the caller's principals through the SAME identity port as search and
        document_segments, so the count cannot disagree with what a query would retrieve
        (LAW 2). `tenant_id` overrides the deployment constant for the hosted multi-tenant
        path, exactly as ADR 0012 does for retrieval.

        Returns None when the backend has no corpus_status() - an honest "unknown", which
        the caller must render as silence rather than as "empty". Claiming an empty corpus
        on a backend that simply cannot count would be a new honesty bug in the fix for an
        honesty bug."""
        # #601 / ADR 0020: the ONE other expansion that must be conversation-aware. LAW 5
        # already requires this denominator to use the same per-request scope as the
        # retrieval it accompanies, and after ADR 0020 that scope carries the active
        # conversation too - a footer that expanded conv-scoped principals blindly would
        # count documents the answer beside it could not retrieve, which is the #392
        # mismatch in the other direction.
        scope = as_read_scope(tenant_id, self.tenant_id)
        principals = expand_principals(self.identity, user_oid, scope)
        try:
            return self.index.corpus_status(scope, principals)
        except NotImplementedError:
            return None

    def record_query_served(self, authorized_docs: int) -> None:
        """Emit query telemetry (LAW 8). Metadata only - a count of RETRIEVED docs.

        The counter is still named `authorized_docs` on the wire because dashboards and the
        boundary contract test pin that key; the number it carries has always been retrieval
        (#393). Renaming the metric is a separate, breaking change and is carded."""
        self.agent.emit(
            "query.served",
            counts={"queries_served": 1, "authorized_docs": authorized_docs},
            ts=self._now(),
        )

    def record_query(self, user: str, question: str, result, surface: str = "ask") -> None:
        """Record a served query: boundary telemetry (counts, LAW 8) + an in-tenant audit
        trail (#45) for access-reason transparency. The audit stores doc identities + the
        user's own question — never document content (LAW 1).

        #623: THE AUDIT WRITE DOES NOT FAIL THE QUESTION, and unlike most fail-open decisions
        in this repo that is not a trade-off between safety and convenience - it is about
        WHERE this runs. Every caller invokes this AFTER the answer exists, and one of them
        (`chat/stream` in app.py) invokes it after the answer has already been streamed to
        the browser: raising there cannot un-send those bytes, it can only turn a delivered
        answer into a 500 that also loses it. Nothing about the user's authorization depends
        on this row, so a failed write costs a governance record and never a wrong answer.

        It is LOUD, though, which is the whole difference between this and the bug it
        replaces. A lost row is logged at ERROR with a data-free reason; what #623 actually
        was is that EVERY row vanished on restart with nothing said anywhere. The READ side
        makes the opposite choice on purpose - /me/questions and /admin/audit 503 rather than
        answer an empty list - because there a failure can still be told truthfully before
        anybody has been shown anything."""
        docs = list(result.retrieved_docs)
        self.record_query_served(len(docs))
        try:
            self.audit_log.record(user, question, surface, docs, self._now())
        except AuditLogUnavailable as exc:
            logging.getLogger("dbsearch").error(
                "audit write dropped for one %s query: %s", surface, exc)

    def draft_proposal(self, user_oid: str, brief: str) -> ProposalDraft:
        """Run the proposal agent and emit metadata-only telemetry (LAW 8)."""
        draft = self.proposal_agent.draft(user_oid, brief)
        retrieved = sum(len(s.retrieved_docs) for s in draft.sections)
        self.agent.emit(
            "proposal.drafted",
            counts={"proposals_drafted": 1, "sections": len(draft.sections),
                    "subquestions": len(draft.plan), "authorized_docs": retrieved},
            ts=self._now(),
        )
        return draft

    def draft_turn(self, user_oid: str, conv_id: str, message: str = "",
                   intent: str = "chat") -> DraftTurn:
        """One turn of the two-phase conversational draft (#57). Emits metadata-only telemetry
        (LAW 8) when a proposal is actually produced. Retrieval/trim is inherited from the
        ProposalAgent's QueryService core (LAW 2)."""
        turn = self.draft_session.turn(user_oid, conv_id, message, intent)
        if turn.draft is not None:
            retrieved = sum(len(s["retrieved_docs"]) for s in turn.draft["sections"])
            self.agent.emit(
                "proposal.drafted",
                counts={"proposals_drafted": 1, "sections": len(turn.draft["sections"]),
                        "subquestions": len(turn.draft["plan"]), "authorized_docs": retrieved},
                ts=self._now(),
            )
        return turn

    def resync_source(self, source_id: str):
        """Re-crawl one registered source. SUBMITS and returns a job handle (#569, ADR 0016) -
        it used to run the whole crawl inside the caller's request.

        Raises KeyError for an unknown source (-> 404 at the route). The crawl resumes from
        the descriptor's cursor, and `resume=True` (the runner's default) picks up a
        non-terminal job for this source rather than starting a fresh one - which is what
        turns a killed process into a continuation instead of a restart (#327/#455).
        """
        desc = self.source_registry.get(source_id)          # KeyError -> 404
        job = self._submit_crawl(source_id, desc.connector, cursor=desc.cursor,
                                 job_tenant=desc.job_tenant or self.tenant_id,
                                 event="source.synced")
        return {"source_id": source_id, "job_id": job.job_id, "job_status": job.status}

    def resync_source_blocking(self, source_id: str) -> SourceSummary:
        """Submit, then wait, then return the summary. For rigs, the CLI and tests - NOT for a
        request handler. Same worker and same job record as `resync_source`, so this is the
        async path with a wait on the end rather than a second code path that could drift."""
        handle = self.resync_source(source_id)
        final = self.await_ingest(handle["job_id"], timeout=None)
        if final is not None and final.status == "failed":
            raise RuntimeError(f"ingest of {source_id!r} failed: {final.error}")
        return self.source_registry.get(source_id).summary()

    def create_api_key(self, user: str, label: str) -> tuple[ApiKeyRecord, str]:
        rec, token = self.api_key_registry.create(user, label)
        self.agent.emit("api_key.created", counts={"keys_created": 1}, ts=self._now())
        return rec, token

    def list_api_keys(self, user: str) -> list[ApiKeyRecord]:
        return self.api_key_registry.list_for(user)

    def revoke_api_key(self, key_id: str, user: str) -> None:
        self.api_key_registry.revoke(key_id, user)   # KeyError -> 404 at the route
        self.agent.emit("api_key.revoked", counts={"keys_revoked": 1}, ts=self._now())


def _sharepoint_from_config(job_tenant: str, cfg: dict):
    """Rebuild a GraphSharePointConnector from a persisted recipe (#883).

    The env half (client id/secret, the file filter) is read here rather than persisted: a
    credential does not belong in a table, and it is the same env the connect path itself
    reads. Returns None when the deployment cannot build one, which is not an error - a box
    without SP_CONNECTOR_* could not resync that source anyway."""
    from dbsearch.connectors.sharepoint_graph import GraphSharePointConnector

    cid = os.environ.get("SP_CONNECTOR_CLIENT_ID", "")
    secret = os.environ.get("SP_CONNECTOR_CLIENT_SECRET", "")
    if not (cid and secret):
        return None
    return GraphSharePointConnector(
        job_tenant, cfg["drive_id"], cfg["az_tenant_id"], cid, secret,
        cfg.get("folder_path") or "", os.environ.get("SP_INGEST_FILE_CONTAINS", ""),
        owner_oid=cfg.get("owner_oid"))


def _rehydrate_sources(registry: SourceRegistry, store, scope: str,
                       connector_factory=_sharepoint_from_config) -> None:
    """Re-register sources that only a previous process knew about (#883).

    The boot seeds cover "sharepoint" and "folder"; this covers `sharepoint:<tid>`, the source
    a customer actually connected, which lived ONLY in the dead process's memory. Without it
    the durable row exists and nothing reads it: /admin/sources still shows the seeded default
    with its zero, and /admin/resync still 404s on the id the canvas is asking about.

    Skipping is always preferred to raising. This runs at import time inside build_edition, so
    an exception here does not degrade a connector - it takes the whole deployment down."""
    if store is None:
        return
    log = logging.getLogger("dbsearch")
    for row in store.list(scope):
        source_id = row.get("source_id") or ""
        if not source_id or source_id in registry.source_ids():
            continue                      # a seed already registered it; the merge restored it
        cfg = row.get("config") or {}
        kind = row.get("kind") or ""
        if kind != "sharepoint" or not (cfg.get("drive_id") and cfg.get("az_tenant_id")):
            log.warning("source %s not rehydrated: no rebuild recipe for kind %r",
                        source_id, kind)
            continue
        try:
            connector = connector_factory(row.get("job_tenant") or scope, cfg)
        except Exception as exc:          # a bad row must not cost the deployment its boot
            log.warning("source %s not rehydrated: %s", source_id, type(exc).__name__)
            continue
        if connector is None:
            log.warning("source %s not rehydrated: SP_CONNECTOR_* not configured", source_id)
            continue
        registry.register(SourceDescriptor(
            source_id=source_id, kind=kind,
            display_name=row.get("display_name") or source_id,
            connector=connector, config=cfg, job_tenant=row.get("job_tenant") or ""))
        log.info("rehydrated source %s (%s docs)", source_id, row.get("doc_count") or 0)


def build_edition() -> Edition:
    from dbsearch.adapters.local import InMemoryIdentity

    backend = os.environ.get("SELFHOST_BACKEND", "memory")
    tenant_id = os.environ.get("DBSEARCH_TENANT_ID", "selfhost")
    user_map = _load_users()
    identity = InMemoryIdentity(user_map)
    azure_connector = None                    # set by the azure backend → registered as the source
    switcher_users = None                     # azure overrides with real Entra test OIDs

    if backend == "memory":
        from dbsearch.adapters.local import ExtractiveLlm, HashingEmbedding, InMemoryIndex, InMemoryObjectStore
        from dbsearch.router.structured import MIN_SCHEMA_HASHING_DIM

        store = InMemoryObjectStore()
        # #450: this embedder is threaded straight into RouterQueryService as the ROUTER's
        # cross-store profile embedder (router_api.py passes `edition.embedder` on; profiles.py's
        # score_stores ranks every store's profile vector with it), not just the document rail.
        # A 128-dim default hashes every store's profile text - including #296's SQL schema
        # terms - into far too few buckets once a catalog holds several SQL stores: at a 9-store
        # catalog a question sharing ZERO tokens with a store scored 0.36 cosine against it (pure
        # hash collision), which crowded the store that actually matched (real score 0.0754) out
        # of the fan-out entirely (4 live experiments, golden-suite task 10/10b). structured.py's
        # schema_index_embedder already fixed this exact class of problem one layer down, for the
        # PER-STORE SQL schema index (#225, MIN_SCHEMA_HASHING_DIM=4096); this mirrors that fix at
        # the router layer, reusing the same constant rather than inventing a second one.
        embedder = HashingEmbedding(dim=MIN_SCHEMA_HASHING_DIM)
        index = InMemoryIndex(store)
        llm = ExtractiveLlm()
        chat_models = {"Extractive (fast, local)": llm}      # #43: one model in pure-memory mode
        chat_model_default = "Extractive (fast, local)"
    elif backend == "memory-ollama":
        # In-memory retrieval (no Docker/Postgres), but GENERATION via a local Ollama model
        # over its OpenAI-compatible /v1 API. Build BOTH demo models so the UI can switch (#43).
        from dbsearch.adapters.llama import LlamaLlm
        from dbsearch.adapters.local import HashingEmbedding, InMemoryIndex, InMemoryObjectStore
        from dbsearch.router.structured import MIN_SCHEMA_HASHING_DIM

        store = InMemoryObjectStore()
        # #450: same router-level profile-embedder collision as the `memory` backend above -
        # see that branch's comment for the full explanation and the #225 precedent this mirrors.
        embedder = HashingEmbedding(dim=MIN_SCHEMA_HASHING_DIM)
        index = InMemoryIndex(store)
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        chat_models = {m: LlamaLlm(base_url, m) for m in ("llama3.2:3b", "llama3.1:8b")}
        chat_model_default = os.environ.get("OLLAMA_CHAT_MODEL", "llama3.2:3b")
        if chat_model_default not in chat_models:
            chat_models[chat_model_default] = LlamaLlm(base_url, chat_model_default)
        llm = chat_models[chat_model_default]
    elif backend == "memory-ollama-embed":
        # In-memory index but REAL semantic embeddings (nomic-embed-text) + Ollama generation.
        # Fixes lexical-only retrieval (a query semantically matches content even with no shared
        # words) WITHOUT needing Postgres/pgvector. InMemoryIndex is dimension-agnostic.
        from dbsearch.adapters.llama import LlamaEmbedding, LlamaLlm
        from dbsearch.adapters.local import InMemoryIndex, InMemoryObjectStore

        store = InMemoryObjectStore()
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        embedder = LlamaEmbedding(base_url, os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text"))
        index = InMemoryIndex(store)
        chat_models = {m: LlamaLlm(base_url, m) for m in ("llama3.2:3b", "llama3.1:8b")}
        chat_model_default = os.environ.get("OLLAMA_CHAT_MODEL", "llama3.2:3b")
        if chat_model_default not in chat_models:
            chat_models[chat_model_default] = LlamaLlm(base_url, chat_model_default)
        llm = chat_models[chat_model_default]
    elif backend == "pgvector-ollama":
        from dbsearch.adapters.llama import LlamaEmbedding, LlamaLlm
        from dbsearch.adapters.local import FilesystemObjectStore
        from dbsearch.adapters.vectordb import PgVectorIndex

        store = FilesystemObjectStore(os.environ.get("OBJECT_STORE_DIR", "/data/objects"))
        base_url = os.environ["OLLAMA_BASE_URL"]            # e.g. http://ollama:11434/v1
        embedder = LlamaEmbedding(base_url, os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text"))
        chat_model_default = os.environ.get("OLLAMA_CHAT_MODEL", "llama3.2")
        llm = LlamaLlm(base_url, chat_model_default)
        chat_models = {chat_model_default: llm}
        dim = int(os.environ.get("DBSEARCH_EMBEDDING_DIM", "768"))  # nomic-embed-text = 768
        index = PgVectorIndex(os.environ["PGVECTOR_DSN"], store, dim=dim)
        index.ensure_schema()
    elif backend == "azure":
        # Real Azure data plane behind the SAME ports (LAW 7): AI Search + Blob + Azure OpenAI
        # + EntraIdentity + Graph SharePoint. We reuse the proven factory wiring rather than
        # re-listing SDK calls here, so cloud-specific code never leaks into the edition (LAW 7).
        # The connect-azure.sh installer populates the env this reads. Generation is Azure OpenAI
        # (in-tenant) → LAW 1 holds for inference; the retrieved-content path runs wherever this
        # server runs — fine for a self-run trial, productionise by hosting it in-tenant (#58).
        import dataclasses

        from dbsearch.config import Settings
        from dbsearch.factory import build_data_plane

        settings = dataclasses.replace(Settings.from_env(), backend="azure")
        dp = build_data_plane(settings)
        store, embedder, index, llm = dp.store, dp.embedder, dp.index, dp.llm
        identity = dp.identity                 # EntraIdentity — real, default-deny group expansion (LAW 2)
        azure_connector = dp.connector         # GraphSharePointConnector (registered below)
        index.ensure_index()                   # idempotent; the installer also ingests before launch
        chat_models = {"Azure OpenAI (in-tenant)": llm}
        chat_model_default = "Azure OpenAI (in-tenant)"
        # Dev-switcher impersonation over REAL Entra OIDs so the tester can flip between an
        # allowed and a denied user to show the permission contrast (same pattern as alice/bob;
        # token-derived identity for prod is tracked in #9). Comma-separated in AZURE_TEST_USER_OIDS.
        switcher_users = [u.strip() for u in os.environ.get("AZURE_TEST_USER_OIDS", "").split(",") if u.strip()]
    else:
        raise ValueError(f"unknown SELFHOST_BACKEND: {backend!r}")

    # ADR 0017 (#538): sharing wraps the identity port rather than living inside either
    # adapter, so a grant applies to EVERY consumer of principal expansion - retrieval, the
    # corpus count, document segments, the trimmed listing - through the one seam they all
    # already share. Neither InMemoryIdentity nor EntraIdentity learns that sharing exists,
    # and LAW 2 keeps a single enforcement point: the principal-overlap test, unchanged.
    # #575: durable when PGVECTOR_DSN is set (same store the deployment already runs for
    # vectors/manifests/accounts - no new infrastructure), memory-only otherwise. Either way
    # `_by_id` stays the only thing per-request expansion reads (grant_store.py, grants.py).
    _grant_dsn = os.environ.get("PGVECTOR_DSN", "")
    grant_registry = GrantRegistry(
        store=PgGrantStore(_grant_dsn) if _grant_dsn else None)
    identity = GrantAwareIdentity(identity, grant_registry)
    # #600: the record that lets a recipient OPEN a shared conversation in the first place -
    # a pure sibling of grant_registry above, same DSN (no new infrastructure), same
    # memory-only-when-unset contract.
    conversation_shares = ConversationShareRegistry(
        store=PgConversationShareStore(_grant_dsn) if _grant_dsn else None)

    # #303: a real SEMANTIC embedder earns the #54 vector-rescue floor, so a chunk that matches
    # the question's MEANING but shares no keywords ("what is X about" vs the book's summary)
    # survives the lexical relevance floor instead of being dropped. The router already does this
    # (_default_wiring), but the edition's OWN QueryService — which answers the /search document
    # bridge — did not, so semantic retrieval was silently defeated on exactly that path. The
    # noisy lexical HashingEmbedding keeps it OFF (0.0), matching the router.
    _fvr = 0.9 if type(embedder).__name__ in ("LlamaEmbedding", "AzureOpenAIEmbedding") else 0.0
    qs = QueryService(index, identity, embedder, llm, store, floor_vector_rescue=_fvr,
                      tenant_id=tenant_id)
    # #596: conversations survive a deploy when the deployment already runs Postgres -
    # same store, no new infrastructure, memory-only otherwise (demo/self-host).
    _conv_dsn = os.environ.get("PGVECTOR_DSN", "")
    conversation = ConversationService(
        qs, llm, store=PgConversationStore(_conv_dsn) if _conv_dsn else None)
    proposal_agent = ProposalAgent(qs, llm)

    # #57 two-phase conversational draft: cheap Haiku for chat, strong Sonnet for the proposal.
    # Opt-in via ANTHROPIC_API_KEY (any backend). Falls back to the backend's default llm for
    # BOTH roles when no key is set, so the feature always works (just without the model split).
    chat_llm = draft_llm = llm
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        from dbsearch.adapters.anthropic import AnthropicLlm, HAIKU, SONNET

        base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        haiku_model = os.environ.get("ANTHROPIC_CHAT_MODEL", HAIKU)
        sonnet_model = os.environ.get("ANTHROPIC_DRAFT_MODEL", SONNET)
        chat_llm = AnthropicLlm(anthropic_key, haiku_model, base_url)
        draft_llm = AnthropicLlm(anthropic_key, sonnet_model, base_url)
        chat_models = {f"Claude Haiku ({haiku_model})": chat_llm,
                       f"Claude Sonnet ({sonnet_model})": draft_llm, **chat_models}
        chat_model_default = f"Claude Haiku ({haiku_model})"
        if "anthropic.com" in base_url:
            # LAW 1: public API => customer content leaves the tenant. DEV/DEMO only (#58).
            logging.getLogger("dbsearch").warning(
                "LAW 1 NOTICE: Anthropic public API (%s) — retrieved content leaves the tenant. "
                "DEV/DEMO only; productionise via Azure-hosted Claude (#58).", base_url)

    # #62 Groq (OpenAI-compatible, open models, very fast). Opt-in via GROQ_API_KEY. Adds
    # selectable chat models; if no Anthropic key, Groq's versatile model becomes the default.
    groq_key = os.environ.get("GROQ_API_KEY")
    # #342: ONE budget for the whole process, shared by every Groq client below. Groq has no
    # per-key hard spend cap, so this is the only cap that exists; scoping it per-client would
    # let the box's total spend grow with the number of selectable models.
    from dbsearch.server.spend_budget import budget_from_env, meter_llm

    groq_budget = budget_from_env()
    if groq_key:
        from dbsearch.adapters.groq import GROQ_DEFAULT_MODELS, GroqLlm

        _default = ",".join(GROQ_DEFAULT_MODELS)
        groq_models = [m for m in os.environ.get("GROQ_MODELS", _default).split(",") if m.strip()]
        for m in groq_models:
            chat_models[f"Groq {m.strip()}"] = meter_llm(GroqLlm(groq_key, m.strip()), groq_budget)
        # Kimi K2 is NO LONGER served by Groq (the moonshotai/kimi-k2-instruct-0905 id is retired
        # from the Groq catalog — 404s on every Groq key). To use Kimi, point this at a provider
        # that still serves it (Moonshot's own OpenAI-compatible API, or OpenRouter) via its key +
        # base_url. Register ONLY when a key is supplied, so it never shows as a broken option.
        #   e.g. GROQ_KIMI_API_KEY=<moonshot key>  GROQ_KIMI_BASE_URL=https://api.moonshot.ai/v1
        #        GROQ_KIMI_MODEL=kimi-k2-0905   (label shown as "Kimi <model>")
        kimi_key = os.environ.get("GROQ_KIMI_API_KEY")
        kimi_model = os.environ.get("GROQ_KIMI_MODEL", "moonshotai/kimi-k2")
        kimi_base = os.environ.get("GROQ_KIMI_BASE_URL", "https://api.groq.com/openai/v1")
        if kimi_key:
            chat_models[f"Kimi {kimi_model}"] = GroqLlm(kimi_key, kimi_model, kimi_base)
        if not anthropic_key and groq_models:           # no Claude -> Groq leads + powers the draft
            chat_model_default = f"Groq {groq_models[0].strip()}"
            chat_llm = draft_llm = chat_models[chat_model_default]
        logging.getLogger("dbsearch").warning(
            "LAW 1 NOTICE: Groq API — retrieved content leaves the tenant. DEV/DEMO only (#58).")
    draft_session = DraftSessionService(qs, chat_llm, draft_llm)

    # #280 (ADR 0009 Slice 3c): the DEMO scope's default generation model, resolved SEPARATELY
    # from the live `chat_model_default`. The demo endpoint is anonymous-reachable by design, so
    # it must never drive a paid/tenant-crossing model by default (the security review's
    # hosted-demo concern): Groq's fast open llama-3.3-70b when a GROQ key is present, else the
    # deterministic in-tenant Extractive model - never a hard failure, and never the live
    # Anthropic default. A public host SHOULD point the demo at a SEPARATE, spend-capped key
    # via GROQ_DEMO_API_KEY (falls back to the live GROQ_API_KEY) so demo traffic can be capped
    # and rate-limited independently of the live product; the per-visitor rate limit is the
    # remaining deploy-time piece. Either way the demo default is never the paid live default.
    demo_groq_key = os.environ.get("GROQ_DEMO_API_KEY") or groq_key
    if demo_groq_key:
        from dbsearch.adapters.groq import GROQ_VERSATILE, GroqLlm
        from dbsearch.adapters.local import ExtractiveLlm
        from dbsearch.server.spend_budget import BudgetedLlm

        # #342: the demo is the anonymous path, so it gets the graceful half of the budget as
        # well as the hard half. Metering alone would turn a spent budget into a 500 for every
        # visitor; BudgetedLlm serves the free in-tenant Extractive model instead, so running
        # out degrades the answer rather than the site. Same fallback the no-key branch uses.
        demo_chat_model = f"Groq {GROQ_VERSATILE}"
        demo_chat_llm = BudgetedLlm(
            meter_llm(GroqLlm(demo_groq_key, GROQ_VERSATILE), groq_budget),
            ExtractiveLlm(), groq_budget)
    else:
        from dbsearch.adapters.local import ExtractiveLlm

        demo_chat_model = "Extractive (fast, local)"
        demo_chat_llm = ExtractiveLlm()

    if backend == "azure":
        embedding_model = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
    elif backend in ("memory", "memory-ollama"):
        embedding_model = "hashing-embedding (dev)"
    else:
        embedding_model = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    control_plane = ControlPlane()
    control_plane.register_tenant(tenant_id, release="phase2", config={})
    agent = DataPlaneAgent(tenant_id, control_plane)

    # #883: the registry's sync-state is durable from here on. DBSEARCH_MANIFEST_DSN or
    # PGVECTOR_DSN, matching app.py's manifest/job wiring - prod sets PGVECTOR_DSN, so reading
    # only DBSEARCH_MANIFEST_DSN (as the ingest job store two screens below still does, #906)
    # would leave this in memory on the one box the defect was measured on.
    _sync_dsn = os.environ.get("DBSEARCH_MANIFEST_DSN") or os.environ.get("PGVECTOR_DSN", "")
    _sync_store = PgSourceSyncStore(_sync_dsn) if _sync_dsn else None
    source_registry = SourceRegistry(store=_sync_store, scope=tenant_id)
    if azure_connector is not None:
        source_registry.register(SourceDescriptor(
            source_id="sharepoint", kind="sharepoint",
            display_name="SharePoint — live (Microsoft Graph)",
            connector=azure_connector,
        ))
    else:
        source_registry.register(SourceDescriptor(
            source_id="sharepoint", kind="sharepoint",
            display_name="SharePoint — Contoso",
            connector=SharePointConnector(tenant_id=tenant_id),
        ))
    # Second connector: ACL-aware PULL ingestion of a local folder (on-prem/air-gapped).
    # Opt-in via FOLDER_SOURCE_DIR so default editions are unchanged; resync_source crawls it.
    folder_dir = os.environ.get("FOLDER_SOURCE_DIR")
    if folder_dir:
        from dbsearch.connectors.folder import FolderConnector

        source_registry.register(SourceDescriptor(
            source_id="folder", kind="folder",
            display_name=f"Local folder — {folder_dir}",
            connector=FolderConnector(tenant_id, folder_dir),
        ))

    # #883: the seeds above are the sources this process can build from its own env. Anything a
    # CUSTOMER connected exists only in the durable table, and comes back here.
    _rehydrate_sources(source_registry, _sync_store, tenant_id)

    api_key_registry = ApiKeyRegistry()
    set_api_key_resolver(api_key_registry.resolve)

    admin_service = AdminService(
        index, identity, control_plane, tenant_id, backend, embedding_model,
        source_registry=source_registry,
    )

    # LAW 1 (demo hardening): a demo backend may carry ANTHROPIC/GROQ keys in its env for
    # other purposes; DBSEARCH_FORCE_EXTRACTIVE pins the DEFAULT answer engine to the
    # deterministic in-tenant Extractive model regardless of those keys, so a caller that
    # omits an explicit model (e.g. the public marketing demo) can never egress content.
    if os.environ.get("DBSEARCH_FORCE_EXTRACTIVE") == "1" and "Extractive (fast, local)" in chat_models:
        chat_model_default = "Extractive (fast, local)"

    # #569: the SAME durable job store the router rail uses (#565), read from the same env, so
    # both rails write one table and a restart can resume either. Unconfigured -> in-memory,
    # which is the existing self-host contract: the crawl still runs off the request, it just
    # does not survive a process restart.
    from dbsearch.pipeline.jobs import InMemoryJobStore, PgJobStore
    from dbsearch.pipeline.worker import IngestJobRunner

    _jobs_dsn = os.environ.get("DBSEARCH_MANIFEST_DSN", "")
    ingest_jobs = PgJobStore(_jobs_dsn) if _jobs_dsn else InMemoryJobStore()

    edition = Edition(
        query_service=qs, conversation_service=conversation, proposal_agent=proposal_agent,
        draft_session=draft_session,
        index=index, store=store, embedder=embedder, identity=identity,
        tenant_id=tenant_id, backend=backend,
        users=switcher_users if switcher_users is not None else sorted(user_map.keys()),
        control_plane=control_plane, agent=agent, admin_service=admin_service,
        embedding_model=embedding_model, source_registry=source_registry,
        api_key_registry=api_key_registry, grant_registry=grant_registry,
        conversation_shares=conversation_shares,
        chat_models=chat_models, chat_model_default=chat_model_default,
        # #623: durable on the same Postgres the deployment already runs (no new
        # infrastructure), memory-only when unset - the identical contract grants (#575),
        # conversations (#596) and shares (#600) are wired with, four lines apart from each
        # other above. This one was the exception, and being the exception is what emptied
        # the owner's question log on every prod restart.
        audit_log=PgAuditLog(_grant_dsn) if _grant_dsn else InMemoryAuditLog(),
        demo_chat_llm=demo_chat_llm, demo_chat_model=demo_chat_model,
        ingest_jobs=ingest_jobs, ingest_runner=IngestJobRunner(ingest_jobs),
    )
    # Opt-in demo seed (#51): a baseline corpus so the Ask example prompts work and the
    # alice/bob LAW-2 contrast is live out of the box — and survives in-memory restarts.
    # Off by default so tests start from an empty index.
    if demo_seed_enabled():
        _seed_demo(edition)
    return edition


def demo_seed_enabled() -> bool:
    """Whether _seed_demo() ran, i.e. whether the two demo documents are actually indexed."""
    return os.environ.get("DBSEARCH_DEMO_SEED") == "1"


# The Ask surface's example prompts. They live HERE, beside the only corpus that can answer
# them, because #392 was exactly the cost of them living anywhere else: they were hardcoded
# in ask.js, so they survived the seed being off and invited every visitor on prod - where
# the index holds zero rows - to click a question guaranteed to return nothing. The operator
# hit that himself and reasonably read it as a broken product. Anything that offers these
# must first ask demo_seed_enabled(); they are true of the demo corpus and of nothing else.
DEMO_EXAMPLE_PROMPTS = (
    "What is our holiday and expenses policy?",     # -> demo-handbook, ACL ["all-staff"]
    "Summarize the Project Falcon valuation",       # -> demo-falcon,  ACL ["deal-team"]
)


def _seed_demo(edition: "Edition") -> None:
    # Lean 2-doc demo corpus — the cleanest permission contrast: ONE all-staff policy doc and ONE
    # deal-team-only confidential doc. Ask "holiday/expenses policy" -> both see the Handbook;
    # Ask "Project Falcon valuation" -> alice (deal-team) gets the $4.2B, bob gets NOTHING (no other
    # doc to fall back on, so he cleanly abstains). Extra "richness" docs were removed on purpose —
    # they overlapped M&A/valuation vocab and muddied the contrast.
    docs = [
        ("demo-handbook", "Staff Handbook", ["all-staff"],
         "Our holiday and expenses policy: all staff receive 25 days annual leave plus public "
         "holidays. Expenses are reimbursed monthly against receipts submitted in the portal. "
         "Remote work is permitted up to three days per week."),
        ("demo-falcon", "Project Falcon — Valuation (Confidential)", ["deal-team"],
         "Project Falcon valuation: confidential acquisition target valued at 4.2 billion dollars. "
         "Deal team only. Estimated synergies of 300 million annually; expected close in Q3."),
    ]
    for ext, title, acl, text in docs:
        edition.ingest_document(ext, title, text, acl, uri=f"seed://{ext}")
