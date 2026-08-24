"""Azure OpenAI adapters — EmbeddingPort + LlmPort (LAW 9, pluggable).

Requires: pip install openai
The LLM is instructed to answer ONLY from the supplied context, which the QueryService
has ALREADY permission-trimmed (LAW 2) — the model never sees unauthorized content.
"""
from __future__ import annotations

from dbsearch.ports.base import EmbeddingPort, LlmPort
from dbsearch.core.copy import NO_EVIDENCE_ANSWER

_API_VERSION = "2024-10-21"


class AzureOpenAIEmbedding(EmbeddingPort):
    def __init__(self, endpoint: str, api_key: str, deployment: str, api_version: str = _API_VERSION) -> None:
        from openai import AzureOpenAI

        self._client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
        self._deployment = deployment

    def embed(self, texts: list[str]) -> list[list[float]]:
        import time

        from openai import RateLimitError

        # AOAI embedding deployments have a TPM rate limit; on 429 respect Retry-After and back
        # off instead of failing the whole ingest (a large crawl bursts well past a low tier).
        for attempt in range(8):
            try:
                resp = self._client.embeddings.create(model=self._deployment, input=texts)
                return [item.embedding for item in resp.data]
            except RateLimitError as e:
                if attempt == 7:
                    raise
                retry_after = getattr(getattr(e, "response", None), "headers", {}) or {}
                wait = int(retry_after.get("retry-after", 0)) or min(30, 3 * (attempt + 1))
                time.sleep(wait)
        return []  # unreachable


class AzureOpenAILlm(LlmPort):
    #: The deployment lives in the CUSTOMER's Azure subscription (the data plane, LAW 1
    #: - "LLM inference runs in the customer's tenant"), so prompts stay in-tenant and
    #: the #462 values rung may use it.
    in_tenant = True

    from dbsearch.ports.prompts import ANSWER_SYSTEM as _SYSTEM

    from dbsearch.ports.prompts import CONDENSE_SYSTEM as _CONDENSE

    def __init__(self, endpoint: str, api_key: str, deployment: str, api_version: str = _API_VERSION) -> None:
        from openai import AzureOpenAI

        self._client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
        self._deployment = deployment

    def answer(self, question: str, context_chunks: list[str]) -> dict:
        if not context_chunks:
            return {"answer": NO_EVIDENCE_ANSWER, "citations": []}
        context = "\n\n".join(f"[{i + 1}] {c}" for i, c in enumerate(context_chunks))
        resp = self._client.chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "system", "content": self._SYSTEM},
                {"role": "user", "content": f"Question: {question}\n\nContext:\n{context}"},
            ],
        )
        return {"answer": resp.choices[0].message.content, "citations": []}

    def condense_question(self, question: str, history: list[dict]) -> str:
        if not history:
            return question
        convo = "\n".join(f"User: {h['question']}\nAssistant: {h['answer']}" for h in history)
        resp = self._client.chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "system", "content": self._CONDENSE},
                {"role": "user", "content": f"{convo}\n\nLast message: {question}"},
            ],
        )
        return (resp.choices[0].message.content or question).strip()
