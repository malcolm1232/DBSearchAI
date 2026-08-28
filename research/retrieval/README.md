# Retrieval efficacy research

The standing investigation into one question: **what is the most accurate way to
retrieve the correct information for a question?**

Start with [`retrieval_efficacy.ipynb`](retrieval_efficacy.ipynb).
It is read-only over committed artifacts and needs no server, no model, and no
network, so it opens and re-runs in about a second.

## Current answer

**Measured on real third-party data (#473), the scorer now passes 29 of 38 questions,
identical across three runs (`evidence/runs/real_pack_495{a,b,c}.json`, 260804).**
The first measurement that morning was 19/38 with **7 confidently wrong**; the 260803
handover tallied the wrong count at **1** after #476/#477/#479/#481, and #486/#491/#495
lifted the pass rate from there.

The confidently-wrong number is the one to care about, and no scorer reports it.
A confidently wrong answer is not a miss the user can detect - it reads exactly like a
correct one.

| capability | what it tests | first run (260803) | latest (495, 260804) |
| --- | --- | --- | --- |
| A | single-table lookup | 6/6 | 6/6 |
| B | aggregate | 4/6 | 5/6 |
| C | within-store join | 4/5 | 5/5 |
| D | **cross-store join** | **0/5** | **0/5** |
| E | value linking | 0/5 | 4/5 |
| F | wrong-vocab paraphrase | 0/5 | 3/5 |
| G | unanswerable, must decline | 5/6 | 6/6 |

Cross-store join is the one capability still at zero (#474; the one-hop case has since been
rescued by ADR 0014, not yet re-measured on this pack). In the first run **18 of the 19
failures reached the right store** - the gap is downstream of retrieval, so no embedder can
close it.

Three numbers from the same investigation are easy to conflate, so keep them apart:
**31/32 is routing** (the right store was reached, every run), **29/38 is SQL answers**, and
the **document rail** is a separate, gitignored pack (#487/#494): 120 private documents,
33 questions, 23/33 byte-identical across three warm runs (`evidence/runs/real_pack_docq_a/b/c`),
then 26/33 (`real_pack_cap_doc_a/b/warm`); zero fabricated answers (G 8/8), zero leaks across
29 restricted documents, and the right document ranked FIRST on 9 of the 10 misses
(`evidence/docq_fail_answers.json`). Retrieval is not the bottleneck on either rail.

Nothing has been re-measured after 260804, so every table here is a floor, not a current score.
The `verdict()` in `08_real_pack.py` predates the #476 decline wording, so re-run its
abstain markers before trusting its WRONG tally on a newer answers file.

Run [`analysis/08_real_pack.py`](analysis/08_real_pack.py) for the verbatim wrong answers.
The sharpest one: *"The average employee salary in the HR compensation database is
$3.11"* - there is no HR database, the router matched the **baseball** store on the shared
tokens `hr, salary` (hr is home runs), and the figure is `AVG(HR)` from a batting table.

Open cards from this run: **#474** no cross-store join · **#475** numeric literals quoted
as strings · **#476** an empty result set narrated as a factual zero · **#477** paraphrase
collapses the structured path · plus real-data evidence on **#462**, **#467** and **#457**.

### Why a second pack exists

Everything below this line was measured on `eval_fixtures/golden_pack`, which the same
model family wrote end to end - documents, questions **and** answers - and then sat.
`eval_fixtures/golden_pack_real` removes the model from the answer key: content is three
unrelated public Kaggle datasets, and every `key_fact` is produced by **executing** SQL on
an independent sqlite engine. A human writes the question and the query; the engine writes
the answer.

**That pack is not in this repository, and cannot be.** Its three sources - Olist
(`olistbr/brazilian-ecommerce`), MovieLens (`shubhammehta21/movie-lens-small-latest-dataset`)
and the Baseball Databank (`open-source-sports/baseball-databank`) - carry licences we may not
redistribute under: GroupLens forbids redistributing MovieLens outright, and Olist is
CC BY-NC-SA, which is NonCommercial. So `eval_fixtures/golden_pack_real/` is gitignored and
exists only on machines that built it. Download the three datasets yourself and run
`scripts/build_real_pack.py` to reproduce it; the numbers below are keyed to the pack content
hash, so a faithful rebuild compares directly and a different one refuses to.

The two packs never cross-compare: the pack content hash is part of the baseline key.

### Earlier answer (model-authored pack)

The bottleneck is **not the embedder**.
Measured on 53 shared golden items, replacing both stubs at once (a hash-based
non-embedder and a non-LLM) with `nomic-embed-text` + `llama3.1:8b` moved the
score 39.6% → 45.3%, and `synthesis-miss` barely moved: 27 → 25.

Honest failure profile once scorer artifacts are removed:

| cause | items | card |
| --- | --- | --- |
| text-to-SQL not wired + no value linking | 18 | #461, #462 |
| doc-side federation returns no answer | 6 | - |
| retrieval-miss | 3 | - |
| routing-miss | 1 | #457 |
| scorer artifact (not a real failure) | 2 | #463 |

An embedder bake-off competes over four of those items.
The structured-query path owns the rest.

### The three findings

**#461 - the self-host rig never runs a real SQL generator.**
`router_api.py:294` gates the LLM generator on `hasattr(llm, "generate_sql")`.
`LlamaLlm` does not implement it, so on any Ollama rig the gate returns `None`
and every SQL store silently falls back to `keyword_sql_generator`
(`router/structured.py:513`) - a regex stub whose own docstring calls it "loudly
not a real generator". It emitted `SELECT SUM(region)` over a text column, and
`SELECT * FROM marketing_spend LIMIT 5` for questions asking for a total.
The chat model only ever wrote prose over whatever rows that stub returned.

**#462 - the generator is shown column names, never value encodings.**
Every `WHERE` literal it writes is a blind guess. The question says "US" and
"paid search"; the rows say `us` and `paid-search`.

**#463 - two correct answers are scored as failures.**
`key_facts` uses word-anchored phrase matching and abstention uses a fixed marker
list, so a correct paraphrase fails. Until fixed, every pass rate here is
understated.

### Is it even achievable?

Yes - 18/18. See the ceiling test below. The gap is implementation, not
capability.

## What is here

| path | what it is |
| --- | --- |
| `retrieval_efficacy.ipynb` | the analysis. Read-only over `eval_results/`. Start here. |
| `build_retrieval_efficacy.py` | generates the notebook from reviewable source. Re-run, then `nbconvert --execute --inplace` to embed outputs. |
| `experiments/ceiling_sql.py` | ceiling test: is the `generate_sql` payload *sufficient*? Runs standalone, prints the A1/A2/B table. |

## What lives elsewhere (and must stay there)

These are production code and shared gates - referenced, not moved, because
other things import them.

| path | what it is |
| --- | --- |
| `eval_fixtures/golden_pack/` | the frozen corpus + 247 questions. Hash `45033e201713`. |
| `scripts/golden_runner.py` | the runner. Composes the pack, asks as alice/bob, scores. |
| `src/dbsearch/eval/golden/` | the scorers: `stage1` (retrieval), `stage2` (answer), `gate`, `scorecard`. |
| `eval_results/baselines/`, `eval_results/runs/` | the artifacts the notebook reads. |
| `2026-07-31-retrieval-efficacy-golden-suite-design.md` | the suite's design (not in the public tree, #685). |

## Reproducing a run

```bash
# semantic rig: real embeddings + real chat model
PYTHONPATH=src SELFHOST_BACKEND=memory-ollama-embed \
  OLLAMA_EMBED_MODEL=nomic-embed-text OLLAMA_CHAT_MODEL=llama3.1:8b \
  DBSEARCH_MAX_BODY_BYTES=5000000 DBSEARCH_RATE_LIMIT=0 \
  python3 -m uvicorn dbsearch.server.app:app --port 8099

# stratified subset (~56 items, minutes); add --full for all 247
PYTHONPATH=src python3 scripts/golden_runner.py \
  --base http://127.0.0.1:8099 --profile semantic --auth dev \
  --embedding nomic-embed-text --chat-model llama3.1:8b \
  --stamp semantic_subset_probe
```

The lexical baseline uses `SELFHOST_BACKEND=memory` and
`--profile hermetic-lexical`. Runs are keyed on
`{profile, embedding, chat_model, pack_hash}`, so artifacts from different
candidates never collide.

## Known limits

- **The corpus is model-authored.** Both the documents and the 247 questions were
  written by the same model family being tested. That circularity can compress
  the differences a bake-off is trying to measure. Replacing it with downloaded
  third-party data is the open next step.
- **A2 and B both score 100% only because this data is clean.** Defensive
  `LOWER()` would still break on "United States" vs `us`. Real data with messier
  encodings is what separates a prompt fix from real value linking.
- **Synthesis metrics are not bit-reproducible.** Retrieval is deterministic
  given a fixed embedder, but generation varies run to run even at temperature 0.
  Treat a 1-2 point synthesis difference as noise; a 1-point recall@5 difference
  is real.
- **The ceiling test proves reachability, not achievement.** It shows the payload
  is sufficient. It does not show a given model in-loop gets there.

## Order of work

1. Wire `generate_sql` onto `LlamaLlm` (#461), re-run the probe.
2. Add value linking to the generator payload (#462), re-run.
3. Then the embedder bake-off (#460), against a retrieval stage no longer masked
   by SQL failures.
