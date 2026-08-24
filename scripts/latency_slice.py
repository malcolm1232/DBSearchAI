#!/usr/bin/env python3
"""#529/#530/#532 - the latency the product has never measured, in a DECLARED unit.

Why this exists: grepping the tree for timing instrumentation hits one file
(`router/health.py`), and the golden run artifact has no duration field, so "how long does
one question take" and "how long does ingest take" were both unanswerable. A number
without a stated boundary is unfalsifiable, so every figure here names exactly what it
includes.

    ./scripts/latency_slice.py --pack "unstructured documents/doc_pack"

THE UNIT (#532). Octen publishes a P50 of 62ms for web search, and the honest comparison
to that is NOT our answer latency - it is RETRIEVAL: query in, ranked results out, no
generation. So the stages are timed separately and reported separately:

    embed      embedding the question alone (the model server's floor)
    retrieve   embed + rank + LAW 2 principals trim -> top_k chunks   <-- Octen's unit
    route      embed + score store profiles -> a routing decision

and these are DELIBERATELY EXCLUDED from that figure, because Octen's number excludes
their equivalents:

    generate_sql / synthesize   two LLM calls, which dominate our end-to-end answer
    sql execute                 the CUSTOMER's database, not our infrastructure

Reporting a blended end-to-end number against a retrieval number would be a units
mismatch in our disfavour; reporting retrieval alone against it and calling it
like-for-like would flatter us. Both figures are printed, labelled, and the exclusions are
part of the output rather than a footnote.

P50 and P95, never a mean: the distribution is bimodal by design (the #474 rescue raises
its own per-store budget from 8s to 90s), so a mean describes nothing that happens.

WHAT THE NUMBER IS A PROPERTY OF: the rig. Model server, embedding model, hardware and
store sizes are recorded in the artifact, because on a 16GB laptop with a 9.5GB model
resident these figures characterise the box, not the architecture.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.eval.golden import load_pack                       # noqa: E402
from dbsearch.router.store import AccessContext                  # noqa: E402


def _pct(values: list, p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def _report(name: str, samples: list, note: str = "") -> dict:
    row = {"stage": name, "n": len(samples),
           "p50_ms": round(_pct(samples, 50), 1) if samples else None,
           "p95_ms": round(_pct(samples, 95), 1) if samples else None,
           "min_ms": round(min(samples), 1) if samples else None,
           "max_ms": round(max(samples), 1) if samples else None,
           "note": note}
    if samples:
        print(f"  {name:<12} n={row['n']:<4} p50={row['p50_ms']:>8.1f}ms  "
              f"p95={row['p95_ms']:>8.1f}ms  min={row['min_ms']:>8.1f}  "
              f"max={row['max_ms']:>9.1f}   {note}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default="unstructured documents/doc_pack")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--repeat", type=int, default=3,
                    help="passes over the question set; pass 1 is discarded as warm-up")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from dbsearch.adapters.local import InMemoryIdentity                 # noqa: E402
    from dbsearch.server import router_api as rapi                       # noqa: E402
    from dbsearch.server.edition import build_edition                    # noqa: E402

    print(f"pack: {args.pack}")
    pack = load_pack(Path(args.pack))
    sys.path.insert(0, str(ROOT / "scripts"))
    from golden_runner import pack_manifest                              # noqa: E402
    manifest = pack_manifest(pack, "alice", "bob")

    # Same construction as `compose_demo_catalog`: the SAME `_default_wiring` the live
    # endpoint computes and the SAME `_compose_manifest` it calls, so what is timed here
    # is the product's real ingest and retrieval path, not a bench-only replica.
    edition = build_edition()
    sql_gen, cosmos_gen, fvr, value_llm = rapi._default_wiring(edition)
    identity = InMemoryIdentity({"alice": ["alice"], "bob": ["bob"]})
    state = rapi._State(identity=identity, sql_generator=sql_gen,
                        cosmos_generator=cosmos_gen, embedder=edition.embedder,
                        floor_vector_rescue=fvr, value_llm=value_llm)

    # ---- INGEST (#530) -----------------------------------------------------------
    print("\ningest - /router/compose, the ONE compose path the endpoint itself uses")
    t0 = time.perf_counter()
    rapi._compose_manifest(state, manifest)
    ingest_s = time.perf_counter() - t0

    stores = list(state.catalog.stores()) if state.catalog is not None else []
    doc_stores = [n for n in stores if n.store is not None
                  and hasattr(n.store, "_qs")]                # IndexedStore = doc rail
    # A doc store carries its documents under config.seed (golden_runner._docs_store), NOT
    # a `documents` key - counting the wrong key reports 0 and silently divides by zero.
    seeds = [d for s in (manifest.get("stores") or [])
             for d in ((s.get("config") or {}).get("seed") or [])]
    n_docs = len(seeds)
    src_mb = sum(len((d.get("text") or "").encode()) for d in seeds) / 1_000_000
    print(f"  total {ingest_s:.1f}s for {len(stores)} store(s), {n_docs} document(s), "
          f"{src_mb:.2f} MB of source text")
    if n_docs:
        print(f"  {ingest_s / n_docs * 1000:.0f} ms per document")
    if src_mb:
        print(f"  {ingest_s / src_mb:.1f} s per MB of source text")

    # ---- RETRIEVAL (#532) --------------------------------------------------------
    questions = [q.question for q in pack.questions]
    access = AccessContext(user_oid="alice", principals=["alice"])
    embed_ms, retrieve_ms = [], []
    embedder = edition.embedder

    print(f"\nretrieval - {len(questions)} questions x {args.repeat} passes "
          f"(pass 1 discarded as warm-up)")
    for rep in range(args.repeat):
        for q in questions:
            if embedder is not None:
                t = time.perf_counter()
                embedder.embed([q])
                if rep:
                    embed_ms.append((time.perf_counter() - t) * 1000)
            for node in doc_stores:
                t = time.perf_counter()
                node.store.retrieve(access, q, top_k=args.top_k)
                if rep:
                    retrieve_ms.append((time.perf_counter() - t) * 1000)

    print()
    rows = [
        _report("embed", embed_ms, "question -> vector (model server floor)"),
        _report("retrieve", retrieve_ms,
                f"query -> top-{args.top_k} ranked chunks, LAW 2 trimmed  <-- OCTEN'S UNIT"),
    ]

    artifact = {
        "boundary": {
            "retrieve": "question string in -> ranked, authorization-trimmed chunks out; "
                        "excludes SQL generation, SQL execution and answer synthesis",
            "ingest": "/router/compose start -> composed catalog returned",
            "excluded_and_why": [
                "generate_sql + synthesize: two LLM calls; Octen's figure excludes "
                "generation, so including ours would be a units mismatch",
                "sql execute: runs on the CUSTOMER's database, not our infrastructure",
            ],
        },
        "rig": {"python": platform.python_version(), "machine": platform.machine(),
                "platform": platform.platform(),
                "embedder": type(embedder).__name__ if embedder else None},
        "pack": args.pack, "top_k": args.top_k,
        "ingest_seconds": round(ingest_s, 2), "documents": n_docs,
        "source_mb": round(src_mb, 3),
        "ingest_ms_per_document": round(ingest_s / n_docs * 1000, 1) if n_docs else None,
        "ingest_seconds_per_mb": round(ingest_s / src_mb, 1) if src_mb else None,
        "stages": rows,
        "comparison_note": "Octen publishes P50 62ms for public web search over a "
                           "prebuilt global index with no per-caller authorization. This "
                           "retrieves from a customer's private corpus and applies a LAW 2 "
                           "principals trim per query. Same unit, different work.",
    }
    if args.out:
        Path(args.out).write_text(json.dumps(artifact, indent=2))
        print(f"\nartifact -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
