#!/usr/bin/env python3
"""06 - Why do the WHERE literals miss, and what would actually fix it?

05 narrowed the residual failure to WHERE literals. This asks whether the fix already
shipped - and it nearly did. The shared _SQL_SYSTEM prompt carries a rule from #230:

    "You are given names and types but NEVER any data values, so you CANNOT know how a
     value is cased or spelled in the table. When filtering on a TEXT value taken from
     the question, always compare case-insensitively - WHERE LOWER(col) = LOWER('the
     value') - never a bare = 'the value'."

That is a real fix for a real bug ('touring bikes' vs 'Touring Bikes'). The question is
whether it is SUFFICIENT here.

    python3 research/retrieval/analysis/06_value_encoding.py
"""
from _common import ROOT, head, table

from dbsearch.eval.golden.pack import load_pack
from dbsearch.eval.golden.stage2 import gold_value

PACK = load_pack(ROOT / "eval_fixtures/golden_pack")

CASES = {
    "gold":
        "SELECT SUM(amount) FROM marketing_spend "
        "WHERE region='us' AND channel='paid-search'",
    "LOWER only (what #230 instructs)":
        "SELECT SUM(amount) FROM marketing_spend "
        "WHERE LOWER(region)=LOWER('US') AND LOWER(channel)=LOWER('paid search')",
    "LOWER + REPLACE (separator-aware)":
        "SELECT SUM(amount) FROM marketing_spend WHERE LOWER(region)='us' "
        "AND REPLACE(LOWER(channel),'-',' ')='paid search'",
    "LIKE hedge (what a blind model wrote)":
        "SELECT SUM(amount) FROM marketing_spend WHERE LOWER(TRIM(region)) IN "
        "('us','usa','united states') AND REPLACE(LOWER(channel),'-',' ') LIKE '%paid search%'",
}


def resolve_literal(written: str, distinct: list) -> str:
    """Sketch of server-side literal resolution: map a value as WRITTEN in the question
    onto a value as STORED, without the model ever seeing the value list."""
    norm = lambda s: "".join(ch for ch in s.lower() if ch.isalnum())
    for d in distinct:
        if norm(d) == norm(written):
            return d
    hits = [d for d in distinct if norm(written) in norm(d) or norm(d) in norm(written)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        return f"AMBIGUOUS{hits}"          # must fail honestly, never silently pick one
    return written


def main() -> None:
    head("06  WHY DO THE LITERALS MISS?")

    rows = []
    for name, sql in CASES.items():
        try:
            v = gold_value(PACK.tables, sql)
        except Exception as e:
            v = f"ERR {e}"
        rows.append({"condition": name, "result": v, "correct": v == 812000.0})
    table(rows, ["condition", "result", "correct"])

    print()
    print("  RESULT - #462")
    print("  The shipped rule handles CASING and not SEPARATORS. LOWER('paid search') is")
    print("  'paid search'; the column holds 'paid-search'. No rows, NULL, and the user is")
    print("  told 'I don't have that information'.")
    print()
    print("  That is the failure mode #230's own comment warns about, in its own words:")
    print('    "A WRONG FILTER IS INDISTINGUISHABLE FROM MISSING DATA: the worst kind of')
    print('     failure, because it hides itself."')
    print()
    print("  WHY NOT JUST ADD ANOTHER PROMPT RULE")
    print("  We could add REPLACE. Then hit 'United States' vs us, or 'NY' vs 'New York',")
    print("  or 'Paid Search (Brand)'. Format variance is open-ended; a rule enumerating")
    print("  it never terminates. The generator needs contact with the actual values.")
    print()
    print("  THE LAW 1 CONSTRAINT THAT SHAPES THE FIX")
    print("  Pasting distinct values into the prompt is NOT legal for hosted providers.")
    print("  From the same #230 comment: 'the model gets the SCHEMA ONLY - names and")
    print("  types, never a row (LAW 1: customer data must not leave the tenant)'.")
    print("  Column values ARE customer data. Two clean designs:")
    print("    (a) values only when the model is in-tenant - a per-adapter capability.")
    print("        Works, but splits behaviour by provider and leaves hosted broken.")
    print("    (b) server-side literal resolution - the model emits the value AS WRITTEN")
    print("        in the question; the server resolves it against the column's distinct")
    print("        values before executing. Nothing leaves, works for every provider.")
    print()
    print("  Sketch of (b) - resolution where the values already are:")
    channels = ["paid-search", "social", "paid-social", "email", "display"]
    for w in ("paid search", "Paid Search", "PAID SEARCH", "email", "paid"):
        print(f"    {w!r:>14} -> {resolve_literal(w, channels)!r}")
    print()
    print("  Note the last line. 'paid' matches BOTH paid-search and paid-social, and the")
    print("  sketch returns AMBIGUOUS rather than guessing. That ambiguity handling is the")
    print("  substance of #462 - a resolver that silently picks one is a fabrication engine.")
    print()
    print("  PRIORITY NOTE: 05 demoted this. A blind generator already hedges its way past")
    print("  separators with LIKE. #462 matters for messy REAL data, not for this corpus.")


if __name__ == "__main__":
    main()
