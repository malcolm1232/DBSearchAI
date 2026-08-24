#!/usr/bin/env python3
"""Golden pack freeze gate (spec 2026-07-31 section 5, review C4). Deterministic,
LLM-free. The pack is only frozen once this exits 0. Run in the selftest sweep via
tests/selftest_validate_golden_pack.py and manually before every baseline.

    python3 scripts/validate_golden_pack.py [--pack eval_fixtures/golden_pack]

Each check_* is a pure function pack -> list[str] of human-readable problem
messages (no out-params: callers never mutate a shared list). `CHECKS` is an
open/closed tuple: adding a check means writing check_foo and appending it."""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.eval.golden.pack import GoldenPack, load_pack          # noqa: E402
from dbsearch.eval.golden.stage2 import fact_in, gold_value          # noqa: E402
from dbsearch.eval.tally import is_numeric_token, num_str, phrase_present  # noqa: E402

_PII_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    re.compile(r"\+?\d[\d\s().-]{9,}\d"),
)


def _doc_text(pack: GoldenPack, doc_id: str):
    for docs in pack.docs.values():
        for d in docs:
            if d["external_id"] == doc_id:
                return d["text"]
    return None


def _all_docs(pack: GoldenPack):
    """Yield (doc_id, text, acl) for every doc in the pack, regardless of store."""
    for docs in pack.docs.values():
        for d in docs:
            yield d["external_id"], d["text"], d.get("acl")


def _all_table_names(pack: GoldenPack) -> set:
    return {name for tables in pack.tables.values() for name in tables}


def _first_numeric_fact(key_facts):
    for fact in key_facts:
        try:
            return float(fact)
        except (TypeError, ValueError):
            continue
    return None


def check_qrels_exist(pack: GoldenPack) -> list:
    """doc_qrels/negative_qrels ids exist in pack.docs, gold_table exists in
    pack.tables, variant_of targets an existing question id, ids are unique."""
    problems = []
    doc_ids = {doc_id for doc_id, _, _ in _all_docs(pack)}
    table_names = _all_table_names(pack)
    question_ids = {q.id for q in pack.questions}
    seen: set = set()
    for item in pack.questions:
        if item.id in seen:
            problems.append(f"{item.id}: duplicate question id")
        seen.add(item.id)
        for doc_id in (*item.doc_qrels, *item.negative_qrels):
            if doc_id not in doc_ids:
                problems.append(f"{item.id}: qrel doc {doc_id!r} not found in pack.docs")
        if item.gold_table and item.gold_table not in table_names:
            problems.append(f"{item.id}: gold_table {item.gold_table!r} not found in pack.tables")
        if item.variant_of and item.variant_of not in question_ids:
            problems.append(f"{item.id}: variant_of {item.variant_of!r} is not an existing question id")
    return problems


def check_gold_sql(pack: GoldenPack) -> list:
    """Every gold_sql must execute via the independent sqlite gold engine; if the
    item also carries a numeric key_fact, the executed result must match it."""
    problems = []
    for item in pack.questions:
        if not item.gold_sql:
            continue
        try:
            gold = gold_value(pack.tables, item.gold_sql)
        except Exception as exc:  # noqa: BLE001 - any sqlite/broker failure is a problem
            problems.append(f"{item.id}: gold_sql failed to execute: {exc}")
            continue
        fact = _first_numeric_fact(item.key_facts)
        if fact is None:
            continue
        try:
            gold_f = float(gold)
        except (TypeError, ValueError):
            problems.append(f"{item.id}: gold_sql result {gold!r} is not numeric but key_fact {fact} is")
            continue
        if abs(gold_f - fact) >= 0.005:
            problems.append(f"{item.id}: gold_sql result {gold_f} does not match key_fact {fact}")
    return problems


def check_fact_not_in_question(pack: GoldenPack) -> list:
    """No key_fact may be readable straight off the question text - that would let
    a model 'answer' the item by echoing the question, not by retrieving anything."""
    problems = []
    for item in pack.questions:
        for fact in item.key_facts:
            if fact_in(item.question, fact):
                problems.append(f"{item.id}: key_fact {fact!r} present in its own question text")
    return problems


def check_forbidden_not_in_question(pack: GoldenPack) -> list:
    """Sub-questions echo into routing.reason (spec), so a forbidden_fact sitting in
    the question text would false-LEAK regardless of what the model retrieves."""
    problems = []
    for item in pack.questions:
        for fact in item.forbidden_facts:
            if fact_in(item.question, fact):
                problems.append(f"{item.id}: forbidden_fact {fact!r} present in its own question text")
    return problems


def check_forbidden_not_in_public_docs(pack: GoldenPack) -> list:
    """A forbidden_fact that a public doc already says is not actually forbidden -
    the item cannot legitimately fail for leaking something everyone can read."""
    problems = []
    public_docs = [(doc_id, text) for doc_id, text, acl in _all_docs(pack) if acl == "public"]
    for item in pack.questions:
        for fact in item.forbidden_facts:
            for doc_id, text in public_docs:
                if fact_in(text, fact):
                    problems.append(
                        f"{item.id}: forbidden_fact {fact!r} present in public doc {doc_id!r}")
    return problems


