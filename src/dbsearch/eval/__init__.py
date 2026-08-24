"""RAG evaluation harness (#42) — measure retrieval quality and answer faithfulness, and
COMPARE generation models on the SAME golden Q&A.

Design:
  - Retrieval is MODEL-INDEPENDENT (it happens before generation), so it's run ONCE per
    question via a QueryService and SHARED across all models being compared.
  - Generation is per-model: each model's LlmPort.answer() is called on the identical
    retrieved context, so differences are purely the model's.

Metrics (all deterministic — no LLM-as-judge, so the harness itself is reproducible/testable):
  - precision@k / hit@k : retrieval — did we surface the doc(s) that actually answer the Q.
  - key_fact_recall      : answer CORRECTNESS — fraction of expected facts present in the answer.
  - abstention           : answer FAITHFULNESS on UNANSWERABLE Qs — a faithful model says it
                           doesn't have the info rather than hallucinating.

The pure functions here are unit-tested in tests/selftest_eval.py with a fake model and the
deterministic HashingEmbedding (no Ollama). scripts/eval_rag.py wires real Ollama models +
sample_corpus/ and prints the side-by-side report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

_ABSTAIN_MARKERS = (
    "i couldn't find", "i could not find", "don't have", "do not have",
    "not in the context", "no information", "i don't know", "i do not know",
    "not provided", "cannot find", "can't find", "isn't in", "is not in",
    # #218: the router's no-evidence answers now say WHY there is no answer instead of always
    # blaming permissions ("I couldn't find anything you have access to"). Those are still
    # ABSTENTIONS - the system declined to answer rather than guessing - so the metric must
    # recognise them, or making the product MORE honest silently scores as LESS faithful.
    # That is the trap this list sets: abstention is measured by WORDING, so any new decline
    # phrasing must be registered here. (Caught by selftest_router_eval when #218 landed.)
    "no data sources are connected",     # nothing composed yet
    "hold that kind of data",            # #211 decline: healthy store, no such data
    "could not complete that query",     # the store errored or timed out
    "matched no records",                # it ran, and nothing fits
    "still reading that source",         # #940: the crawl has not finished - not an emptiness
)


@dataclass
class GoldenItem:
    question: str
    relevant_docs: list[str]          # external_ids that should answer this Q ([] = unanswerable)
    key_facts: list[str] = field(default_factory=list)  # substrings a correct answer should contain
    answerable: bool = True           # if False, a faithful model must abstain


def precision_at_k(retrieved_ids: list[str], relevant: list[str], k: int) -> float:
    """Fraction of the top-k retrieved docs that are relevant. 0.0 if nothing retrieved."""
    topk = retrieved_ids[:k]
    if not topk:
        return 0.0
    rel = set(relevant)
    return sum(1 for d in topk if d in rel) / len(topk)


def hit_at_k(retrieved_ids: list[str], relevant: list[str], k: int) -> bool:
    """Did any relevant doc make the top-k (recall as a hit/miss)."""
    rel = set(relevant)
    return any(d in rel for d in retrieved_ids[:k])


def key_fact_recall(answer: str, key_facts: list[str]) -> float:
    """Fraction of expected key facts present (case-insensitive substring). 1.0 if none specified."""
    if not key_facts:
        return 1.0
    a = answer.lower()
    return sum(1 for f in key_facts if f.lower() in a) / len(key_facts)


def abstained(answer: str) -> bool:
    """Heuristic: did the model decline to answer (the LlmPort system prompt instructs this)."""
    a = answer.lower()
    return any(m in a for m in _ABSTAIN_MARKERS)


@dataclass
class ItemResult:
    question: str
    retrieved_ids: list[str]
    precision: float
    hit: bool
    answerable: bool
    per_model: dict[str, dict]        # model -> {"answer", "key_fact_recall", "abstained", "faithful"}


@dataclass
class EvalReport:
    k: int
    models: list[str]
    items: list[ItemResult]

    def retrieval_summary(self) -> dict:
        # Retrieval precision/recall is undefined for unanswerable Qs (no relevant doc exists),
        # so they're excluded — standard IR practice.
        scored = [i for i in self.items if i.answerable]
        n = len(scored) or 1
        return {
            "mean_precision_at_k": round(sum(i.precision for i in scored) / n, 3),
            "hit_rate_at_k": round(sum(1 for i in scored if i.hit) / n, 3),
            "n": len(scored),
        }

    def model_summary(self, model: str) -> dict:
        ans = [i for i in self.items if i.per_model[model].get("answerable", True)]
        unans = [i for i in self.items if not i.per_model[model].get("answerable", True)]
        cr = round(sum(i.per_model[model]["key_fact_recall"] for i in ans) / (len(ans) or 1), 3)
        # faithfulness = correct when answerable, abstained when not
        faithful = round(sum(1 for i in self.items if i.per_model[model]["faithful"]) / (len(self.items) or 1), 3)
        ab = round(sum(1 for i in unans if i.per_model[model]["abstained"]) / (len(unans) or 1), 3) if unans else None
        return {"key_fact_recall": cr, "faithfulness": faithful, "abstention_on_unanswerable": ab}


def run_eval(retriever, llms: dict, golden: list[GoldenItem], user: str, k: int = 5) -> EvalReport:
    """Run retrieval once per Q (shared, permission-trimmed via `retriever.retrieve`), then
    generate with each model in `llms` (name -> LlmPort) on the identical context.

    `retriever` is anything with `.retrieve(user, question) -> list[chunk]` where each chunk
    has `.doc_external_id` and `.text` (a QueryService satisfies this)."""
    items: list[ItemResult] = []
    for g in golden:
        chunks = retriever.retrieve(user, g.question)
        retrieved_ids = [c.doc_external_id for c in chunks]
        per_model: dict[str, dict] = {}
        for name, llm in llms.items():
            answer = llm.answer(g.question, [c.text for c in chunks])["answer"]
            kfr = key_fact_recall(answer, g.key_facts)
            ab = abstained(answer)
            faithful = (not ab and kfr >= 0.5) if g.answerable else ab
            per_model[name] = {
                "answer": answer, "key_fact_recall": round(kfr, 3),
                "abstained": ab, "faithful": faithful, "answerable": g.answerable,
            }
        items.append(ItemResult(
            question=g.question, retrieved_ids=retrieved_ids,
            precision=precision_at_k(retrieved_ids, g.relevant_docs, k),
            hit=hit_at_k(retrieved_ids, g.relevant_docs, k),
            answerable=g.answerable,
            per_model=per_model,
        ))
    return EvalReport(k=k, models=list(llms.keys()), items=items)
