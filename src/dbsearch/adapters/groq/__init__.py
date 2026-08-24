"""Groq via its OpenAI-compatible API (LAW 9, pluggable model). Groq serves OPEN models
(Llama 3.x etc.) at very high token throughput — great for the fast gather chat and a snappy
demo.

Because Groq speaks the OpenAI Chat Completions API, GroqLlm subclasses LlamaLlm (so it inherits
answer / answer_stream / condense_question for free) and adds the proposal-planning, section-
drafting and gather overrides — reusing the SAME system prompts as the Anthropic adapter so the
behaviour is consistent whichever provider serves the model.

⚠ LAW 1: Groq is an external API — content leaves the tenant. DEV/DEMO only, same caveat as the
Anthropic public API (#58). Like every LlmPort it MUST only receive post-trim content (LAW 2).

Optional dep:  pip install '.[llama]'   (the openai client; Groq reuses it)
"""
from __future__ import annotations

from dbsearch.adapters.anthropic import (
    _COSMOS_SYSTEM, _DECOMPOSE_SYSTEM, _DRAFT_SYSTEM, _ELICIT_SYSTEM, _PLAN_SYSTEM,
    _SQL_SYSTEM,
    _SUMMARY_SYSTEM,
)
from dbsearch.adapters.llama import LlamaLlm

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Current Groq production models (Jan 2026): a strong versatile model + a fast small one.
GROQ_VERSATILE = "llama-3.3-70b-versatile"
GROQ_INSTANT = "llama-3.1-8b-instant"

# Curated default fleet — mirrors QuantifyMe's Groq set MINUS Kimi (kimi-k2 is gated to QM's
# key tier; it returns 404 on the DBSearch key). All verified callable on the DBSearch key.
# Override the whole list with the GROQ_MODELS env var. (Kimi: add "moonshotai/kimi-k2-instruct-0905"
# once it's provisioned on the key in use. Qwen2.5 is NOT on Groq — it's a local Ollama model.)
GROQ_DEFAULT_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3-32b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    GROQ_VERSATILE,
    GROQ_INSTANT,
]


class GroqLlm(LlamaLlm):
    #: Groq is a third-party cloud API. LlamaLlm claims in_tenant=True for self-hosted
    #: endpoints; inheriting that here would let the #462 values rung ship customer
    #: values off-tenant. Overridden back to the LlmPort default, explicitly (LAW 1).
    in_tenant = False

    def __init__(self, api_key: str, model: str = GROQ_VERSATILE,
                 base_url: str = GROQ_BASE_URL) -> None:
        super().__init__(base_url, model, api_key=api_key)

    # _chat and generate_sql now live on LlamaLlm (#461) — they only ever used
    # self._client/self._model, and keeping them here is what let the base class ship
    # without a SQL generator at all. Inherited verbatim; nothing about Groq changes.

    @staticmethod
    def _context(context_chunks: list[str]) -> str:
        # #233: number ONLY evidence passages; instruction lines ([coverage]/[query]) keep their
        # own label but consume no citable [n], so the model can never cite an instruction line and
        # leave a dangling footnote. See AnthropicLlm._context for the full rationale.
        capped = [c[: LlamaLlm._MAX_CHARS_PER_CHUNK] for c in context_chunks]
        out, n = [], 0
        for c in capped:
            if c.startswith("[coverage]") or c.startswith("[query]"):
                out.append(c)
            else:
                n += 1
                out.append(f"[{n}] {c}")
        return "\n\n".join(out)

    # --- proposal planning + drafting (mirrors AnthropicLlm) ---------------------------------
    def plan_subquestions(self, brief: str, sections: list[str]) -> list[str]:
        user = f"Brief: {brief}\n\nSections (in order):\n" + "\n".join(f"- {s}" for s in sections)
        out = self._chat(_PLAN_SYSTEM, user, max_tokens=512)
        lines = [ln.strip(" -•\t") for ln in out.splitlines() if ln.strip()]
        if len(lines) < len(sections):
            lines += [f"{s} for: {brief}" for s in sections[len(lines):]]
        return lines[: len(sections)]

    def draft_section(self, title: str, brief: str, context_chunks: list[str]) -> str:
        if not context_chunks:
            return "No authorized source material found for this section."
        user = f"Brief: {brief}\nSection: {title}\n\nContext passages:\n{self._context(context_chunks)}"
        return self._chat(_DRAFT_SYSTEM, user)

    def draft_section_stream(self, title: str, brief: str, context_chunks: list[str]):
        if not context_chunks:
            yield "No authorized source material found for this section."
            return
        user = f"Brief: {brief}\nSection: {title}\n\nContext passages:\n{self._context(context_chunks)}"
        stream = self._client.chat.completions.create(
            model=self._model, temperature=0, max_tokens=self._MAX_OUTPUT_TOKENS, stream=True,
            messages=[{"role": "system", "content": _DRAFT_SYSTEM}, {"role": "user", "content": user}],
        )
        for ch in stream:
            delta = ch.choices[0].delta.content if ch.choices else None
            if delta:
                yield delta

    # --- conversational gather (#57) ---------------------------------------------------------
    def elicit_requirements(self, history: list[dict]) -> str:
        if not history:
            return "Tell me about the proposal you'd like to draft — who's the client, and what do they need?"
        convo = "\n".join(f"User: {h.get('question','')}\nAssistant: {h.get('answer','')}" for h in history)
        return self._chat(_ELICIT_SYSTEM, convo, max_tokens=256)

    def summarize_requirements(self, history: list[dict]) -> str:
        convo = "\n".join(f"User: {h.get('question','')}" for h in history)
        return self._chat(_SUMMARY_SYSTEM, convo, max_tokens=512)

    # --- federated NL2SQL (#135) -------------------------------------------------------------
    def decompose_question(self, question: str) -> list:
        """#215: split a compound question so every half keeps the JOIN KEY (mirrors
        AnthropicLlm). The model only PROPOSES; `llm_decomposer` validates the shape and falls
        back to the deterministic split, so a bad generation can never lose half the question."""
        import json
        import re as _re

        raw = (self._chat(_DECOMPOSE_SYSTEM, question, max_tokens=400) or "").strip()
        if raw.startswith("```"):
            raw = _re.sub(r"^```[A-Za-z]*\s*", "", raw)
            raw = _re.sub(r"\s*```$", "", raw).strip()
        try:
            parts = json.loads(raw)
        except Exception:
            return []
        return parts if isinstance(parts, list) else []

    def generate_cosmos_query(self, question: str, schema: list) -> str:
        """Schema-grounded NL2query for Cosmos (#229; mirrors AnthropicLlm). The model only
        proposes the query - guard, CANNOT_ANSWER decline and keyword fallback live in
        `llm_cosmos_generator`. LAW 1: field names and types only, never a document value."""
        fields = ", ".join(f"{f['name']} ({f.get('type', '?')})"
                           for f in (schema[0]["fields"] if schema else []))
        container = schema[0].get("container", "c") if schema else "c"
        user = f"Container: {container}\nFields: {fields}\n\nQuestion: {question}"
        return self._complete(_COSMOS_SYSTEM, user, max_tokens=512)