def check_negative_qrels_lack_truth(pack: GoldenPack) -> list:
    """A distractor doc that actually contains the answer is not a distractor."""
    problems = []
    for item in pack.questions:
        for doc_id in item.negative_qrels:
            text = _doc_text(pack, doc_id)
            if text is None:
                continue
            for fact in item.key_facts:
                if fact_in(text, fact):
                    problems.append(
                        f"{item.id}: key_fact {fact!r} present in negative_qrel doc {doc_id!r}")
    return problems


def check_token_discipline(pack: GoldenPack) -> list:
    """Non-numeric forbidden_facts under 3 chars are too short to anchor reliably
    (mirrors usecase_runner._sensitive_tokens' floor)."""
    problems = []
    for item in pack.questions:
        for fact in item.forbidden_facts:
            if not is_numeric_token(str(fact)) and len(str(fact)) < 3:
                problems.append(f"{item.id}: forbidden_fact {fact!r} is shorter than 3 chars")
    return problems


def _alignment_value_str(gold) -> str:
    if isinstance(gold, (int, float)):
        return num_str(float(gold))
    return str(gold)


def check_alignments(pack: GoldenPack) -> list:
    """pack.meta['alignments'] entries assert a cross-store truth: a SQL result must
    be readable in prose in the paired doc (capability-5 cross-store questions)."""
    problems = []
    for entry in pack.meta.get("alignments", []):
        doc_id = entry.get("must_appear_in_doc")
        text = _doc_text(pack, doc_id)
        if text is None:
            problems.append(f"{doc_id}: alignment target doc not found in pack.docs")
            continue
        try:
            gold = gold_value(pack.tables, entry.get("sql", ""))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{doc_id}: alignment sql failed to execute: {exc}")
            continue
        value = _alignment_value_str(gold)
        if not phrase_present(text, value):
            problems.append(f"{doc_id}: alignment sql result {value!r} not present in doc text")
    return problems


def check_derivations(pack: GoldenPack) -> list:
    """pack.meta['derivations'] record the provenance of every NON-numeric key_fact.

    Execution accuracy is numeric-only, so a question whose answer is a string carries no
    gold_sql and is scored on fact recall alone - which is exactly where an authored
    answer could slip in unchecked. A derivation re-executes the query that produced the
    fact, so a string answer is held to the same engine-derived standard as a number
    (#473: no answer in the real pack is model-authored)."""
    problems = []
    by_id = {q.id: q for q in pack.questions}
    for entry in pack.meta.get("derivations", []):
        item = by_id.get(entry.get("id"))
        if item is None:
            problems.append(f"{entry.get('id')}: derivation names a question not in the pack")
            continue
        try:
            gold = gold_value(pack.tables, entry.get("sql", ""))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{item.id}: derivation sql failed to execute: {exc}")
            continue
        value = _alignment_value_str(gold)
        if value != entry.get("value"):
            problems.append(
                f"{item.id}: derivation re-executes to {value!r}, recorded {entry.get('value')!r}")
        elif value not in item.key_facts:
            problems.append(
                f"{item.id}: derived value {value!r} is not among its key_facts {list(item.key_facts)}")
    return problems


def check_unique_tables(pack: GoldenPack) -> list:
    """Table names are unique pack-wide (the sqlite namespace is flat), and every
    table must have at least one data row - a header-only CSV lets gold_value's
    column sniffing treat an empty body as vacuously REAL (Task 5 deferral,
    authorized extension: reject that input class here)."""
    problems = []
    seen: dict = {}
    for store_id, tables in pack.tables.items():
        for name, path in tables.items():
            if name in seen:
                problems.append(f"table {name}: duplicate across stores {seen[name]!r} and {store_id!r}")
            else:
                seen[name] = store_id
            with open(path, newline="") as f:
                rows = list(csv.reader(f))
            if len(rows) <= 1:
                problems.append(f"table {name}: zero data rows")
    return problems


def check_profiles(pack: GoldenPack) -> list:
    """Haystack and wrong-vocab items are, by design, unsolvable by exact lexical
    match; listing hermetic-lexical in profiles would gate a profile that can never
    pass (review C1)."""
    problems = []
    for item in pack.questions:
        gated = "haystack" in item.hardness or "wrong-vocab" in item.hardness
        if gated and "hermetic-lexical" in item.profiles:
            problems.append(f"{item.id}: haystack/wrong-vocab item lists hermetic-lexical in profiles")
    return problems


def check_pii(pack: GoldenPack) -> list:
    """Synthetic content must not look like a real email or phone number."""
    problems = []
    for doc_id, text, _ in _all_docs(pack):
        if any(p.search(text) for p in _PII_PATTERNS):
            problems.append(f"{doc_id}: doc text matches a PII-looking pattern")
    return problems


CHECKS = (
    check_qrels_exist, check_gold_sql, check_fact_not_in_question,
    check_forbidden_not_in_question, check_forbidden_not_in_public_docs,
    check_negative_qrels_lack_truth, check_token_discipline, check_alignments,
    check_derivations, check_unique_tables, check_profiles, check_pii,
)


def validate(pack: GoldenPack) -> list:
    return [problem for check in CHECKS for problem in check(pack)]


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="Validate a golden pack (freeze gate).")
    parser.add_argument("--pack", default=str(ROOT / "eval_fixtures" / "golden_pack"))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    pack = load_pack(Path(args.pack))
    problems = validate(pack)
    for p in problems:
        print(f"FAIL {p}")
    status = "FROZEN-ELIGIBLE" if not problems else f"{len(problems)} problem(s)"
    print(f"pack {pack.content_hash[:12]}: {status}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
