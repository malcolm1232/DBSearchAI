#!/usr/bin/env python3
"""05 - Is this even achievable, and what must the payload contain?

Before optimising anything, ask whether the information the generator RECEIVES is
sufficient. If a strong model cannot answer from that exact payload, the payload is the
bug and no model swap helps.

METHODOLOGY NOTE - read this before trusting any number here.

The first version of this test was run by an assistant that had ALREADY READ the gold
SQL. It scored 18/18 and concluded "the payload is sufficient". That was contaminated:
knowing `customer_id=29485` was the answer, it wrote `customer_id=29485` without
thinking, and so never discovered what a model does when it cannot see a column's type.

Re-run with a context-free generator - given only the payload, no gold, no repo access,
no knowledge that this was a test - the same 18 items scored 11/18. All seven failures
were one defensive cast:

    WHERE CAST(customer_id AS TEXT) = '29485'    ->  '29485.0' != '29485'  ->  no rows

Blind to types, casting everything to text is a REASONABLE strategy. It is right for
text and fatal for numbers. Adding types to the payload took the same protocol to 18/18.

The lesson generalises past this bug: an evaluator who knows the answer cannot measure
the difficulty of not knowing it.

    python3 research/retrieval/analysis/05_ceiling_test.py
"""
import csv
import json

from _common import RESEARCH, ROOT, head, questions, table

from dbsearch.eval.golden.pack import load_pack
from dbsearch.eval.golden.stage2 import gold_value

PACK = load_pack(ROOT / "eval_fixtures/golden_pack")
QS = questions()


def distinct(table_name: str, col: str) -> list:
    for store in PACK.tables.values():
        if table_name in store:
            rows = list(csv.reader(open(store[table_name])))
            i = rows[0].index(col)
            return list(dict.fromkeys(r[i] for r in rows[1:]))
    return []


def run(sql: str):
    try:
        return gold_value(PACK.tables, sql)
    except Exception:
        return "ERR"


def correct(qid: str, sql: str) -> bool:
    gold = gold_value(PACK.tables, QS[qid]["gold_sql"])
    got = run(sql)
    return got is not None and got != "ERR" and abs(float(got) - float(gold)) < 1e-9


def main() -> None:
    head("05  CEILING TEST - IS THE PAYLOAD SUFFICIENT?")

    print("  What the generator received for B-001, BEFORE #468:")
    print("    Schema:")
    print("      marketing_spend(region, channel, amount)")
    print("    Question: What was the total marketing spend in the US on paid search?")
    print()
    print("  Value encodings in the data - never shown to the generator (LAW 1):")
    for t, c in (("marketing_spend", "region"), ("marketing_spend", "channel")):
        print(f"    {t}.{c} = {distinct(t, c)}")
    print()
    print('  The question says "US" and "paid search"; the data says "us" and')
    print('  "paid-search". Every literal the generator writes is a guess.')

    gen = json.load(open(RESEARCH / "evidence" / "context_free_sql.json"))
    names, typed = gen["names_only"], gen["with_types"]
    items = list(typed)

    print("\n  Context-free generator, per item:")
    table([{"item": q, "gold": gold_value(PACK.tables, QS[q]["gold_sql"]),
            "names only": "PASS" if correct(q, names[q]) else "fail",
            "names+types": "PASS" if correct(q, typed[q]) else "fail"}
           for q in items],
          ["item", "gold", "names only", "names+types"])

    n = len(items)
    a = sum(correct(q, names[q]) for q in items)
    b = sum(correct(q, typed[q]) for q in items)
    print()
    print(f"    context-free, names only  : {a}/{n}  ({100*a//n}%)")
    print(f"    context-free, names+types : {b}/{n}  ({100*b//n}%)   <- #468")
    print(f"    contaminated (knew gold)  : {n}/{n}  (100%)  <- DISREGARD, see docstring")

    print("\n  The two SQL styles, side by side (E-001):")
    print(f"    names only : {names['E-001']}")
    print(f"    names+types: {typed['E-001']}")

    print()
    print("  RESULT")
    print("  The payload IS sufficient - but only once it carries types. The gap was never")
    print("  the model's SQL ability; it was that _SQL_SYSTEM promised 'names and types'")
    print("  while the payload sent names only (#468, fixed).")
    print()
    print("  Note WHICH items moved. The B-series - the separator problem that looked like")
    print("  the hard case - passed in BOTH conditions: a blind generator hedges with")
    print("  LIKE '%paid%search%' and gets there. It was the E/F numeric filters, which")
    print("  looked trivial, that failed.")
    print()
    print("  That inverts the prior ranking. Value linking (#462) is still the robust")
    print("  answer for messy real data, but it is not the top lever it appeared to be.")
    print()
    print("  WHAT THIS DOES NOT CLAIM: that llama3.1:8b in-loop reaches this. The ceiling")
    print("  is what a strong model can do from the payload; the probe measures what the")
    print("  shipping config actually does. The two can disagree - see 07.")


if __name__ == "__main__":
    main()
