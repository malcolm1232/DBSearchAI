#!/usr/bin/env python3
"""07 - Did fixing #461 move the number? (No. It made it worse, and that was useful.)

04 showed the SQL came from a regex because LlamaLlm had no generate_sql. #461 adds it,
so router_api's capability gate selects the real LLM generator.

The isolation is the point: same rig, same pack, same embedder, same chat model. The only
change is which component writes the SQL.

The result was a DROP, 24/56 -> 19/56. The first hypothesis was that this was #463 all
over again - a better generator phrasing refusals more naturally and being punished by a
marker list. Reading the actual answers refuted that. Five of the six new failures are
real fabrication. Recorded below verbatim, because "we assumed it was the scorer" is
exactly the mistake this file exists to prevent repeating.

    python3 research/retrieval/analysis/07_461_verification.py
"""
from _common import POST, PRE, head, profile, runs, shared_ids, table

# Captured live from the post-#461 rig. All six are answerable=False: the store does not
# exist, so the only correct response is to say so.
G_ANSWERS = [
    ("G-001", "I do not have that information.",
     "CORRECT refusal"),
    ("G-007", "We do have access to an Exec Comp dataset.",
     "FABRICATION - flatly false"),
    ("G-002", "There is no data in the Exec Comp system that matches the query's filter "
              "criteria. The COUNT(*) result from the query is 0.",
     "FABRICATION - asserts the system EXISTS and is merely empty"),
    ("G-008", "The Exec Comp source contains None. [12]",
     "FABRICATION - asserts the source exists"),
    ("G-003", "The company does not have an Exec Comp store that can be queried, since "
              "the COUNT(*) result is 0 (from [11]).",
     "right answer, INVENTED evidence"),
    ("G-005", "There is no Exec Comp data source connected here. The query result shows "
              "COUNT(*)=0 for both marketing_spend and sales_orders where "
              "LOWER(channel/item) = 'exec comp'.",
     "right answer, INVENTED evidence"),
]


def main() -> None:
    r = runs()
    if PRE not in r or POST not in r:
        print(f"need both {PRE} and {POST} in eval_results/ - see README")
        return

    a, b = r[PRE], r[POST]
    ids = shared_ids(a, b)
    head(f"07  DID #461 MOVE THE NUMBER?   ({len(ids)} shared items)")

    pa, pb = profile(a, ids), profile(b, ids)
    table([{"metric": k, "pre-#461 (regex stub)": pa[k], "post-#461 (real LLM)": pb[k]}
           for k in ("passed", "rate", "routing-miss", "retrieval-miss", "synthesis-miss",
                     "chk:key-facts", "chk:exec-accuracy", "chk:abstention")],
          ["metric", "pre-#461 (regex stub)", "post-#461 (real LLM)"])

    ai = {i["id"]: i for i in a["items"]}
    bi = {i["id"]: i for i in b["items"]}
    up = [i for i in ids if not ai[i].get("passed") and bi[i].get("passed")]
    down = [i for i in ids if ai[i].get("passed") and not bi[i].get("passed")]
    print(f"\n  newly PASSING ({len(up)}): {up}")
    print(f"  newly FAILING ({len(down)}): {down}")

    print("\n  The abstention check went 1 -> 6. It fires only on answerable=False items,")
    print("  i.e. questions the system SHOULD refuse. All six are capability G, asking")
    print("  about an 'Exec Comp' store that does not exist. What it actually said:\n")
    for qid, ans, verdict in G_ANSWERS:
        print(f"    {qid}  [{verdict}]")
        print(f"          {ans}")

    print()
    print("  RESULT - #467, and #461 stays OPEN")
    print("  Only G-001 refuses correctly. G-007 states the opposite of the truth. G-002")
    print("  and G-008 assert the source exists. G-003 and G-005 reach the right answer")
    print("  through invented evidence - G-005 searched for a STORE NAME as a column")
    print("  VALUE (LOWER(channel)='exec comp'), got zero rows, and narrated that as proof.")
    print()
    print("  ROOT CAUSE. With a real generator wired, the model writes syntactically valid")
    print("  SQL against an UNRELATED table instead of returning CANNOT_ANSWER. That is the")
    print("  #211 fabrication class the codebase was hardened against. The regex stub was")
    print("  ACCIDENTALLY safer: its output led the synthesizer to decline.")
    print()
    print("  So #461 is a real fix that UNMASKED a real defect. A score drop caused by")
    print("  removing something that was hiding a bug is progress, but it is not done.")
    print()
    print("  METHOD NOTE. The first read of this was 'probably a scorer artifact, like")
    print("  #463'. That was wrong, and only reading the six answers revealed it. When a")
    print("  metric moves, read the outputs before believing a story about the metric.")


if __name__ == "__main__":
    main()
