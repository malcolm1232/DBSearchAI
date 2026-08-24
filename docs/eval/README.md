# RAG eval harness (#42)

> *"I measure value, not hype."* Same retrieval, two models — does a bigger model actually
> answer better? The numbers decide, not the marketing.

## What it measures
- **Retrieval** (model-independent — it happens before generation, so it's run once and shared):
  `precision@k` and `hit-rate@k` over the messy, unstructured `sample_corpus/` (arXiv papers,
  NIST frameworks, Gutenberg books), with **real `nomic-embed-text` passage-level embeddings**.
- **Generation**, per model, on the *identical* retrieved context:
  - `key-fact recall` — answer **correctness** (expected facts present).
  - `faithfulness` — correct when answerable, **abstains** when not (no hallucination).
  - `abstention-on-unanswerable` — does it decline a question the corpus can't answer.

All metrics are deterministic (no LLM-as-judge), so the harness is reproducible and its logic is
unit-tested in `tests/selftest_eval.py` (hermetic — no Ollama). Retrieval goes through the real
permission-trimmed `QueryService`, so the eval can never score a doc a user isn't allowed to see.

## Headline result (2026-06-27, full output in `results-2026-06-27.txt`)
| | retrieval precision@5 | key-fact recall | faithfulness | abstention |
|---|---|---|---|---|
| (shared retrieval) | **1.0** (7/7) | — | — | — |
| `llama3.2:3b` | — | 0.857 | 0.875 | 1.0 |
| `llama3.1:8b` | — | **1.0** | **1.0** | 1.0 |

**The money question** — "Who is Sherlock Holmes's companion and narrator?" *Same retrieved passages*:
- `3b` → ❌ *"Irene Adler is Sherlock Holmes's companion and the narrator"* (hallucinated)
- `8b` → ✅ *"Dr. Watson"*

Retrieval was perfect for both; the 8B model is measurably **more faithful to the same context**.
That's the architect's point: spend the bigger-model budget where the eval shows it pays off.

## Run it
```bash
./scripts/fetch_corpus.sh          # populate sample_corpus/ (public PDFs/txt; gitignored)
ollama pull nomic-embed-text && ollama pull llama3.2:3b && ollama pull llama3.1:8b
python3 scripts/eval_rag.py
```

## Limitations (honest)
- **Doc cap**: each doc is capped to the first 40 KB to keep the eval fast; golden facts are
  answerable from each document's opening. Whole-corpus eval is future work.
- **Chunking**: the eval script does passage-level chunking itself; the product pipeline is still
  one-chunk-per-doc (Phase 1). Production passage chunking is future work.
- **Hybrid reranking (#44, now on)**: retrieval fuses vector + lexical (RRF) after the trim.
  On this corpus pure vector was already at precision@5 1.0, so hybrid holds hit-rate@5 at 1.0
  (the relevant doc is always surfaced) with precision@5 ≈ 0.97 — the expected precision/recall
  trade of hybrid; its win is rescuing exact-keyword matches dense vectors rank too low (see
  `tests/selftest_rerank.py`).
