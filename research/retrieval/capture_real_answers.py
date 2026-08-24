#!/usr/bin/env python3
"""Capture the real pack's ANSWERS and generated SQL, verbatim, as evidence.

This is the ONLY script under `research/retrieval/` that touches the network. Everything
in `analysis/` is read-only over committed artifacts; this one exists because the run
artifact written by `golden_runner.py` records metrics and failure buckets but NOT the
prose the product actually produced - and the metric alone cannot tell you whether a
failure is a real defect or a scoring artifact.

That distinction is the lesson of 260731: a 5-point drop was first read as a scorer
repeat and turned out to be fabrication. Reading the outputs was the only thing that
revealed it. So the outputs get committed alongside the numbers.

    PYTHONPATH=src python3 research/retrieval/capture_real_answers.py \\
        --base http://127.0.0.1:8099
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.eval.http_probe import call  # noqa: E402

PACK = ROOT / "eval_fixtures" / "golden_pack_real"
OUT = ROOT / "research" / "retrieval" / "evidence" / "real_pack_answers.json"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8099")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)

    items = [json.loads(line) for line in open(PACK / "questions.jsonl")]
    captured = []
    for item in items:
        status, result = call(args.base, "/router/ask", {"question": item["question"]},
                              None, identity="alice", identity_header="X-DBSearch-User")
        citations = result.get("citations", []) if isinstance(result, dict) else []
        routing = result.get("routing", {}) if isinstance(result, dict) else {}
        captured.append({
            "id": item["id"],
            "capability": item["capability"],
            "question": item["question"],
            "gold": list(item.get("key_facts", [])),
            "gold_sql": item.get("gold_sql", ""),
            "status": status,
            "answer": (result.get("answer") if isinstance(result, dict) else str(result)) or "",
            "routed": [s.get("store_id") for s in routing.get("stores", [])],
            "routed_why": [s.get("why") for s in routing.get("stores", [])],
            "sql": [c.get("sql") for c in citations if c.get("sql")],
            "cited_tables": sorted({c.get("table") for c in citations if c.get("table")}),
            # #479: whether a literal was resolved server-side, and to what. Without this
            # the evidence cannot distinguish a repair from the generator happening to
            # write the stored encoding itself - and those look identical in the SQL.
            "resolved": [c["proof"]["resolved"] for c in citations
                         if (c.get("proof") or {}).get("resolved")],
        })
        print(f"{item['id']:6} [{status}] {captured[-1]['answer'][:90]}")

    Path(args.out).write_text(json.dumps(captured, indent=1) + "\n")
    print(f"\nwrote {len(captured)} answers -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
