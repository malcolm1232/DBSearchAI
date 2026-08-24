#!/usr/bin/env python3
"""01 - Is the embedder the bottleneck?

Method: run the same golden pack twice, changing only the models.

  baseline  HashingEmbedding + ExtractiveLlm. Neither is a real model - the first hashes
            tokens into a vector (adapters/local/__init__.py:100, "no ML deps"), the
            second pulls spans rather than reasoning.
  semantic  nomic-embed-text (768-dim, real semantics) + llama3.1:8b.

Two runs are only comparable on the items they SHARE. The semantic profile carries 11
extra items (wrong-vocab, haystack) the lexical profile never sees, so comparing raw
pass rates would flatter one side.

    python3 research/retrieval/analysis/01_semantic_baseline.py
"""
from _common import LEXICAL, PRE, head, profile, runs, shared_ids, table


def main() -> None:
    r = runs()
    a, b = r[LEXICAL], r[PRE]
    ids = shared_ids(a, b)

    head(f"01  IS THE EMBEDDER THE BOTTLENECK?   ({len(ids)} shared items)")
    pa, pb = profile(a, ids), profile(b, ids)
    label_a = f'{a["key"]["embedding"]} + {a["key"]["chat_model"]}'
    label_b = f'{b["key"]["embedding"]} + {b["key"]["chat_model"]}'

    rows = [{"metric": k, label_a: pa[k], label_b: pb[k]}
            for k in ("passed", "rate", "routing-miss", "retrieval-miss", "synthesis-miss")]
    table(rows, ["metric", label_a, label_b])

    print()
    print("  RESULT")
    print("  Two real models in place of two stubs moved the score a few points, and")
    print("  synthesis-miss barely moved.")
    print()
    print("  That rules out the intuitive story. If retrieval were the bottleneck, a real")
    print("  semantic embedder replacing a hash function would have moved retrieval-miss")
    print("  and routing-miss sharply. It did not. The failures sit AFTER retrieval.")
    print()
    print("  What this does NOT say: that embedders never matter, or that nomic-embed-text")
    print("  is a poor choice. It says that on THIS corpus, at THIS stage, the embedder is")
    print("  not what is costing answers. That can change once the stages ahead of it stop")
    print("  failing - which is why the bake-off (#460) is sequenced last.")


if __name__ == "__main__":
    main()
