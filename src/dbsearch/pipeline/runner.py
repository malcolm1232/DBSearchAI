"""Queue-driven ingestion runner (LAW 4, ADR 0005).

Stages are decoupled and joined by the queue; each consumes one topic and publishes
the next. Messages carry REFERENCES (object-store keys), never document bytes. In
production each stage is its own autoscaling worker; here we drain them in order
in-process. The stage code is identical either way.

    connector --[parse]--> parse/extract --[chunkembed]--> chunk+embed --[index]--> index
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from dbsearch.core.models import Chunk, Document, Segment
from dbsearch.core.validation import has_traversal_segment
from dbsearch.ports.base import (
    ConnectorPort,
    EmbeddingPort,
    ExtractorPort,
    IndexPort,
    ItemUnreadable,
    ObjectStorePort,
    ParseProducedNoText,
    QueuePort,
    UnsupportedMedia,
)
from dbsearch.core.headroom import FLOOR_ENV  # noqa: E402  #843
from dbsearch.core.headroom import shortfall as disk_shortfall


class NoDiskHeadroom(RuntimeError):
    """#843: a crawl stopped because the blob volume is at its free-space floor.

    Its own type so a caller can tell "the disk is full" from "this document was unreadable".
    An operator condition, never a claim about the customer's data or their plan - the same
    distinction #831 drew between 507 and 402 on the upload path."""


@dataclass
class IngestResult:
    """What one crawl reported: documents seen + the source's next change cursor.

    `skipped` (#455) is the part of `doc_count` a resumed run did NOT redo because a previous
    attempt had already indexed it. Reported separately rather than folded away: "synced 4884
    documents" and "synced 4884, of which 4700 were already done" are different facts, and
    the second is the one that tells an operator the resume worked."""
    doc_count: int
    cursor: str | None
    skipped: int = 0
    unreadable: int = 0   # #712: listed but not fetchable (per-file 403, export cap)
    # #910: what this crawl means for CORPUS SIZE. `doc_count` above is and stays the
    # PER-CRAWL processed counter ("synced N documents" in progress UIs); writing it over
    # the corpus size is the #910 defect. A full crawl's doc_count IS the corpus; an
    # incremental crawl contributes only its net change, reported here.
    full_crawl: bool = True
    created: int = 0      # net-new documents this crawl indexed (incremental only)
    deleted: int = 0      # documents this crawl removed via source tombstones


_DEFAULT_MAX_CHARS = int(os.environ.get("CHUNK_MAX_CHARS", "1200"))


def _chunk_text(text: str, max_chars: int = 0, overlap: int = 150) -> list[str]:
    max_chars = max_chars or _DEFAULT_MAX_CHARS
    """Sliding-window chunks (size + overlap), whitespace-normalized so a multi-page PDF
    becomes many small passages instead of one giant blob. Retrieval then hands the LLM a
    few short, relevant chunks — fast generation + precise citations (#49)."""
    # #367: NUL is never meaningful text — it comes from a mis-decoded file (a UTF-16 doc
    # read as 8-bit, binary bytes reaching a text parser). It must die HERE, at the one
    # normalization chokepoint, not at a storage adapter: PostgreSQL text cannot hold 0x00,
    # so a single NUL used to abort the whole crawl at the index stage.
    text = " ".join(text.replace("\x00", " ").split())
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    step = max_chars - overlap
    chunks: list[str] = []
    for start in range(0, len(text), step):
        chunk = text[start:start + max_chars].strip()
        if chunk:
            chunks.append(chunk)
        if start + max_chars >= len(text):
            break
    return chunks


