"""Scorecard build/persist + baseline key (spec 2026-07-31 section 6). Baselines are
committed under eval_results/baselines/; run outputs land in eval_results/runs/
(gitignored). A baseline is only comparable to a run with the IDENTICAL key."""
from __future__ import annotations

import json
from pathlib import Path

from . import gate


def baseline_key(profile: str, embedding: str, chat_model: str, pack_hash: str) -> dict:
    return {"profile": profile, "embedding": embedding,
            "chat_model": chat_model, "pack_hash": pack_hash}


def build_scorecard(rows: list, key: dict, notes: "str | None" = None) -> dict:
    """`notes` is an optional short free-text annotation (e.g. auth mode, base URL,
    full-vs-subset tier) stored verbatim under "notes"; None (the default) keeps
    every existing caller's output unchanged (MINOR-7)."""
    slices = {}
    for name, srows in sorted(gate.slice_rows(rows).items()):
        clusters = {r["cluster"] for r in srows}
        failed = sorted({r["cluster"] for r in srows if not r["passed"]})
        slices[name] = {"n": len(srows), "n_clusters": len(clusters),
                        "passed": sum(1 for r in srows if r["passed"]),
                        "failed_clusters": failed}
    return {"key": key, "slices": slices, "items": rows, "notes": notes}


def write_run(card: dict, out_dir: Path, stamp: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stamp}.json"
    path.write_text(json.dumps(card, indent=1))
    return path


def _baseline_path(directory: Path, key: dict) -> Path:
    return directory / f"{key['profile']}_{key['pack_hash'][:12]}.json"


def save_baseline(card: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = _baseline_path(directory, card["key"])
    path.write_text(json.dumps(card, indent=1))
    return path


def load_baseline(directory: Path, key: dict) -> dict | None:
    path = _baseline_path(directory, key)
    if not path.exists():
        return None
    card = json.loads(path.read_text())
    return card if card["key"] == key else None
