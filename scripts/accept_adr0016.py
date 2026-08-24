#!/usr/bin/env python3
"""#564 - the ADR 0016 acceptance run on doc_pack_10x (40.19 MB / 4884 documents).

This is the case #536 measured as impossible: `/router/compose` exceeded its 3600s timeout
because the crawl ran inside the request. ADR 0016's Verification says done means it
COMPLETES, resumes rather than restarts after a mid-crawl failure, and reports truthful
progress throughout - measured, not asserted.

Driven through the real async path (start_sync -> IngestJobRunner -> run_ingestion with a
JobCheckpoint) against the real local embedder, so the rate is a real rate.

    ./scripts/pad_doc_pack.py --factor 10 --out /tmp/doc_pack_10x   # build the pack first
    python3 scripts/accept_adr0016.py --docs 200                    # rate probe + projection
    python3 scripts/accept_adr0016.py --docs 4884 --crash-at 20000  # the acceptance (~70 min)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import dbsearch.router.providers.connector as C  # noqa: E402
from dbsearch.adapters.llama import LlamaEmbedding  # noqa: E402
from dbsearch.adapters.local import InMemoryIdentity  # noqa: E402

PACK = Path(os.environ.get("DOC_PACK", "/tmp/doc_pack_10x")) / "docs"
OLLAMA = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
SID = "acceptance"


class CountingEmbedder:
    """The real embedder, instrumented, able to die once at a chosen chunk count so the
    resume is measured rather than assumed."""

    def __init__(self) -> None:
        self._inner = LlamaEmbedding(OLLAMA, EMBED_MODEL)
        self.chunks = 0
        self.calls = 0
        self.crash_at: int | None = None
        self._lock = threading.Lock()

    def embed(self, texts):
        with self._lock:
            if self.crash_at is not None and self.chunks >= self.crash_at:
                raise RuntimeError("simulated ingest failure mid-crawl")
        vecs = self._inner.embed(texts)
        with self._lock:
            self.chunks += len(texts)
            self.calls += 1
        return vecs


def stage(n_docs: int) -> Path:
    """A staging folder of n_docs laid out as <acl-group>/<file> for FolderConnector."""
    root = Path(tempfile.mkdtemp(prefix="accept564-"))
    group = root / "all-staff"
    group.mkdir(parents=True)
    for p in sorted(q for q in PACK.rglob("*") if q.is_file())[:n_docs]:
        shutil.copy2(p, group / p.name)
    return root


def build_without_crawling(root: Path):
    """Build the store but suppress build()'s automatic submit, so the pipe's embedder can be
    swapped for the instrumented one before any work happens."""
    prov = C.ConnectorStoreProvider("folder", C.folder_connector_factory,
                                    identity=InMemoryIdentity({"alice": ["all-staff"]}))
    real = C.ConnectorStoreProvider.start_sync
    C.ConnectorStoreProvider.start_sync = lambda self, sid, *, force_new=False: None
    try:
        store = prov.build({"id": SID, "business_unit": "bu", "title": "Acceptance pack",
                            "description": "policies contracts finance",
                            "path": str(root), "acl": ["all-staff"]})
    finally:
        C.ConnectorStoreProvider.start_sync = real
    embedder = CountingEmbedder()
    obj, _default, index = prov._pipes[SID]
    prov._pipes[SID] = (obj, embedder, index)
    return prov, store, index, embedder


def crawl(prov, embedder, *, force_new: bool, label: str) -> dict:
    """One crawl through the real async path, watching the job the way a UI would."""
    before_chunks = embedder.chunks
    t0 = time.perf_counter()
    job = prov.start_sync(SID, force_new=force_new)
    ticks, last, stalls = [], None, 0
    while True:
        j = prov.jobs.get(job.job_id)
        key = (j.phase, j.docs_done)
        if key != last:
            ticks.append((round(time.perf_counter() - t0, 1), j.phase, j.docs_done,
                          j.docs_total, j.docs_skipped))
            last = key
        if j.status in ("succeeded", "failed"):
            break
        time.sleep(0.5)
    prov.wait_for_ingest(SID, timeout=30)
    final = prov.jobs.get(job.job_id)
    elapsed = time.perf_counter() - t0
    return {"label": label, "job_id": final.job_id, "status": final.status,
            "phase": final.phase, "error": final.error,
            "docs_done": final.docs_done, "docs_total": final.docs_total,
            "docs_skipped": final.docs_skipped,
            "elapsed_s": round(elapsed, 1),
            "chunks_embedded_this_run": embedder.chunks - before_chunks,
            "progress_ticks": len(ticks),
            "first_ticks": ticks[:3], "last_ticks": ticks[-3:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=200)
    ap.add_argument("--crash-at", type=int, default=0, help="chunks after which run 1 dies")
    args = ap.parse_args()

    total = sum(1 for p in PACK.rglob("*") if p.is_file())
    root = stage(args.docs)
    mb = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) / 1e6
    full_mb = sum(p.stat().st_size for p in PACK.rglob("*") if p.is_file()) / 1e6
    print(f"pack {total} docs / {full_mb:.2f} MB · this run {args.docs} docs / {mb:.2f} MB")
    print(f"embedder {EMBED_MODEL} at {OLLAMA}\n", flush=True)

    prov, store, index, embedder = build_without_crawling(root)
    results = []

    if args.crash_at:
        embedder.crash_at = args.crash_at
        r1 = crawl(prov, embedder, force_new=True, label="run-1 (dies mid-crawl)")
        results.append(r1)
        print(json.dumps(r1, indent=2), flush=True)
        indexed_after_crash = {c.doc_external_id for _k, (c, _v) in index._snapshot()}
        print(f"  indexed and queryable after the crash: {len(indexed_after_crash)} documents",
              flush=True)
        embedder.crash_at = None
        r2 = crawl(prov, embedder, force_new=False, label="run-2 (resume)")
        results.append(r2)
        print(json.dumps(r2, indent=2), flush=True)
    else:
        results.append(crawl(prov, embedder, force_new=True, label="single run"))
        print(json.dumps(results[-1], indent=2), flush=True)

    indexed = {c.doc_external_id for _k, (c, _v) in index._snapshot()}
    items, _ = prov.sources.get(SID).connector.list_changes(None)
    expected = {i["external_id"] for i in items}
    wall = sum(r["elapsed_s"] for r in results)

    print("\n=== VERDICT ===")
    print(f"  documents in source      : {len(expected)}")
    print(f"  documents indexed        : {len(indexed)}")
    print(f"  documents MISSING        : {len(expected - indexed)}")
    print(f"  total chunks embedded    : {embedder.chunks}")
    print(f"  wall clock               : {wall:.0f}s ({wall/60:.1f} min) for {mb:.1f} MB")
    print(f"  rate                     : {wall/mb:.1f} s/MB")
    if args.docs < total:
        print(f"  projection for {full_mb:.0f} MB   : {(wall/mb)*full_mb/60:.0f} min")
    if len(results) == 2:
        redone = results[1]["chunks_embedded_this_run"]
        saved = results[0]["chunks_embedded_this_run"]
        print(f"  resume skipped           : {results[1]['docs_skipped']} documents")
        print(f"  chunks paid once         : {saved} of {saved + redone} "
              f"({saved/(saved+redone):.0%} not redone)")

    ok = (not (expected - indexed)) and results[-1]["status"] == "succeeded"
    shutil.rmtree(root, ignore_errors=True)
    prov._runner.shutdown()
    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
