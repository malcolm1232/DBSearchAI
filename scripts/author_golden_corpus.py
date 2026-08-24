#!/usr/bin/env python3
"""Golden pack assembler (spec 2026-07-31 Task 8, extended Task 10 with --haystack).
Thin CLI: it never authors real content itself, only shape-checks, sorts, and stamps
a pack an agent already wrote - except for the haystack filler corpus, which IS
generated here because it is defined as "deterministic template variation over a
fixed wordlist", i.e. mechanical, not authored content.

    python3 scripts/author_golden_corpus.py --stamp --by "claude-opus-5" --on "2026-07-31"
    python3 scripts/author_golden_corpus.py --haystack 200

Steps: (a) json.loads every .json file and every questions.jsonl line to catch
shape errors before they reach the freeze gate, (b) rewrite questions.jsonl sorted
by id, (c) with --stamp, merge generated_by/generated_on/provenance into
pack_meta.json, (d) print the pack hash. No wall-clock reads: the stamp date comes
from --on, so re-running with the same flags is byte-for-byte reproducible.

`--haystack N` is a separate action (it returns before the shape-check/sort/stamp
flow): it (re)generates exactly N filler docs into docs/hr-haystack/, named
filler-0001.json.. filler-000N.json, each derived ONLY from its own index via fixed
wordlists and modular arithmetic - no `random`, no wall-clock reads - so running it
twice with the same N overwrites every file with byte-identical content (idempotent)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.eval.golden.pack import pack_hash  # noqa: E402

_HAYSTACK_DEPARTMENTS = (
    "Finance", "Operations", "Marketing", "Engineering", "Facilities", "Legal",
    "Procurement", "Customer Success", "Product", "Design",
)
_HAYSTACK_TOPICS = (
    "quarterly planning", "team offsite", "budget review", "vendor onboarding",
    "office relocation", "software licensing", "conference attendance",
    "training rollout", "equipment refresh", "process audit", "town hall recap",
    "calendar sync", "meeting cadence", "internal survey", "brown bag session",
    "hiring pipeline", "vendor renewal", "compliance checklist", "workspace update",
    "catering vendor",
)
_HAYSTACK_ADJECTIVES = (
    "routine", "upcoming", "recent", "quarterly", "annual", "internal", "informal",
    "preliminary", "standard", "optional",
)
_HAYSTACK_ACTIONS = (
    "reviewed", "discussed", "scheduled", "finalized", "circulated", "archived",
    "updated", "postponed", "summarized", "confirmed",
)


def _haystack_doc(i: int) -> dict:
    """One filler doc for index `i` (0-based). Every field is a pure function of
    `i` over the fixed wordlists above via modular arithmetic - deterministic,
    no randomness or wall-clock, so the same `i` always produces the same doc."""
    seq = i + 1
    dept = _HAYSTACK_DEPARTMENTS[i % len(_HAYSTACK_DEPARTMENTS)]
    topic1 = _HAYSTACK_TOPICS[i % len(_HAYSTACK_TOPICS)]
    topic2 = _HAYSTACK_TOPICS[(i * 7 + 3) % len(_HAYSTACK_TOPICS)]
    adj = _HAYSTACK_ADJECTIVES[(i * 3) % len(_HAYSTACK_ADJECTIVES)]
    action = _HAYSTACK_ACTIONS[(i * 5 + 1) % len(_HAYSTACK_ACTIONS)]
    quarter = f"Q{(i % 4) + 1}"
    text = (
        f"This is a {adj} internal memo from the {dept} team. This cycle the team "
        f"{action} the {topic1} initiative alongside {topic2} plans for {quarter}. "
        "No action items require immediate follow-up from other departments. This "
        "memo is part of the general internal record and does not describe "
        "compensation, budgets, or confidential deal information."
    )
    return {"external_id": f"filler-{seq:04d}", "title": f"{dept} Team Memo {seq:04d}",
            "text": text, "acl": "public"}


def _generate_haystack(root: Path, n: int) -> None:
    """(Re)write exactly `n` filler docs into docs/hr-haystack/, overwriting any
    existing filler-*.json in place - idempotent by construction (Task 10)."""
    out_dir = root / "docs" / "hr-haystack"
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        doc = _haystack_doc(i)
        (out_dir / f"{doc['external_id']}.json").write_text(
            json.dumps(doc, sort_keys=True, indent=2) + "\n")


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="Assemble/stamp a golden pack.")
    parser.add_argument("--stamp", action="store_true",
                         help="merge generated_by/generated_on/provenance into pack_meta.json")
    parser.add_argument("--by", default="", help="value for pack_meta.json generated_by")
    parser.add_argument("--on", default="", help="value for pack_meta.json generated_on (e.g. 2026-07-31)")
    parser.add_argument("--pack", default=str(ROOT / "eval_fixtures" / "golden_pack"))
    parser.add_argument("--haystack", type=int, default=0,
                         help="(re)generate N deterministic filler docs into docs/hr-haystack/ "
                              "and exit, without running the shape-check/sort/stamp flow")
    return parser.parse_args(argv)


def _check_json_files(root: Path) -> list:
    """json.loads every *.json file under root; a decode error is a shape error,
    reported with its path rather than raised, so main can print every problem
    before exiting instead of stopping at the first one."""
    problems = []
    for path in sorted(root.rglob("*.json")):
        try:
            json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            problems.append(f"{path.relative_to(root)}: {exc}")
    return problems


def _check_questions_lines(root: Path) -> list:
    """json.loads every non-blank line of questions.jsonl; same shape-error
    contract as _check_json_files."""
    problems = []
    jsonl = root / "questions.jsonl"
    if not jsonl.exists():
        return [f"{jsonl.relative_to(root)}: file not found"]
    for lineno, line in enumerate(jsonl.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"questions.jsonl:{lineno}: {exc}")
    return problems


def _rewrite_questions_sorted(root: Path) -> None:
    """Parse every question line, sort by id, and rewrite the file. Runs only
    after shape-checking has already passed, so every line is valid JSON here."""
    jsonl = root / "questions.jsonl"
    items = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    items.sort(key=lambda d: d["id"])
    jsonl.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in items))


def _stamp_meta(root: Path, by: str, on: str) -> None:
    """Merge the reproducibility stamp into pack_meta.json. The date is whatever
    --on says, never wall-clock, so two runs with the same flags produce the same
    bytes and therefore the same pack hash."""
    meta_path = root / "pack_meta.json"
    meta = json.loads(meta_path.read_text())
    meta.update({"generated_by": by, "generated_on": on, "provenance": "synthetic"})
    meta_path.write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n")


def main(argv=None) -> int:
    args = _parse_args(argv)
    root = Path(args.pack)
    if args.haystack:
        _generate_haystack(root, args.haystack)
        print(f"haystack: wrote {args.haystack} filler doc(s) to docs/hr-haystack/")
        return 0
    problems = _check_json_files(root) + _check_questions_lines(root)
    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        return 1
    _rewrite_questions_sorted(root)
    if args.stamp:
        _stamp_meta(root, args.by, args.on)
    print(f"pack {pack_hash(root)[:12]}: assembled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
