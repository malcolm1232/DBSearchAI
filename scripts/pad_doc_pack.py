#!/usr/bin/env python3
"""#536 - build a DISTRACTOR-PADDED copy of a doc pack, to test whether retrieval stays
correct as a corpus grows.

Every correctness claim this product has is measured at one scale: the doc pack is 4.05MB
/ 120 documents. "Does it still retrieve the right thing at 500MB" is unanswerable, and
500MB is the realistic open-source user. This makes it answerable, cheaply, by keeping the
questions and gold answers IDENTICAL and growing only the noise.

    ./scripts/pad_doc_pack.py --factor 10 --out /tmp/doc_pack_10x
    PYTHONPATH=src python3 scripts/golden_runner.py --pack /tmp/doc_pack_10x ...

WHAT IS AND IS NOT SAFE AS A DISTRACTOR - this is the whole validity of the test:

- A distractor must be REAL PROSE from the same domain. Lorem ipsum sits somewhere useless
  in embedding space, so the fact-bearing chunk stays trivially separable and the test
  passes for the wrong reason.
- A distractor must NOT contain any gold `key_fact`. Otherwise padding can hand the model
  a correct answer from a document that was never gold, which INFLATES the score - the
  opposite of what this measures. Documents referenced by any question's `doc_qrels` are
  excluded, and so is any document whose text contains a key_fact substring (on the real
  pack that removes 21 of 96 otherwise-eligible documents, mostly on the 3-character fact
  "WIS" matching incidentally - conservative on purpose: fewer, certainly-clean
  distractors beat more, possibly-helpful ones).
- Copies are SENTENCE-SHUFFLED with a seeded RNG. Byte-identical duplicates collapse onto
  one point in embedding space and would either all rank together or all be deduped - both
  of which test something other than "can the right chunk still be found". Shuffling
  changes the vector without inventing a single new fact.
- Distractors are `public`, so they are visible to the asking identity. An ACL-invisible
  distractor is trimmed before scoring and pads nothing.

The result is a pack whose ONLY difference from the original is how much irrelevant text
the right answer has to beat. Any item that passes at 1x and fails at Nx is a recall
cliff, and the N where it flips localises which fixed constant caused it (top_k=5 x
rerank_factor=4 = a 20-candidate pool regardless of corpus size; the evidence merge cap of
12; see #537 for the full inventory).
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
from pathlib import Path

_SENT = re.compile(r"(?<=[.!?])\s+")


def _shuffled(text: str, rng: random.Random) -> str:
    """Same sentences, different order. No new facts can appear, and the embedding moves."""
    parts = [p for p in _SENT.split(text) if p.strip()]
    if len(parts) < 2:
        return text
    rng.shuffle(parts)
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default="unstructured documents/doc_pack")
    ap.add_argument("--out", required=True)
    ap.add_argument("--factor", type=float, required=True,
                    help="target total corpus size as a multiple of the original")
    ap.add_argument("--seed", type=int, default=536)
    args = ap.parse_args()

    src, out = Path(args.pack), Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src, out)

    questions = [json.loads(l) for l in (src / "questions.jsonl").read_text().splitlines()
                 if l.strip()]
    gold_ids = {d for q in questions for d in (q.get("doc_qrels") or [])}
    facts = [str(f).casefold() for q in questions for f in (q.get("key_facts") or []) if f]

    doc_dirs = sorted({p.parent for p in (src / "docs").rglob("*.json")})
    if len(doc_dirs) != 1:
        print(f"expected exactly one doc store dir, found {len(doc_dirs)}: {doc_dirs}")
        return 1
    store_dir = doc_dirs[0]
    docs = [json.loads(p.read_text()) for p in sorted(store_dir.rglob("*.json"))]

    def is_clean(d: dict) -> bool:
        if d["external_id"] in gold_ids:
            return False
        low = d.get("text", "").casefold()
        return not any(f in low for f in facts)

    pool = [d for d in docs if is_clean(d)]
    original_chars = sum(len(d.get("text", "")) for d in docs)
    excluded = len(docs) - len(pool)
    if not pool:
        print("no clean distractors available - cannot pad honestly")
        return 1

    target_extra = int(original_chars * (args.factor - 1))
    if target_extra <= 0:
        print(f"factor {args.factor} adds nothing; pack copied unchanged")
        return 0

    rng = random.Random(args.seed)
    written, added_chars, rounds = 0, 0, 0
    out_store = out / "docs" / store_dir.name
    while added_chars < target_extra:
        rounds += 1
        for d in pool:
            if added_chars >= target_extra:
                break
            text = _shuffled(d.get("text", ""), rng)
            # Belt and braces: never emit a distractor carrying a gold fact, even if the
            # shuffle somehow produced one. A silent inflation would be worse than a crash.
            low = text.casefold()
            if any(f in low for f in facts):
                continue
            ext = f"pad{rounds:04d}-{d['external_id']}"
            (out_store / f"{ext}.json").write_text(json.dumps({
                "external_id": ext,
                "title": f"[padding {rounds}] {d.get('title', '')}",
                "uri": f"pad://{rounds}/{d.get('uri', ext)}",
                "text": text,
                "acl": "public",          # must be VISIBLE or it pads nothing
            }, indent=1))
            written += 1
            added_chars += len(text)

    total = original_chars + added_chars
    print(f"padded pack -> {out}")
    print(f"  distractor pool   : {len(pool)} clean of {len(docs)} docs "
          f"({excluded} excluded: gold or key-fact bearing)")
    print(f"  documents         : {len(docs)} -> {len(docs) + written}")
    print(f"  corpus chars      : {original_chars:,} -> {total:,} "
          f"({total / original_chars:.1f}x)")
    print(f"  approx MB         : {original_chars/1e6:.2f} -> {total/1e6:.2f}")
    print(f"  questions/gold    : UNCHANGED ({len(questions)} questions)")
    print(f"\n  est. ingest at the measured 70.6 s/MB: {total/1e6*70.6/60:.0f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
