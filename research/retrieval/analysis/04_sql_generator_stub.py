#!/usr/bin/env python3
"""04 - Who wrote that broken SQL?

Four questions each asking for a single number produced SELECT SUM(region) - summing a
TEXT column - or SELECT * ... LIMIT 5. The natural assumption is that the 8B model is
too weak.

It never wrote them. This calls the regex stub DIRECTLY, offline, and compares its
output to what the live system emitted. If they match byte for byte, no model was
involved.

    python3 research/retrieval/analysis/04_sql_generator_stub.py
"""
from _common import evidence, head, table

from dbsearch.adapters.anthropic import AnthropicLlm
from dbsearch.adapters.groq import GroqLlm
from dbsearch.adapters.llama import LlamaLlm
from dbsearch.router.structured import keyword_sql_generator

SCHEMA = [{"table": "marketing_spend",
           "columns": [{"name": "region", "type": "TEXT"},
                       {"name": "channel", "type": "TEXT"},
                       {"name": "amount", "type": "INTEGER"}]}]


def main() -> None:
    head("04  WHO WROTE THAT BROKEN SQL?")
    ev = evidence("live_asks_pre461.json")

    print("  Regex stub called offline vs what the LIVE system produced:")
    rows = []
    for e in ev["sql_path"]:
        stub = keyword_sql_generator(e["question"], SCHEMA)
        rows.append({"id": e["id"], "stub output": stub[:52],
                     "matches live": "YES" if stub == e["generated_sql"] else "no"})
    table(rows, ["id", "stub output", "matches live"])

    print("\n  Every one matches. The live SQL is byte-identical to a regex run offline.")

    print("\n  Why it produces SUM(region) - trace B-001 through structured.py:513:")
    print("    1. matches \\b(total|sum)\\b            -> agg = SUM")
    print("    2. no 'by X'                          -> group = None")
    print("    3. mentioned(columns) looks for a literal column name in the question.")
    print("       Columns are region/channel/amount; the question says 'in the US on")
    print("       paid search' - none appear                      -> None")
    print("    4. falls back to the FIRST column                  -> region")
    print("    5. emits SELECT SUM(region) AS total_region FROM marketing_spend")
    print()
    print("    B-002 has no trigger word at all, so it drops to the last line:")
    print("       SELECT * FROM {table} LIMIT {top_k}")
    print()
    print("    The stub's own docstring: 'deterministic naive NL2SQL over the FIRST")
    print("    table - the demo default, loudly not a real generator.'")

    print("\n  Why it was reached at all - router_api.py:294 gates on this hasattr:")
    table([{"adapter": n, "generate_sql": hasattr(c, "generate_sql")}
           for n, c in (("LlamaLlm (Ollama/vLLM/self-host)", LlamaLlm),
                        ("GroqLlm", GroqLlm),
                        ("AnthropicLlm", AnthropicLlm))],
          ["adapter", "generate_sql"])
    print("    Before #461 the first row was False, so _sql_llm resolved to None and EVERY")
    print("    SQL store kept the deterministic default. On any self-host rig, all")
    print("    text-to-SQL was a regex; the chat model only wrote prose over its rows.")
    print("    That is why swapping in a stronger chat model (01) changed almost nothing.")

    b002 = next(e for e in ev["sql_path"] if e["id"] == "B-002")
    print("\n  Why nobody noticed - the failure hides itself:")
    print(f"    question: {b002['question']}")
    print(f"    SQL     : {b002['generated_sql']}")
    print(f"    answer  : {b002['answer']}")
    print("    truth   : 812000")
    print("    No exception, no empty result, no warning. A wrong number, fluently stated,")
    print("    with a citation attached. Worse than an error, because nothing signals it.")

    print()
    print("  RESULT - #461 fixed, but NOT closed. See 07: wiring a real generator")
    print("  unmasked a fabrication defect the stub had been masking.")


if __name__ == "__main__":
    main()
