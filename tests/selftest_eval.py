"""#42 — RAG eval harness logic, hermetic (no Ollama).

Proves the scoring + comparison logic is correct and non-vacuous: the pure metrics behave,
run_eval shares one retrieval across models and scores each independently, and it plugs into
a REAL permission-trimmed QueryService (so the eval can never score a doc a user can't see).

    python3 tests/selftest_eval.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIdentity, InMemoryIndex, InMemoryObjectStore,
    InMemoryQueue, PlainTextExtractor,
)
from dbsearch.connectors.upload import UploadConnector  # noqa: E402
from dbsearch.eval import (  # noqa: E402
    GoldenItem, abstained, hit_at_k, key_fact_recall, precision_at_k, run_eval,
)
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.query import QueryService  # noqa: E402


class FakeChunk:
    def __init__(self, doc, text):
        self.doc_external_id = doc
        self.text = text


class FakeRetriever:
    """Returns canned chunks per question — isolates run_eval logic from embedding behavior."""
    def __init__(self, mapping):
        self._m = mapping

    def retrieve(self, user, question):
        return self._m.get(question, [])


class FixedLlm:
    """A model that always returns the same canned answer (stand-in for a real LlmPort)."""
    def __init__(self, answer):
        self._a = answer

    def answer(self, question, context_chunks):
        return {"answer": self._a, "citations": []}


class MapLlm:
    """A model that returns a per-question canned answer (realistic stand-in for a LlmPort)."""
    def __init__(self, mapping):
        self._m = mapping

    def answer(self, question, context_chunks):
        return {"answer": self._m.get(question, ""), "citations": []}


def test_pure_metrics():
    assert precision_at_k(["a", "b", "c"], ["a", "c"], 3) == 2 / 3
    assert precision_at_k([], ["a"], 3) == 0.0
    assert precision_at_k(["x", "y"], ["a"], 5) == 0.0
    assert hit_at_k(["x", "a"], ["a"], 5) is True
    assert hit_at_k(["x", "y"], ["a"], 1) is False        # 'a' not in top-1
    assert key_fact_recall("BERT is bidirectional", ["bert", "bidirectional"]) == 1.0
    assert key_fact_recall("BERT only", ["bert", "bidirectional"]) == 0.5
    assert key_fact_recall("anything", []) == 1.0
    assert abstained("I couldn't find anything you have access to about that.") is True
    assert abstained("Project Falcon is a merger.") is False
    print("  PASS  pure metrics (precision@k, hit@k, key_fact_recall, abstained)")


def test_run_eval_compares_models():
    retr = FakeRetriever({
        "What is BERT?": [FakeChunk("bert", "BERT is a bidirectional transformer encoder.")],
        "What is the capital of Mars?": [],   # unanswerable / no retrieval
    })
    golden = [
        GoldenItem("What is BERT?", relevant_docs=["bert"],
                   key_facts=["bidirectional", "encoder"], answerable=True),
        GoldenItem("What is the capital of Mars?", relevant_docs=[],
                   key_facts=[], answerable=False),
    ]
    llms = {
        "good": MapLlm({
            "What is BERT?": "BERT is a bidirectional encoder.",
            "What is the capital of Mars?": "I couldn't find anything about that.",
        }),
        "bad": MapLlm({
            "What is BERT?": "BERT is a recurrent network.",
            "What is the capital of Mars?": "Mars's capital is Olympus City.",
        }),
    }
    rep = run_eval(retr, llms, golden, user="analyst", k=5)

    # retrieval (shared): only the answerable BERT Q is scored (Mars is unanswerable -> excluded).
    rs = rep.retrieval_summary()
    assert rs["n"] == 1, rs
    assert rs["hit_rate_at_k"] == 1.0, rs
    assert rs["mean_precision_at_k"] == 1.0, rs

    # good model: full correctness on BERT, abstains on Mars -> faithful on both
    good = rep.model_summary("good")
    assert good["key_fact_recall"] == 1.0, good
    assert good["faithfulness"] == 1.0, good
    assert good["abstention_on_unanswerable"] == 1.0, good

    # bad model: misses BERT facts AND hallucinates Mars -> faithful on neither
    bad = rep.model_summary("bad")
    assert bad["key_fact_recall"] == 0.0, bad
    assert bad["faithfulness"] == 0.0, bad
    assert bad["abstention_on_unanswerable"] == 0.0, bad
    print("  PASS  run_eval shares retrieval + scores models independently (good > bad)")


def test_run_eval_respects_permission_trim():
    """The eval retrieves through a REAL QueryService — it can never grade a doc the user can't see."""
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    index = InMemoryIndex(store)
    embedder = HashingEmbedding()
    q = "confidential falcon merger acquisition valuation deal team"
    conn = UploadConnector("t", "deal-falcon", "Falcon",
                           q.encode(), "text/plain", ["deal-team"], "")
    run_ingestion(conn, queue, store, PlainTextExtractor(), embedder, index)
    identity = InMemoryIdentity({"alice": ["deal-team"], "bob": ["all-staff"]})
    qs = QueryService(index, identity, embedder, FixedLlm("x"), store, tenant_id="t")
    golden = [GoldenItem(q, relevant_docs=["deal-falcon"], key_facts=["falcon"])]

    rep_alice = run_eval(qs, {"m": FixedLlm("Falcon is a merger.")}, golden, user="alice", k=5)
    rep_bob = run_eval(qs, {"m": FixedLlm("Falcon is a merger.")}, golden, user="bob", k=5)
    assert rep_alice.items[0].hit is True, "alice (deal-team) must retrieve Falcon (positive control)"
    assert rep_bob.items[0].hit is False, "LAW-2 BREACH: bob retrieved Falcon in the eval"
    print("  PASS  run_eval honors permission trim (alice hits Falcon, bob does not)")


def main():
    print("RAG eval-harness self-test (hermetic — no Ollama):")
    test_pure_metrics()
    test_run_eval_compares_models()
    test_run_eval_respects_permission_trim()
    print("\nALL EVAL TESTS PASSED — scoring is correct, model comparison is sound, trim is honored.")


if __name__ == "__main__":
    main()
