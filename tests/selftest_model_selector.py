"""#43 — model selector (pluggable generation models).

Proves: the optional `llm` override changes ONLY generation (retrieval/trim is byte-identical,
LAW 2), the conversation layer threads it, the Edition exposes a model registry + a safe
resolver, and /config advertises the models. Hermetic — no Ollama (uses a fake LlmPort).

    python3 tests/selftest_model_selector.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIdentity, InMemoryIndex, InMemoryObjectStore,
    InMemoryQueue, PlainTextExtractor,
)
from dbsearch.connectors.upload import UploadConnector  # noqa: E402
from dbsearch.query import QueryService  # noqa: E402
from dbsearch.query.conversation import ConversationService  # noqa: E402


class FakeLlm:
    def __init__(self, tag):
        self.tag = tag

    def answer(self, question, context_chunks):
        return {"answer": f"[{self.tag}] {len(context_chunks)} chunk(s)", "citations": []}

    def condense_question(self, question, history):
        return question


def _qs():
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    index = InMemoryIndex(store)
    embedder = HashingEmbedding()
    q = "confidential falcon merger acquisition valuation deal team"
    conn = UploadConnector("t", "deal-falcon", "Falcon", q.encode(), "text/plain", ["deal-team"], "")
    run = __import__("dbsearch.pipeline.runner", fromlist=["run_ingestion"]).run_ingestion
    run(conn, queue, store, PlainTextExtractor(), embedder, index)
    identity = InMemoryIdentity({"alice": ["deal-team"], "bob": ["all-staff"]})
    return QueryService(index, identity, embedder, FakeLlm("default"), store, tenant_id="t")


def test_llm_override_changes_only_generation():
    qs = _qs()
    q = "confidential falcon merger valuation"
    # the override model generates the answer...
    r = qs.answer("alice", q, llm=FakeLlm("MODEL-X"))
    assert r.answer.startswith("[MODEL-X]"), r.answer
    # ...but the permission trim is unaffected: bob still can't see the deal doc.
    rb = qs.answer("bob", q, llm=FakeLlm("MODEL-X"))
    assert "deal-falcon" in r.retrieved_docs and "deal-falcon" not in rb.retrieved_docs, (r.retrieved_docs, rb.retrieved_docs)
    # default path (no override) still works
    assert qs.answer("alice", q).answer.startswith("[default]")
    print("  PASS  llm override changes generation only; trim (LAW 2) unaffected")


def test_conversation_threads_model():
    qs = _qs()
    conv = ConversationService(qs, FakeLlm("conv-default"))
    r = conv.ask("alice", "c1", "confidential falcon valuation", llm=FakeLlm("MODEL-Y"))
    assert r.answer.startswith("[MODEL-Y]"), r.answer
    print("  PASS  ConversationService.ask threads the selected model")


def test_edition_registry_and_config():
    import importlib
    import dbsearch.server.edition as ed
    importlib.reload(ed)
    e = ed.build_edition()                       # memory backend
    assert e.chat_model_default in e.chat_models, e.chat_models
    # resolver: known -> that model; unknown/empty -> default (never errors)
    assert e.resolve_chat_llm(e.chat_model_default) is e.chat_models[e.chat_model_default]
    assert e.resolve_chat_llm("nope") is e.chat_models[e.chat_model_default]
    assert e.resolve_chat_llm("") is e.chat_models[e.chat_model_default]
    print(f"  PASS  Edition model registry + safe resolver (default={e.chat_model_default!r})")

    import dbsearch.server.app as app_mod
    importlib.reload(app_mod)
    cfg = TestClient(app_mod.app).get("/config").json()
    assert cfg["chat_model"] == e.chat_model_default, cfg
    assert isinstance(cfg["chat_models"], list) and cfg["chat_models"], cfg
    print(f"  PASS  /config advertises chat_models={cfg['chat_models']}")


def main():
    print("Model-selector self-test (#43, hermetic):")
    test_llm_override_changes_only_generation()
    test_conversation_threads_model()
    test_edition_registry_and_config()
    print("\nALL MODEL-SELECTOR TESTS PASSED — generation is pluggable; trim stays faithful.")


if __name__ == "__main__":
    main()
