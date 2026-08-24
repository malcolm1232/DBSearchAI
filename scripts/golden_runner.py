#!/usr/bin/env python3
"""Golden retrieval-efficacy runner (spec 2026-07-31 Task 9). Drives a live DBSearch
server over HTTP: composes the golden pack as a manifest, asks every selected item as
alice (re-asking as bob on protection=="restricted" items, LAW 2), scores each answer
with the Task 4-6 pure scorers, and gates the run against a saved baseline.

    python3 scripts/golden_runner.py --profile hermetic-lexical --full --set-baseline

`_compose_pack`, `_bob_catalog_problem` and `run_items` are the only functions that touch
the network - the rest are pure, proven in-process by tests/selftest_golden_eval.py."""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# This module's OWN directory, so `golden_manifest` resolves however this file is loaded -
# as a script, as an import, or by file path via importlib (selftest_golden_eval does the
# last, and it puts nothing on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dbsearch.eval.golden import (  # noqa: E402
    attribute, gate, gold_value, load_pack, score_stage1, score_stage2, scorecard,
)
from dbsearch.eval.http_probe import (
    WARM_QUESTION, call, catalog_visible, warm_rig,
)  # noqa: E402


# Manifest building (pure) lives in golden_manifest (#478) - re-exported here so
# existing `from golden_runner import pack_manifest` call sites keep working.
from golden_manifest import (  # noqa: E402,F401
    _csv_columns_rows, _docs_store, _seed_acl, _sql_store, pack_manifest,
)


def _hardness_cells(questions) -> set:
    return {(q.capability, tag) for q in questions for tag in q.hardness}


def _first_uncovered_variant(variants: list, selected_ids: set, cap: str, tag: str):
    """First not-yet-selected variant (pre-sorted by id) of `cap` carrying `tag`."""
    for q in variants:
        if q.id not in selected_ids and q.capability == cap and tag in q.hardness:
            return q
    return None


def select_subset(questions, full: bool) -> list:
    """full = every item for the profile. default = the stratified subset (spec
    section 7): every parent (`variant_of == ""`) PLUS, per (capability, hardness
    tag) cell not yet covered, the first (id sort) variant carrying that tag.
    `questions` is already profile-filtered, so an added variant is always
    profile-eligible. Parents alone miss query-variant/wrong-vocab/haystack cells
    (those tags live only on variants), which used to drop that whole dimension
    from the default tier. Deterministic: no randomness."""
    if full:
        return list(questions)
    parents = [q for q in questions if not q.variant_of]
    selected = list(parents)
    selected_ids = {q.id for q in parents}
    covered = _hardness_cells(parents)
    variants = sorted((q for q in questions if q.variant_of), key=lambda q: q.id)
    for cap, tag in sorted(_hardness_cells(questions)):
        if (cap, tag) in covered:
            continue
        q = _first_uncovered_variant(variants, selected_ids, cap, tag)
        if q is not None:
            selected.append(q)
            selected_ids.add(q.id)
            covered.update(_hardness_cells([q]))
    return selected



# --------------------------------------------------------------------------------- #
# Scoring glue - takes already-fetched /router/ask response dicts. `_gold_for`,
# `_restricted_verdict`, `score_row` and `check_parity` are pure (no I/O); `_error_row`
# prints its reason and lives here since it is only called from `run_items` below.
# --------------------------------------------------------------------------------- #

def _gold_for(pack, item):
    """gold_value executes BEFORE asking (spec section 8); a failing gold_sql is
    left to the caller (run_items) to turn into a per-item run ERROR."""
    if not item.gold_sql:
        return None
    return gold_value(pack.tables, item.gold_sql)


def _restricted_verdict(item, gold, bob_result, passed: bool, attribution: str) -> tuple:
    """LAW 2: a restricted item with no bob re-ask fails closed; otherwise bob's
    OWN response is scored for a leak, which overrides alice's own verdict."""
    if bob_result is None:
        return False, "error"
    bob_s2 = score_stage2(bob_result, item, gold=gold)
    if "leak" in bob_s2["failures"]:
        return False, "leak"
    return passed, attribution


def score_row(item, alice_result: dict, bob_result: "dict | None", gold,
             mode: str = "demo") -> dict:
    """Pure: the full stage1+stage2+attribution pipeline for one item's already-fetched
    responses, in the Task 6 row shape. Callable directly without ever touching HTTP.

    On a protection=="restricted" item, alice is the AUTHORIZED asker: her reaching the
    restricted store and stating the forbidden fact IS the correct answer, not a leak.
    So her own stage1/stage2 are scored against a copy with forbid_stores/
    forbidden_facts cleared - those two fields exist only to judge BOB's re-ask, via
    `_restricted_verdict` below, which uses the item UNCHANGED.

    `mode` is the rig's auth mode ("dev"/"demo", from --auth), not a hardcoded
    literal (MINOR-8): the slice key is `capability|tag|mode`. Defaults to "demo"
    so existing in-process callers are unaffected."""
    alice_item = item
    if item.protection == "restricted":
        alice_item = replace(item, forbid_stores=(), forbidden_facts=())
    s1 = score_stage1(alice_result, alice_item)
    s2 = score_stage2(alice_result, alice_item, gold=gold)
    passed = not s1["failures"] and not s2["failures"]
    attribution = attribute(s1, s2)
    if item.protection == "restricted":
        passed, attribution = _restricted_verdict(item, gold, bob_result, passed, attribution)
    return {"id": item.id, "cluster": item.variant_of or item.id, "capability": item.capability,
            "hardness": list(item.hardness), "mode": mode, "passed": passed,
            "attribution": attribution, "stage1": s1, "stage2": s2}


