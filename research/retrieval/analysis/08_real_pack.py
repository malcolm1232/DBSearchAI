#!/usr/bin/env python3
"""08 - The real-data pack (#473): what breaks when the corpus stops grading itself.

Every number 01-07 reports was measured on `eval_fixtures/golden_pack`, which the same
model family wrote end to end - documents, questions AND answers - and then sat. This
run uses `golden_pack_real`: three unrelated public Kaggle datasets, and an answer key
produced by EXECUTING SQL on an independent sqlite engine. A human wrote the question
and the query; nothing model-authored is in the answer key.

Two lenses, because they disagree and the disagreement is the point:

* the scorer's pass rate, which counts an honest decline on an unanswerable item as a
  pass and an honest decline on an answerable one as a failure;
* the ANSWER-LEVEL verdict - correct, declined, or confidently wrong - which is what a
  user actually experiences. The second is not in the scorer at all.

    python3 research/retrieval/analysis/08_real_pack.py
"""
from _common import ROOT, head, runs, table

from dbsearch.eval.golden.stage2 import fact_in

import json

RUN = "real_pack_final1"
CONFIRM = "real_pack_final2"          # identical inputs, re-run to separate noise
ANSWERS = ROOT / "research/retrieval/evidence/real_pack_answers.json"

CAPABILITIES = {
    "A": "single-table lookup",
    "B": "aggregate",
    "C": "within-store join",
    "D": "CROSS-STORE join",
    "E": "value linking (#462)",
    "F": "wrong-vocab paraphrase",
    "G": "unanswerable, must decline (#467)",
}

# How the product declines. Matched as substrings against the lowercased answer; this is
# reporting prose, not scoring - the scorer has its own _ABSTAIN_MARKERS.
DECLINES = ("i do not have that information", "none of the sources", "not guessed")


def verdict(answer: str, gold: list) -> str:
    """correct / declined / WRONG, from the answer text alone.

    An unanswerable item (no gold) is correct only by declining. An answerable item is
    correct only by asserting the executed figure; declining is a miss but an HONEST
    one, and asserting anything else is the failure that actually costs a user."""
    declined = any(d in answer.lower() for d in DECLINES)
    if not gold:
        return "declined" if declined else "WRONG"
    if all(fact_in(answer, fact) for fact in gold):
        return "correct"
    return "declined" if declined else "WRONG"


def main() -> None:
    all_runs = runs()
    run, confirm = all_runs[RUN], all_runs[CONFIRM]

    head("Reproducibility first - is a single run worth reading?")
    a = {i["id"]: i["passed"] for i in run["items"]}
    b = {i["id"]: i["passed"] for i in confirm["items"]}
    flips = [k for k in sorted(a) if a[k] != b[k]]
    print(f"  two runs, identical inputs: {len(flips)} verdict flip(s)"
          f"{' -> ' + ', '.join(flips) if flips else ' - stable, so the split below is real'}")

    head("Scorer pass rate, by capability")
    rows = []
    for cap, label in CAPABILITIES.items():
        items = [i for i in run["items"] if i["capability"] == cap]
        passed = sum(1 for i in items if i["passed"])
        rows.append({"capability": cap, "what it tests": label,
                     "passed": f"{passed}/{len(items)}"})
    total = sum(1 for i in run["items"] if i["passed"])
    rows.append({"capability": "", "what it tests": "TOTAL",
                 "passed": f"{total}/{len(run['items'])}"})
    table(rows, ["capability", "what it tests", "passed"])
    att = {}
    for item in run["items"]:
        if not item["passed"]:
            att[item["attribution"]] = att.get(item["attribution"], 0) + 1
    print(f"\n  D, E and F are ZERO. Those three are the entire gap - and the failures are"
          f"\n  not retrieval: {att}. 18 of 19 reached the right"
          "\n  store and then got the question wrong downstream. An embedder cannot fix any"
          "\n  of them.")

    head("Answer-level verdict - what a user would actually get")
    answers = json.load(open(ANSWERS))
    tally = {"correct": 0, "declined": 0, "WRONG": 0}
    wrong = []
    for item in answers:
        v = verdict(item["answer"], item["gold"])
        tally[v] += 1
        if v == "WRONG":
            wrong.append(item)
    n = len(answers)
    print(f"  correct   {tally['correct']:>3}/{n}")
    print(f"  declined  {tally['declined']:>3}/{n}   (honest: says it does not know)")
    print(f"  WRONG     {tally['WRONG']:>3}/{n}   ({100*tally['WRONG']/n:.0f}% - asserts "
          "something untrue, with no hedge)")

    head("The confidently-wrong answers, verbatim")
    for item in wrong:
        print(f"\n  {item['id']}  gold={item['gold'] or 'must decline'}")
        print(f"    Q: {item['question']}")
        print(f"    A: {item['answer'][:150]}")
        if item["sql"]:
            print(f"    SQL: {item['sql'][0][:150]}")

    head("Why they are wrong - five distinct causes, not one")
    for item_id, card, why in (
        ("B-003", "#475", "numeric literal quoted as a string: yearID='2015' against a REAL "
                          "column matches nothing, and 'None' is reported as the answer"),
        ("D-004", "#474", "no cross-store join, so the generator wrote the degenerate "
                          "SUM(product_category_name) inside the one store it could see"),
        ("E-002", "#462", "'science fiction' is stored as Sci-Fi"),
        ("E-003", "#462", "'dominican republic' is stored as D.R."),
        ("E-005", "#477", "invented an extra predicate (delivery dates) the question never "
                          "asked for, and the over-constrained query returned nothing"),
        ("F-001", "#477", "paraphrase; substituted an unrelated proxy column - COUNT(DISTINCT "
                          "order_item_id) WHERE freight_value IS NOT NULL - for 'delivered'"),
        ("G-001", "#467", "fabrication: an HR compensation question routed to BASEBALL on the "
                          "token overlap 'hr, salary' - hr is home runs - and answered AVG(HR)"),
    ):
        print(f"  {item_id}  {card}  {why}")
    print("\n  Cutting across four of them (#476): an EMPTY RESULT SET is narrated as a"
          "\n  factual zero. 'There are 0 films in the catalogue that are science fiction'"
          "\n  is not a retrieval failure the user can detect - it reads like an answer."
          "\n  Zero rows means the query was wrong, not that the quantity is zero.")


if __name__ == "__main__":
    main()