def run_ingestion(
    connector: ConnectorPort,
    queue: QueuePort,
    store: ObjectStorePort,
    extractor: ExtractorPort,
    embedder: EmbeddingPort,
    index: IndexPort,
    cursor: str | None = None,
    strict: bool = False,
    progress: "Callable[[str, int, int], None] | None" = None,
    checkpoint: "JobCheckpoint | None" = None,
) -> IngestResult:
    """Run a full/incremental crawl end-to-end. Idempotent: re-running REPLACES a document's
    chunk set, never duplicates and never leaves orphans — every doc published this crawl is
    `index.delete()`d before the fresh chunks are indexed (see below). `cursor` resumes from
    the source's last change token (None => full crawl).

    `strict=True` (single interactive upload) propagates UnsupportedMedia/ParseProducedNoText
    instead of skipping the document — see _stage_parse.

    `progress(phase, done, total)` (#302) is an optional UI hook called as the crawl moves
    through discover -> fetch -> extract -> embed -> index. Best-effort and side-effect-free to
    the pipeline; defaults to a no-op so every existing caller is unchanged.

    `checkpoint` (#455, ADR 0016) makes the crawl RESUMABLE. Without it the behaviour is
    exactly as before; with it, each document is recorded the moment its chunks are indexed,
    and a resumed run skips what is already done. Note what it does NOT do: it never touches
    the cursor. The cursor advances per BATCH, not per item, so persisting `next_cursor`
    before the batch finishes would silently skip every unprocessed item in it — a store that
    answers as though those documents do not exist. `next_cursor` is returned only on a
    completed batch and the caller persists it exactly as it always did, which is why a
    partially-ingested source is *behind* and never *wrong*.

    ONE DOCUMENT AT A TIME, end to end (the #455 restructure). The stages used to be drained
    over the whole batch — fetch all, parse all, embed all, index all. That is why a crawl
    that died at 90% had indexed NOTHING: embedding is the entire cost and it sits before the
    index stage. Draining per document means a document is either fully indexed or not
    started, which is the only granularity a checkpoint can honestly record. The stage code
    is untouched and still joined by the queue (LAW 4 / ADR 0005) — each stage simply drains
    one document's messages instead of the batch's. Embedding batches are unaffected: they
    were always gathered per document (`_stage_chunk_embed` batches one doc's chunks)."""
    _p = progress or (lambda *a: None)
    if checkpoint is not None:
        checkpoint.started(cursor)
    try:
        return _run(connector, queue, store, extractor, embedder, index, cursor, strict,
                    _p, checkpoint)
    except BaseException as exc:
        # The job is marked failed HERE rather than left to the caller. A caller that forgets
        # leaves the record stuck in "running" forever, and "running" is what `resumable()`
        # looks for — a stuck row would make every later restart try to resume a run that is
        # not coming back. That is #327's failure shape, not a tidiness concern.
        if checkpoint is not None:
            checkpoint.failed(exc)
        raise