def _error_row(item, reason: str, mode: str = "demo") -> dict:
    """A row that fails CLOSED before any scoring ran (transport error, a >=400
    status on either ask, or a gold_sql that would not execute)."""
    print(f"ERROR {item.id}: {reason}")
    return {"id": item.id, "cluster": item.variant_of or item.id, "capability": item.capability,
            "hardness": list(item.hardness), "mode": mode, "passed": False,
            "attribution": "error", "stage1": {"metrics": {}, "failures": [reason]},
            "stage2": {"metrics": {}, "failures": [reason]}}


def check_parity(items, rows) -> list:
    """Ledger ruling (b): gate.compare silently ignores a slice that vanishes from
    `current`, so a dropped item reads as clean rather than the bug it is. Returns
    human-readable problems; [] means every selected item produced one row."""
    item_ids, row_ids = [q.id for q in items], [r["id"] for r in rows]
    problems = []
    if len(item_ids) != len(row_ids):
        problems.append(f"{len(item_ids)} item(s) selected but {len(row_ids)} row(s) produced")
    missing = sorted(set(item_ids) - set(row_ids))
    if missing:
        problems.append(f"no row produced for {missing}")
    return problems


# --------------------------------------------------------------------------------- #
# HTTP (impure) - the only functions in this module that touch the network
# --------------------------------------------------------------------------------- #

def run_items(base, pack, items, session_alice, session_bob,
              identity_alice: str = "alice", identity_bob: str = "bob",
              identity_header: str = "X-DBSearch-Demo-User", mode: str = "demo") -> list:
    """Ask every item as alice; re-ask as bob (unconditionally) when protection ==
    "restricted". A transport error or >=400 status on EITHER ask fails the item
    closed via `_error_row`. `mode` (the CLI's --auth string) threads into every
    row's "mode" field (MINOR-8)."""
    rows = []
    for item in items:
        try:
            gold = _gold_for(pack, item)
        except Exception as exc:                       # noqa: BLE001 - fail closed per item
            rows.append(_error_row(item, f"gold_sql failed to execute: {exc}", mode=mode))
            continue
        try:
            status, alice_result = call(base, "/router/ask", {"question": item.question},
                                        session_alice, identity=identity_alice,
                                        identity_header=identity_header)
        except Exception as exc:                       # noqa: BLE001
            rows.append(_error_row(item, f"transport error: {exc}", mode=mode))
            continue
        if status >= 400:
            rows.append(_error_row(item, f"server returned {status}", mode=mode))
            continue
        bob_result = None
        if item.protection == "restricted":
            try:
                b_status, bob_result = call(base, "/router/ask", {"question": item.question},
                                            session_bob, identity=identity_bob,
                                            identity_header=identity_header)
            except Exception as exc:                    # noqa: BLE001
                rows.append(_error_row(item, f"bob re-ask transport error: {exc}", mode=mode))
                continue
            if b_status >= 400:
                rows.append(_error_row(item, f"bob re-ask returned {b_status}", mode=mode))
                continue
        rows.append(score_row(item, alice_result, bob_result, gold, mode=mode))
    return rows


# --------------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------------- #

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="DBSearch golden retrieval-efficacy runner")
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--pack", default=str(ROOT / "eval_fixtures" / "golden_pack"),
                        help="pack dir; golden_pack_real is the real-data pack (#473)")
    parser.add_argument("--profile", required=True, choices=("hermetic-lexical", "semantic"))
    parser.add_argument("--embedding", default="memory-hashing")
    parser.add_argument("--chat-model", default="extractive")
    parser.add_argument("--full", action="store_true",
                        help="every item for the profile, not just the stratified subset")
    parser.add_argument("--set-baseline", action="store_true")
    parser.add_argument("--no-warm", action="store_true",
                        help="skip the #491 warm-up asks (hermetic rigs have no cold "
                             "start to wait out)")
    parser.add_argument("--stamp", default=None,
                        help="run artifact file stem; default (if omitted) is "
                             "'{profile}_{pack-hash-first8}', still overridable")
    parser.add_argument("--auth", default="dev", choices=("dev", "demo"),
                        help="dev (default): X-DBSearch-User, a real identity oid - required "
                             "for /router/compose (403s any demo:* identity) and so /router/ask "
                             "reaches the composed workspace, not the baked demo catalog. "
                             "demo: X-DBSearch-Demo-User, the hosted-demo header.")
    return parser.parse_args(argv)


_AUTH_HEADERS = {"dev": "X-DBSearch-User", "demo": "X-DBSearch-Demo-User"}


