"""#41 — LLaMA model-swap wiring (memory-ollama backend).

Proves the demo can keep the in-memory retrieval core but swap GENERATION to a local
Ollama model (via its OpenAI-compatible /v1 API), selectable by OLLAMA_CHAT_MODEL —
so the same corpus can be answered by llama3.2:3b OR llama3.1:8b with one env var, no
Docker/Postgres. Hermetic: LlamaLlm only constructs an OpenAI client at build time
(no network call), so this runs without a live Ollama.

    python3 tests/selftest_model_swap.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def build(model=None):
    os.environ["SELFHOST_BACKEND"] = "memory-ollama"
    os.environ.pop("OLLAMA_CHAT_MODEL", None)
    if model is not None:
        os.environ["OLLAMA_CHAT_MODEL"] = model
    # build_edition reads env at call time; re-import fresh each call.
    import importlib

    import dbsearch.server.edition as ed
    importlib.reload(ed)
    return ed.build_edition()


def main():
    print("Model-swap wiring self-test (memory-ollama: in-memory retrieval + Ollama generation):")

    # 1. In-memory retrieval is preserved (no Postgres, no real embeddings).
    e = build(model="llama3.1:8b")
    assert e.backend == "memory-ollama", e.backend
    assert type(e.index).__name__ == "InMemoryIndex", type(e.index).__name__
    assert type(e.embedder).__name__ == "HashingEmbedding", type(e.embedder).__name__
    print("  PASS  retrieval core stays in-memory (InMemoryIndex + HashingEmbedding)")

    # 2. Generation is the Ollama LlmPort, and the model is the one selected.
    llm = e.query_service._llm
    assert type(llm).__name__ == "LlamaLlm", type(llm).__name__
    assert llm._model == "llama3.1:8b", llm._model
    # conversation + proposal agent share the SAME llm instance (one swap point).
    assert e.conversation_service._llm is llm, "conversation must reuse the same llm"
    print(f"  PASS  generation swapped to LlamaLlm  ->  model={llm._model!r} (8b selected)")

    # 3. Switching the env var switches the model (3b) — the swap is one env var.
    e2 = build(model="llama3.2:3b")
    assert e2.query_service._llm._model == "llama3.2:3b", e2.query_service._llm._model
    print("  PASS  OLLAMA_CHAT_MODEL switches model  ->  llama3.2:3b")

    # 4. Default model when unset (the lightweight local default).
    e3 = build(model=None)
    assert e3.query_service._llm._model == "llama3.2:3b", e3.query_service._llm._model
    print("  PASS  default model = llama3.2:3b when OLLAMA_CHAT_MODEL unset")

    print("\nALL MODEL-SWAP TESTS PASSED — generation is a pluggable Ollama model; retrieval unchanged.")


if __name__ == "__main__":
    main()