def _run(connector, queue, store, extractor, embedder, index, cursor, strict, _p,
         checkpoint) -> IngestResult:
    _p("discovering", 0, 0)
    items, next_cursor = connector.list_changes(cursor)
    total = len(items)
    # What a previous attempt at THIS job already indexed. Empty unless the caller explicitly
    # opted into a resume, so a job mid-flight in another worker is never mistaken for one to
    # be picked up (see JobCheckpoint).
    already: set[str] = checkpoint.already_done if checkpoint is not None else set()
    doc_count = 0
    skipped = 0
    unreadable = 0
    # #910: corpus-delta bookkeeping. `full_crawl` is a property of the crawl we were ASKED
    # to run (no cursor = list everything), so a full crawl's doc_count is the corpus size
    # and created/deleted stay 0. `chunk_count_for` is the optional index capability that
    # lets an incremental crawl tell "new document" from "updated document" - without it,
    # created stays 0 (an honest undercount a full crawl repairs) rather than counting
    # every update as growth.
    full_crawl = cursor is None
    created = 0
    deleted = 0
    _count_chunks = getattr(index, "chunk_count_for", None)
    _tenant = getattr(connector, "tenant_id", None)
    _p("fetching", 0, total)

    for n, item in enumerate(items, start=1):
        # #910: a deletion tombstone is handled entirely here - no fetch, no Document. The
        # chunks go immediately (LAW 2 freshness: a file deleted at the source must stop
        # serving), and the corpus count goes down only for documents this index actually
        # held, so an out-of-scope tombstone from a folder-scoped delta changes nothing.
        # Optional-capability read (the lexical_search idiom): connectors are duck-typed in
        # places, and a source without deletion reporting is simply one that never deletes.
        _deletions = getattr(connector, "deletions", None)
        gone = _deletions(item) if callable(_deletions) else []
        if gone:
            for ext in gone:
                if _tenant is None:
                    break
                held = _count_chunks(_tenant, ext) > 0 if callable(_count_chunks) else True
                index.delete(_tenant, ext)
                if held:
                    deleted += 1
            _p("deleting", n, total)
            continue

        ids = connector.external_ids(item)
        if ids and already.issuperset(ids):
            # Skipped BEFORE fetch_content: a completed document must cost neither network
            # nor embedding on resume, which is the whole point of the checkpoint.
            skipped += len(ids)
            doc_count += len(ids)
            _p("skipping", n, total)
            if checkpoint is not None:
                checkpoint.progress("skipping", n, total, skipped)
            continue

        _p("fetching", n, total)
        try:
            raw, mime = connector.fetch_content(item)
        except ItemUnreadable as exc:
            if strict:
                raise
            # LAW 3: one item failing never blocks the rest - and never silently. The
            # count is the difference between "synced 40 documents" and "synced 40 of 43,
            # 3 unreadable", and only the second tells an operator the store is partial.
            # A connector may only raise ItemUnreadable for a PERMANENT per-item refusal
            # (403/404 or equivalent) - a transient/systemic failure (429, 5xx) must fail
            # the whole crawl instead, since the cursor advances during listing and a
            # skipped item here is never re-listed (see ports/base.py's ItemUnreadable
            # docstring, and gdrive.py's `_raise_for_fetch_failure`).
            unreadable += 1
            logging.getLogger("dbsearch.ingest").warning(
                "skipping an unreadable item %r: %s", item.get("external_id", "?"), exc)
            if checkpoint is not None:
                checkpoint.progress("skipping", n, total, skipped)
            continue
        seen: set[tuple[str, str]] = set()
        for doc in connector.to_documents(item):
            # #576 review Finding A (CRITICAL), layer 2 backstop: /ingest validates its
            # caller-typed external_id up front (server/app.py), but this is where EVERY
            # OTHER path lands too - SharePoint, the folder connector, resync - and a
            # connector-sourced id is still untrusted input. `has_traversal_segment` is
            # the LOOSER check (not `is_safe_external_id`): a folder connector's id is
            # legitimately a multi-segment relative path ("all-staff/handbook.txt",
            # connectors/folder.py) and that is safe - only an actual `.`/`..` traversal
            # segment or an absolute path is refused. One document with a bad id must not
            # abort the whole crawl (LAW 3: one connector/item failing never blocks the
            # rest), so it is skipped and logged, exactly like an unextractable item below
            # (_stage_parse) already is.
            if has_traversal_segment(doc.external_id):
                logging.getLogger("dbsearch.ingest").error(
                    "skipping a document with an unsafe external_id: %r", doc.external_id)
                continue
            # #843: the headroom guard belongs HERE, not only on the upload endpoint. Every
            # ingest path in the product funnels through this loop - the interactive upload,
            # /ingest, and every connector crawl - and the crawl is the one that actually
            # dominates the volume (#833 measured 211MB of connector blobs against 199KB of
            # uploads). Checked per document, before the write, so a crawl over a large Drive
            # stops at the floor instead of taking the disk down for every account on the box.
            low = disk_shortfall(store, len(raw))
            if low is not None:
                free, floor = low
                raise NoDiskHeadroom(
                    f"stopped before writing {doc.external_id!r}: {free} bytes free and this "
                    f"deployment keeps {floor} bytes of headroom. Free space or raise "
                    f"{FLOOR_ENV}, then run this source again.")
            # #910: measured BEFORE this crawl writes the document, so an update to an
            # existing document is growth of nothing. Full crawls skip the query - their
            # doc_count is the corpus size and created is unused.
            if (not full_crawl and callable(_count_chunks)
                    and _count_chunks(doc.tenant_id, doc.external_id) == 0):
                created += 1
            doc.content_ref = store.put(f"raw/{doc.tenant_id}/{doc.external_id}", raw)
            doc.source_meta["mime"] = mime
            queue.publish("parse", {"document": doc.to_dict()})
            doc_count += 1
            seen.add((doc.tenant_id, doc.external_id))

        # The stages report their own 1-of-1 progress, which is useless to a UI; re-frame it
        # onto the document counter so the caller sees "embedding, 143 of 4884".
        def _stage_p(phase: str, _done: int, _total: int, _n: int = n) -> None:
            _p(phase, _n, total)
            if checkpoint is not None:
                checkpoint.progress(phase, _n, total, skipped)

        _stage_parse(queue, store, extractor, strict, progress=_stage_p, total=1)
        _stage_chunk_embed(queue, store, embedder, progress=_stage_p, total=1)
        # Delete-before-index (LAW 2 freshness) happens INSIDE _stage_index, per document, at
        # the moment that document's replacement chunks are ready — see #391. It used to run
        # as one up-front sweep over the whole batch, which meant the corpus was empty from
        # that point until the last document finished embedding. Embedding is the slow stage
        # (CPU-only nomic-embed-text on the 2-vCPU prod box: 66 chunks/min), so a real
        # re-ingest blanked the index for hours and every query answered "Searched 0 documents
        # you can access" — indistinguishable from a broken product.
        written = _stage_index(queue, index)

        # The guarantee the up-front sweep existed for: a document that yielded NO chunks this
        # crawl (parse failure, or genuinely empty now) must still lose its stale chunks rather
        # than keep serving old content under an old ACL. Those never reach _stage_index, so
        # they are collected here. Under strict=True the caller also sees the raised
        # UnsupportedMedia/ParseProducedNoText, so a failed re-upload is reported, not silent.
        for tenant_id, external_id in seen - written:
            index.delete(tenant_id, external_id)

        # Recorded LAST, and only now. After the sweep above, every document in `seen` is in
        # its correct final state — indexed with fresh chunks, or correctly holding none. A
        # checkpoint written any earlier would let a resume skip a document that never made
        # it into the index, which is silent data loss wearing the mask of progress.
        if checkpoint is not None:
            for _, external_id in seen:
                checkpoint.record(external_id)
            checkpoint.progress("indexing", n, total, skipped)

    _p("done", doc_count, doc_count)
    result = IngestResult(doc_count=doc_count, cursor=next_cursor, skipped=skipped,
                          unreadable=unreadable, full_crawl=full_crawl,
                          created=created, deleted=deleted)
    if checkpoint is not None:
        # `complete` runs the caller's commit (persist the cursor onto the source, refresh
        # the routing profile) and only then marks the job succeeded - so a client that polls
        # the job and immediately reads the store can never see the two disagree.
        checkpoint.complete(result)
    return result


