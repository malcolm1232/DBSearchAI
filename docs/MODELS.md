# Models — where to get them, how to swap them, what to expect

DBSearch.AI is **model-pluggable by design** (LAW 9).
The product never depends on one vendor's model: every LLM job goes through an adapter,
and every adapter that speaks the OpenAI-compatible chat API can serve any model you can
run.
Swapping a model is a config change, never a code change.

## 1. The three model jobs

One deployment uses up to three models, each independently swappable:

| job | env var | default | what it does |
| --- | --- | --- | --- |
| chat / synthesis | `OLLAMA_CHAT_MODEL` | `llama3.2` (base), `llama3.1:8b` (eval rig) | NL2SQL generation, answer synthesis, extraction, cross-store planning, value disambiguation |
| embeddings | `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | document + schema retrieval vectors |
| hosted (optional) | edition config | — | Azure OpenAI / Anthropic / Groq adapters for managed editions |

## 2. Where to download models

**Ollama library** ([ollama.com/library](https://ollama.com/library)) — the default
runtime. `ollama pull <name>` fetches a quantized, ready-to-serve build:

```bash
ollama pull qwen2.5-coder:7b     # ~4.7 GB - strong at SQL generation
ollama pull qwen3:8b             # ~5.2 GB - strong all-rounder
ollama pull llama3.1:8b          # ~4.9 GB - the measured baseline
ollama pull nomic-embed-text     # ~274 MB - embeddings
```

**Hugging Face** ([huggingface.co/models](https://huggingface.co/models)) — the upstream
source for everything, including GGUF quantizations you can import into Ollama
(`ollama create` with a Modelfile) or serve directly with vLLM / llama.cpp / TGI.

Any server exposing an **OpenAI-compatible `/v1` endpoint** works — Ollama, vLLM,
llama.cpp server, TGI, LM Studio. The adapter only needs a `base_url` and a model name.

## 3. How to swap

**Docker self-host** — set the env var and restart; models auto-pull on first up:

```bash
OLLAMA_CHAT_MODEL=qwen2.5-coder:7b docker compose up -d
```

**Bare rig (the eval setup)**:

```bash
ollama pull qwen2.5-coder:7b
PYTHONPATH=src SELFHOST_BACKEND=memory-ollama-embed \
  OLLAMA_EMBED_MODEL=nomic-embed-text OLLAMA_CHAT_MODEL=qwen2.5-coder:7b \
  python3 -m uvicorn dbsearch.server.app:app --port 8080
```

**A different server than Ollama**: point the LLaMA adapter's `base_url` at any
OpenAI-compatible endpoint (vLLM at `http://gpu-box:8000/v1`, etc.). Nothing else changes.

## 4. Sizing ladder (what to expect from each rung)

Measured on the golden retrieval-efficacy packs (see
`FINDINGS_260803_what_breaks_and_why.md`): retrieval quality is model-light — a 274 MB
embedder reaches doc-MRR 1.00 — but **answer quality is chat-model-bound**.

| rung | models | RAM/VRAM | expectation |
| --- | --- | --- | --- |
| laptop demo | `qwen2.5:0.5b`, `llama3.2:1b` | ~1-2 GB | demo-grade; honest declines over wrong answers |
| workstation | `qwen2.5-coder:7b`, `qwen3:8b`, `llama3.1:8b` | ~6-10 GB | the measured baseline tier; the verify-everything architecture (below) is what makes this tier usable |
| self-host prod | `qwen3:32b`, `llama3.3:70b` | 24-48 GB GPU | the small-model failure classes largely disappear |
| managed in-tenant | Azure OpenAI in **your** subscription | n/a | frontier quality, data residency intact |

## 5. Why small models are viable here at all

The architecture assumes the model is unreliable and **verifies every model output
mechanically** before trusting it: SQL is validated against the visible schema; a
resolved value must be a verbatim member of the column's own values; a cross-store bind
must mechanically align; an extracted span must appear verbatim in its source chunk;
uncertainty always degrades to an honest decline, never a guess.
That is why the same codebase is acceptable on an 8B model and excellent on a hosted one
— the checks don't get weaker when the model gets stronger.

## 6. In-tenant vs external models (LAW 1)

Adapters carry an `in_tenant` capability flag.
A model whose endpoint runs inside your tenant (self-hosted Ollama/vLLM, Azure OpenAI in
your subscription) is allowed extra capabilities that send *values you already own* in a
prompt — e.g. the literal-disambiguation rung.
External endpoints (public APIs) never receive stored values, only metadata: schema
names, types, authored descriptions.
Default is **closed**: an adapter must declare in-tenancy explicitly.

## 7. Comparing models yourself

The eval harness measures any model per-item, three runs, against frozen packs:

```bash
./scripts/model_bakeoff.sh qwen2.5-coder:7b qwen3:8b
```

Each model gets a discarded warm run plus three scored runs per pack; results land in
`eval_results/runs/` stamped by model, and baselines key on the chat model, so
comparisons never collide.
Compare **per item**, never by aggregate score.
