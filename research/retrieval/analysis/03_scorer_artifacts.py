#!/usr/bin/env python3
"""03 - Are some "failures" actually correct answers?

Yes. At least two. This matters more than its size: a scorer that punishes correct
answers makes every measurement pessimistic and, worse, can make a real improvement look
like a regression.

Two scoring primitives, both exact-ish string tests applied to free prose:

  key_facts   word-anchored phrase matching   (eval/tally.py:14)
  abstention  a fixed marker list             (eval/__init__.py:24)

This runs them directly on captured answers - no server, no model, deterministic.

    python3 research/retrieval/analysis/03_scorer_artifacts.py
"""
from _common import PRE, evidence, head, runs

from dbsearch.eval import _ABSTAIN_MARKERS, abstained
from dbsearch.eval.tally import phrase_present


def main() -> None:
    head("03  ARE SOME 'FAILURES' CORRECT ANSWERS?")
    byid = {e["id"]: e for e in evidence("live_asks_pre461.json")["doc_path"]}

    a = byid["A-004"]
    print("  A-004  -- paraphrase rejected by phrase matching")
    print(f"    question : {a['question']}")
    print(f"    gold fact: {a['key_facts'][0]!r}")
    print(f"    answered : {a['answer']}")
    print(f"    phrase_present(answer, gold) -> {phrase_present(a['answer'], a['key_facts'][0])}")
    print("    human verdict: the answer is CORRECT.")
    print("    'no minimum length of employment required' vs 'no minimum service")
    print("    requirement' - same meaning, different words, so a substring test says no.")

    g = byid["G-005"]
    print("\n  G-005  -- correct refusal rejected by the marker list")
    print(f"    question : {g['question']}   (answerable = False, so refusing IS correct)")
    print(f"    answered : {g['answer']}")
    print(f"    abstained(answer) -> {abstained(g['answer'])}")
    print("    markers it is tested against:")
    print("      " + ", ".join(repr(m) for m in _ABSTAIN_MARKERS[:6]) + ", ...")
    print("    The system refused clearly. None of the markers appear, so it scores as a")
    print("    failure to abstain.")

    print("\n  The codebase already knew. eval/__init__.py:32 says:")
    print('    "That is the trap this list sets: abstention is measured by WORDING, so any')
    print('     new decline phrasing must be added here."')
    print("  It was written when #218 made refusals more informative, warning that making")
    print("  the product MORE honest would silently score as LESS faithful.")

    run = runs()[PRE]
    passed = sum(1 for i in run["items"] if i.get("passed"))
    n = len(run["items"])
    print(f"\n  Cost, pre-#461 run:")
    print(f"    reported  : {passed}/{n}  ({100*passed/n:.1f}%)")
    print(f"    corrected : {passed+2}/{n}  ({100*(passed+2)/n:.1f}%)   <- FLOOR, only the 2 confirmed")

    print()
    print("  RESULT - #463")
    print("  Two confirmed, both the scorer rather than the product. 2 is a lower bound:")
    print("  the same brittleness may suppress items nobody inspected by hand.")
    print()
    print("  The fix is NOT to loosen the scorer into uselessness - a test that accepts any")
    print("  prose stops catching real wrong answers. Targeted: score prose key_facts on")
    print("  MEANING while keeping exact matching for identifiers and numbers (HX-90,")
    print("  812000 genuinely must appear verbatim), and detect abstention STRUCTURALLY")
    print("  rather than by wording.")
    print()
    print("  CAUTION: this cuts both ways. Post-#461, five capability-G items looked like")
    print("  the same artifact and were NOT - see 07. Assuming 'it is just the scorer'")
    print("  hid real fabrication until the answers were read.")


if __name__ == "__main__":
    main()