def _stage_parse(queue: QueuePort, store: ObjectStorePort, extractor: ExtractorPort,
                 strict: bool = False, progress=None, total: int = 0) -> None:
    log = logging.getLogger("dbsearch.ingest")
    _p = progress or (lambda *a: None)
    done = 0
    for msg in queue.consume("parse"):
        doc = Document.from_dict(msg["document"])
        done += 1
        _p("extracting", done, total)
        # LAW 3: one unextractable item (e.g. a scanned/image-only PDF with no text layer, or an
        # unsupported type) must NOT abort the whole crawl. Skip it and keep going; the doc is
        # simply not indexed (a later OCR-capable extractor can pick it up on re-sync).
        # `strict` is for the single-file interactive upload, where there is no "rest of the
        # crawl" to protect and a silent skip would 200 back an empty ingest: there the typed
        # parse failures propagate so the endpoint can map them to 415/422.
        try:
            raw = store.get(doc.content_ref)
            segments = extractor.extract_segments(raw, doc.source_meta.get("mime", "text/plain"))
        except (UnsupportedMedia, ParseProducedNoText):
            if strict:
                raise
            log.warning("skipping %s (%s): unextractable",
                        doc.external_id, doc.source_meta.get("mime", "?"))
            continue
        except Exception as e:
            log.warning("skipping %s (%s): %s", doc.external_id, doc.source_meta.get("mime", "?"), e)
            continue
        payload = json.dumps([s.to_dict() for s in segments]).encode()
        seg_ref = store.put(f"segments/{doc.tenant_id}/{doc.external_id}", payload)  # ref, not inline (LAW 4)
        queue.publish("chunkembed", {"document": doc.to_dict(), "segments_ref": seg_ref})