def _compose_pack(args, manifest: dict, identity_header: str) -> "str | None":
    """POST /router/compose the golden pack manifest as alice. None on success,
    else a problem string. NOTE (Task 9 -> Task 10): /router/compose 403s ANY
    "demo:*" identity by design, and a demo:* identity's /router/ask always routes
    to the baked demo catalog, never the composed workspace - `--auth dev` (the
    default) sends `X-DBSearch-User` instead, which on a bare local rig with no
    real login resolves alice/bob to REAL identity oids reaching the SAME
    composed workspace on both compose and ask."""
    # timeout: a real document corpus is EMBEDDED at compose time - minutes, not the
    # seconds a small SQL fixture takes, which the default turned into a fake failure.
    status, resp = call(args.base, "/router/compose", {"manifest": manifest}, None,
                        identity="alice", identity_header=identity_header, timeout=3600)
    if status >= 400:
        return f"compose failed with status {status}: {resp}"
    return None


def _bob_catalog_problem(base: str, identity_header: str) -> "str | None":
    """Probe bob's OWN catalog before trusting any item result (spec section 3, the
    #368 workspace-regime check). None if bob can see the composed catalog, else a
    problem string. A per-owner-workspace rig can leave bob's catalog empty even
    though compose just succeeded for alice - a different, empty workspace, not a
    permission boundary - which would make LAW 2/leak checks pass VACUOUSLY green
    and a frozen baseline launder that silently. Aborts BEFORE any item runs."""
    status, resp = call(base, "/router/catalog", None, None, identity="bob",
                        identity_header=identity_header)
    if status >= 400:
        return f"bob's /router/catalog returned {status}: {resp}"
    if not catalog_visible(resp):
        return "bob cannot see the composed catalog (empty/absent, per-owner-workspace regime)"
    return None


def _report_parity_problems(problems: list) -> None:
    print("RUN ERROR: row-count parity violated (a gate compare on this run would "
         "be invalid):")
    for p in problems:
        print(f"  {p}")


def _write_and_report(rows: list, args, pack, key: dict) -> dict:
    """Build the scorecard (MINOR-7 notes: auth/base/tier), write the run under an
    auto stamp when --stamp was omitted, and print the tally."""
    stamp = args.stamp or f"{args.profile}_{pack.content_hash[:8]}"
    note = f"auth={args.auth} base={args.base} tier={'full' if args.full else 'subset'}"
    card = scorecard.build_scorecard(rows, key, notes=note)
    run_path = scorecard.write_run(card, ROOT / "eval_results" / "runs", stamp)
    passed = sum(1 for r in rows if r["passed"])
    print(f"wrote run: {run_path}")
    print(f"{passed}/{len(rows)} passed")
    return card


def _report_gate(result) -> int:
    if result.red:
        print("GATE RED:")
        for r in result.regressions:
            print(f"  {r}")
    else:
        print("GATE GREEN")
    return 1 if result.red else 0


def main(argv=None) -> int:
    args = parse_args(argv)
    pack = load_pack(Path(args.pack))
    filtered = [q for q in pack.questions if args.profile in q.profiles]
    items = select_subset(filtered, args.full)
    print(f"golden runner: pack {pack.content_hash[:12]}, profile {args.profile!r}, "
         f"{len(items)} item(s) selected")

    identity_header = _AUTH_HEADERS[args.auth]
    manifest = pack_manifest(pack, "alice", "bob")
    compose_problem = _compose_pack(args, manifest, identity_header)
    if compose_problem:
        print(f"RUN ERROR: {compose_problem}")
        return 3

    catalog_problem = _bob_catalog_problem(args.base, identity_header)
    if catalog_problem:
        print(f"RUN ERROR: {catalog_problem} - a frozen baseline here would launder "
             "vacuously-green LAW 2/leak checks (spec section 3, #368)")
        return 3

    if not args.no_warm:
        spent, ready = warm_rig(lambda: call(
            args.base, "/router/ask", {"question": WARM_QUESTION}, None,
            identity="alice", identity_header=identity_header, timeout=300))
        state = "READY" if ready else "STILL COLD - treat run 1 per the #483 discard rule"
        print(f"warm-up: {spent} throwaway ask(s), rig {state}")

    rows = run_items(args.base, pack, items, None, None,
                     identity_header=identity_header, mode=args.auth)
    problems = check_parity(items, rows)
    if problems:
        _report_parity_problems(problems)
        return 3

    key = scorecard.baseline_key(args.profile, args.embedding, args.chat_model, pack.content_hash)
    card = _write_and_report(rows, args, pack, key)

    if args.set_baseline:
        baseline_path = scorecard.save_baseline(card, ROOT / "eval_results" / "baselines")
        print(f"baseline saved: {baseline_path}")
        return 0

    baseline = scorecard.load_baseline(ROOT / "eval_results" / "baselines", key)
    if baseline is None:
        print(f"no baseline yet for key {key}; run again with --set-baseline to create one")
        return 2

    gate.check_keys(key, baseline["key"])
    result = gate.compare(rows, baseline["items"])
    return _report_gate(result)


if __name__ == "__main__":
    raise SystemExit(main())
