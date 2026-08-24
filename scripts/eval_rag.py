"""LIVE RAG eval (#42) — compare llama3.2:3b vs llama3.1:8b on the SAME golden Q&A over the
messy unstructured sample_corpus/ (arXiv papers, NIST frameworks, Gutenberg books).

Needs a local Ollama (nomic-embed-text + both chat models). Not in the hermetic suite —
its scoring logic is unit-tested in tests/selftest_eval.py.

    python3 scripts/eval_rag.py

What it does:
  1. Reads sample_corpus/, extracts text (pypdf for PDFs), splits each doc into passages,
     and indexes them in-memory with REAL nomic-embed-text embeddings (passage-level retrieval).
  2. Runs each golden question's retrieval ONCE (shared) through a permission-trimmed
     QueryService, then generates an answer with EACH model on the identical context.
  3. Prints retrieval precision/hit-rate + per-model key-fact recall, faithfulness, and
     abstention-on-unanswerable, side by side.

This is the "I measure value, not hype" artifact: bigger model, same retrieval — does answer
quality actually improve? The numbers say so, not the marketing.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.adapters.llama import LlamaEmbedding, LlamaLlm  # noqa: E402
from dbsearch.adapters.local import (  # noqa: E402
    InMemoryIdentity, InMemoryIndex, InMemoryObjectStore, InMemoryQueue, PlainTextExtractor,
)
from dbsearch.adapters.local.rich_extractor import LocalRichExtractor  # noqa: E402
from dbsearch.connectors.upload import UploadConnector  # noqa: E402
from dbsearch.eval import GoldenItem, run_eval  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.query import QueryService  # noqa: E402

OLLAMA = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
MODELS = ["llama3.2:3b", "llama3.1:8b"]
TENANT = "eval"
ACL = ["corpus"]
MAX_CHARS = 40_000   # cap per doc to keep the eval fast; golden facts are answerable from the opening
PASSAGE = 1_000
OVERLAP = 150

CORPUS_DOCS = {
    "attention": "attention-is-all-you-need.pdf",
    "bert": "bert.pdf",
    "rag": "rag-lewis-2020.pdf",
    "nist-csf": "nist-csf-2.0.pdf",
    "nist-ai-rmf": "nist-ai-rmf-100-1.pdf",
    "pride": "pride-and-prejudice.txt",
    "sherlock": "sherlock-holmes.txt",
}

GOLDEN = [
    GoldenItem("What model architecture does 'Attention Is All You Need' propose?",
               ["attention"], ["transformer", "attention"]),
    GoldenItem("What does BERT stand for?",
               ["bert"], ["bidirectional", "encoder", "transformer"]),
    GoldenItem("What is Retrieval-Augmented Generation?",
               ["rag"], ["retrieval", "generation"]),
    GoldenItem("What does the NIST Cybersecurity Framework help organizations manage?",
               ["nist-csf"], ["cybersecurity", "risk"]),
    GoldenItem("What is the goal of the NIST AI Risk Management Framework?",
               ["nist-ai-rmf"], ["risk"]),
    GoldenItem("In the famous opening of Pride and Prejudice, what must a single man with a good fortune be in want of?",
               ["pride"], ["wife"]),
    GoldenItem("Who is Sherlock Holmes's companion and the narrator of the stories?",
               ["sherlock"], ["watson"]),
    GoldenItem("What is DBSearch.AI's monthly subscription price?",
               [], [], answerable=False),   # not in the corpus -> a faithful model must abstain
]


def passages(text: str):
    text = text[:MAX_CHARS]
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + PASSAGE])
        i += PASSAGE - OVERLAP
    return out


class ParentRetriever:
    """Wraps a QueryService so retrieved passage ids (docid#pN) normalize to their parent doc
    for precision/hit scoring, while still exercising the real permission-trimmed retrieve()."""
    def __init__(self, qs):
        self._qs = qs

    def retrieve(self, user, question):
        chunks = self._qs.retrieve(user, question)
        for c in chunks:
            c.doc_external_id = c.doc_external_id.split("#", 1)[0]
        return chunks


def main():
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    embedder = LlamaEmbedding(OLLAMA, EMBED_MODEL)
    index = InMemoryIndex(store)
    rich, plain = LocalRichExtractor(), PlainTextExtractor()

    print(f"Indexing sample_corpus/ with {EMBED_MODEL} (passage-level)…")
    total = 0
    for docid, fname in CORPUS_DOCS.items():
        path = ROOT / "sample_corpus" / fname
        data = path.read_bytes()
        if fname.endswith(".pdf"):
            text = rich.extract(data, "application/pdf")
        else:
            text = data.decode("utf-8", errors="ignore")
        ps = passages(text)
        for i, p in enumerate(ps):
            conn = UploadConnector(TENANT, f"{docid}#p{i}", f"{docid} (p{i})",
                                   p.encode(), "text/plain", ACL, fname)
            run_ingestion(conn, queue, store, plain, embedder, index)
        total += len(ps)
        print(f"  {docid:13} {len(ps):3} passages")
    print(f"  total: {total} passages indexed\n")

    identity = InMemoryIdentity({"analyst": ["corpus"]})
    qs = QueryService(index, identity, embedder, LlamaLlm(OLLAMA, MODELS[0]), store, top_k=5)
    retriever = ParentRetriever(qs)
    llms = {m: LlamaLlm(OLLAMA, m) for m in MODELS}

    print("Running golden Q&A (retrieval shared, generation per model)…\n")
    rep = run_eval(retriever, llms, GOLDEN, user="analyst", k=5)

    # --- report ---
    print("=" * 78)
    print("RETRIEVAL (model-independent, nomic-embed-text, passage-level, top-5):")
    rs = rep.retrieval_summary()
    print(f"  mean precision@5 = {rs['mean_precision_at_k']}   hit-rate@5 = {rs['hit_rate_at_k']}   (n={rs['n']})")
    print("=" * 78)
    for m in MODELS:
        ms = rep.model_summary(m)
        print(f"GENERATION — {m}:")
        print(f"  key-fact recall = {ms['key_fact_recall']}   faithfulness = {ms['faithfulness']}   "
              f"abstention-on-unanswerable = {ms['abstention_on_unanswerable']}")
    print("=" * 78)
    print("\nPER-QUESTION:")
    for it in rep.items:
        print(f"\nQ: {it.question}")
        print(f"   retrieved={it.retrieved_ids[:5]}  precision@5={it.precision:.2f}  hit={it.hit}")
        for m in MODELS:
            pm = it.per_model[m]
            print(f"   [{m}] kfr={pm['key_fact_recall']:.2f} faithful={pm['faithful']} :: {pm['answer'].strip()[:160]}")


if __name__ == "__main__":
    main()
