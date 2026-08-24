#!/usr/bin/env python3
"""02 - What is actually inside "synthesis-miss"?

`synthesis-miss` is what the scorer calls a failure when routing and retrieval both
looked fine (eval/golden/stage2.py:130 - precedence: leak, routing, retrieval, then
synthesis).

That single label covers two situations with OPPOSITE fixes:

  1. the system genuinely answered wrong, or
  2. the system answered correctly and the SCORER rejected the phrasing.

Lumping them together is how a scoring bug gets mistaken for a product bug. Splitting by
whether an item carries a `gold_sql` separates the structured path from the document one.

    python3 research/retrieval/analysis/02_synthesis_decomposition.py
"""
from collections import Counter

from _common import PRE, evidence, head, questions, runs, table


def main() -> None:
    qs = questions()
    items = [i for i in runs()[PRE]["items"]
             if not i.get("passed") and i.get("attribution") == "synthesis-miss"]
    sql = [i for i in items if qs[i["id"]].get("gold_sql")]
    docs = [i for i in items if not qs[i["id"]].get("gold_sql")]

    head("02  WHAT IS INSIDE synthesis-miss?")
    print(f"  synthesis-miss total          : {len(items)}")
    print(f"    SQL path (gold_sql present) : {len(sql)}")
    print(f"    doc path (prose key_facts)  : {len(docs)}")

    print("\n  Which stage-2 check fired:")
    c = Counter(f for i in items for f in (i["stage2"].get("failures") or []))
    table([{"check": k, "items": v} for k, v in c.most_common()], ["check", "items"])

    print("\n  Doc-path items and what they needed:")
    table([{"id": i["id"], "cap": i["capability"],
            "needs": ", ".join(map(str, qs[i["id"]].get("key_facts") or [])) or "(abstention)"}
           for i in docs], ["id", "cap", "needs"])

    print("\n  Captured answers (evidence/live_asks_pre461.json - no server needed):")
    for e in evidence("live_asks_pre461.json")["doc_path"]:
        print(f"    {e['id']:<8} {e['verdict'].split(' - ')[0]}")
        print(f"             {e['answer'][:96]}")

    print()
    print("  RESULT")
    print("  The failures split roughly 18 structured / 8 document. Of the document ones,")
    print("  at least two are CORRECT answers the scorer rejected (see 03), and the rest")
    print("  are genuine federation failures - the system found part of the answer and")
    print("  never joined to the rest.")
    print()
    print("  The 18 structured failures turn out to be ONE defect wearing many hats (04).")


if __name__ == "__main__":
    main()