def _stage_chunk_embed(queue: QueuePort, store: ObjectStorePort, embedder: EmbeddingPort,
                       batch_size: int = 16, progress=None, total: int = 0) -> None:
    _p = progress or (lambda *a: None)
    done = 0
    for msg in queue.consume("chunkembed"):
        doc = Document.from_dict(msg["document"])
        done += 1
        _p("embedding", done, total)   # #302: the slow stage — report per doc so the UI moves
        segments = [Segment.from_dict(s) for s in json.loads(store.get(msg["segments_ref"]).decode())]
        # Gather all chunks first, then embed in BATCHES — one embedding API call per `batch_size`
        # chunks instead of one per chunk (a ~10-50x speedup on large docs; a 300-page book is
        # ~600 chunks = ~600 serial API calls otherwise). Behaviour is otherwise identical.
        pairs = [(ctext, seg.locator) for seg in segments for ctext in _chunk_text(seg.text)]
        n = 0
        for i in range(0, len(pairs), batch_size):
            batch = pairs[i:i + batch_size]
            vecs = embedder.embed([t for t, _ in batch])   # single API call for the batch
            for (ctext, locator), vec in zip(batch, vecs):
                # Real-world docs (esp. SharePoint) carry lone UTF-16 surrogates that strict
                # UTF-8 rejects; 'replace' keeps ingestion alive (bad char → '?') instead of
                # crashing the whole run — and the stored bytes stay strict-decodable downstream.
                text_ref = store.put(f"chunk/{doc.tenant_id}/{doc.external_id}/{n}", ctext.encode("utf-8", "replace"))
                chunk = Chunk(
                    tenant_id=doc.tenant_id,
                    doc_external_id=doc.external_id,
                    chunk_id=f"{doc.external_id}#{n}",
                    text_ref=text_ref,
                    allowed_principals=[p.oid for p in doc.acl],  # denormalized ACL (LAW 2)
                    # #834: the vector rides the message (the queue is in-process). An emb/
                    # blob was a hand-off buffer read once at upsert and never again - 20%
                    # of prod's store for a value used one stage later.
                    embedding=vec,
                    title=doc.title,
                    uri=doc.uri,
                    locator=locator,  # slide/page/row/section — DATA-PLANE only (LAW 1)
                    owner_oid=doc.owner_oid,  # ADR 0012: attribution flows through, never gates
                    doc_bytes=doc.doc_bytes,  # #775: usage rides the rows, not a second ledger
                )
                queue.publish("index", {"chunk": chunk.to_dict()})
                n += 1


def _stage_index(queue: QueuePort, index: IndexPort) -> set[tuple[str, str]]:
    """Write the fresh chunks, replacing each document's previous chunk set at the moment its
    own first chunk lands (#391) — so every OTHER document stays queryable throughout, and the
    replaced one is only briefly incomplete instead of the whole corpus being empty for the
    length of the crawl. The delete must fire before that document's first upsert: chunk ids
    are `{external_id}#{n}`, so upsert-only would orphan the higher-n chunks of a previous,
    larger version. First-ingest delete is a harmless no-op.

    Returns the (tenant_id, external_id) pairs that produced at least one chunk, so the caller
    can sweep the documents that produced none."""
    written: set[tuple[str, str]] = set()
    for msg in queue.consume("index"):
        chunk = Chunk.from_dict(msg["chunk"])
        key = (chunk.tenant_id, chunk.doc_external_id)
        if key not in written:
            index.delete(*key)
            written.add(key)
        index.upsert([chunk])
    return written
