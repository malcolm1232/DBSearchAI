"""Shared loaders for the retrieval-efficacy analyses.

Every script in this directory is READ-ONLY over committed artifacts: `eval_results/`,
`eval_fixtures/golden_pack/`, and `research/retrieval/evidence/`. None starts a server,
calls a model, or touches the network, so each runs in about a second and its output
cannot drift away from the prose in the README.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "eval_results").is_dir())
sys.path.insert(0, str(ROOT / "src"))
RESEARCH = ROOT / "research" / "retrieval"

LEXICAL = "hermetic-lexical_45033e201713"
PRE = "semantic_subset_probe"        # pre-#461: regex keyword_sql_generator
POST = "semantic_subset_461fix"      # post-#461: real LLM generator


def runs() -> dict:
    """Committed baselines, scratch runs, and the RETAINED copies under research/.

    `eval_results/runs/` is gitignored, so anything cited here also lives in
    `research/retrieval/evidence/runs/`. That directory is searched LAST and wins: the
    tracked copy is the one the analysis was written against. This ordering exists
    because the original probe artifact was once lost with a deleted worktree.
    """
    out = {}
    for d in (ROOT / "eval_results" / "baselines", ROOT / "eval_results" / "runs",
              RESEARCH / "evidence" / "runs"):
        for f in sorted(d.glob("*.json")):
            out[f.stem] = json.load(open(f))
    return out


def questions() -> dict:
    return {q["id"]: q for q in (json.loads(l) for l in
            open(ROOT / "eval_fixtures/golden_pack/questions.jsonl"))}


def evidence(name: str) -> dict:
    return json.load(open(RESEARCH / "evidence" / name))


def head(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def table(rows: list, cols: list) -> None:
    """Minimal fixed-width table - no pandas dependency for a handful of rows."""
    widths = [max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows)) for c in cols]
    print("  " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(cols, widths)))


def profile(run: dict, ids=None) -> dict:
    """Pass count and failure attribution for a run, optionally restricted to `ids`."""
    items = run["items"] if ids is None else [i for i in run["items"] if i["id"] in ids]
    att = Counter(i.get("attribution") for i in items if not i.get("passed"))
    s2 = Counter(f for i in items for f in (i["stage2"].get("failures") or []))
    passed = sum(1 for i in items if i.get("passed"))
    return {"passed": passed, "total": len(items),
            "rate": f"{100*passed/len(items):.1f}%" if items else "-",
            "routing-miss": att.get("routing-miss", 0),
            "retrieval-miss": att.get("retrieval-miss", 0),
            "synthesis-miss": att.get("synthesis-miss", 0),
            "chk:key-facts": s2.get("key-facts", 0),
            "chk:exec-accuracy": s2.get("execution-accuracy", 0),
            "chk:abstention": s2.get("abstention", 0)}


def shared_ids(a: dict, b: dict) -> list:
    """Two runs are only comparable on the items they BOTH contain."""
    return sorted({i["id"] for i in a["items"]} & {i["id"] for i in b["items"]})
