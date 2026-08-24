#!/usr/bin/env python3
"""Live probe for #519 and #518 - READ the served response, write no code first.

Both cards end with the same instruction: do not tune blind. #519's leading hypothesis is
that the LLM decomposer splits D-002/E-004 into halves that route to DIFFERENT stores, so
#503's same-store test correctly passes them through as compound and never fires. #518
asks whether D-003's disclosed reason is really F-005's ("the filter half carried no key
values") or a second cause #504's repair cannot reach.

Neither question needs new code to answer - it needs the decision the server already made.
This dumps, per item: the answer, the decomposer's sub_queries with each half's ROUTED
store, every disclosure, and any literal-repair trace.

    ./scripts/probe_519_518.py            # rig must already be up on :8099
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.eval.golden import load_pack                      # noqa: E402
from dbsearch.eval.http_probe import call                       # noqa: E402
from golden_runner import _AUTH_HEADERS, pack_manifest          # noqa: E402

ITEMS = {"D-002": "#519", "E-004": "#519", "D-003": "#518", "F-005": "#518 control"}


def _walk(node, key, seen=None):
    """Every value stored under `key` anywhere in a nested response - the field names
    differ by layer (decision.sub_queries vs trace entries), and guessing one path is how
    a probe reports 'absent' for something that was there under another name."""
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                found.append(v)
            found.extend(_walk(v, key))
    elif isinstance(node, list):
        for v in node:
            found.extend(_walk(v, key))
    return found


def _show(item_id, tag, resp):
    print(f"\n{'=' * 78}\n{item_id}  ({tag})\n{'=' * 78}")
    answer = resp.get("answer")
    print(f"ANSWER: {str(answer)[:300]}")
    for field in ("sub_queries", "subqueries", "decomposition", "halves"):
        for hit in _walk(resp, field):
            print(f"\n{field.upper()}: {json.dumps(hit, indent=2)[:1500]}")
    for field in ("store_id", "store", "routed_store", "stores"):
        hits = _walk(resp, field)
        if hits:
            print(f"{field}: {json.dumps(hits)[:400]}")
    disclosures = _walk(resp, "disclosures") + _walk(resp, "disclosure")
    if disclosures:
        print(f"\nDISCLOSURES: {json.dumps(disclosures, indent=2)[:1200]}")
    for field in ("sql", "repair", "repairs", "substitutions", "reason", "trace"):
        hits = _walk(resp, field)
        if hits:
            print(f"\n{field.upper()}: {json.dumps(hits, indent=2)[:1400]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8099")
    parser.add_argument("--pack", default=str(ROOT / "eval_fixtures" / "golden_pack_real"))
    parser.add_argument("--out", default=None, help="also dump raw JSON here")
    parser.add_argument("--no-compose", action="store_true")
    args = parser.parse_args()

    header = _AUTH_HEADERS["dev"]
    pack = load_pack(Path(args.pack))
    if not args.no_compose:
        manifest = pack_manifest(pack, "alice", "bob")
        status, resp = call(args.base, "/router/compose", {"manifest": manifest}, None,
                            identity="alice", identity_header=header, timeout=3600)
        if status >= 400:
            print(f"COMPOSE FAILED {status}: {resp}")
            return 1
        print(f"composed: {status}")

    raw = {}
    for item in pack.questions:
        if item.id not in ITEMS:
            continue
        print(f"\n>>> asking {item.id}: {item.question}")
        status, resp = call(args.base, "/router/ask", {"question": item.question}, None,
                            identity="alice", identity_header=header, timeout=300)
        if status >= 400:
            print(f"  ask failed {status}: {resp}")
            continue
        raw[item.id] = resp
        _show(item.id, ITEMS[item.id], resp)

    if args.out:
        Path(args.out).write_text(json.dumps(raw, indent=2))
        print(f"\nraw -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
