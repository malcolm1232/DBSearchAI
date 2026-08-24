"""LLaMA (or any open-weight model) via an OpenAI-compatible endpoint.

Implements LlmPort + EmbeddingPort (LAW 9, pluggable model). Works with vLLM, Ollama,
or TGI serving Llama/Mistral/etc., all of which expose an OpenAI-compatible /v1 API.

This is the in-tenant / air-gapped model option: point `base_url` at a model server
running INSIDE the customer's VNet, and NO content ever leaves the tenant (LAW 1) —
no Azure OpenAI, no external API. Same ports, so it's a drop-in for the Azure models.

Optional dep: pip install '.[llama]'   (just the openai client, pointed locally)
Example base_url: http://llm.internal:8000/v1   model: "meta-llama/Llama-3.1-8B-Instruct"
"""
from __future__ import annotations

import re

from dbsearch.ports.base import EmbeddingPort, LlmPort

from dbsearch.ports.prompts import ANSWER_SYSTEM as _SYSTEM  # noqa: E402  (#403)
from dbsearch.core.copy import NO_EVIDENCE_ANSWER


class LlamaLlm(LlmPort):
    from dbsearch.ports.prompts import CONDENSE_SYSTEM as _CONDENSE

    #: `base_url` points at a model server the customer runs themselves (Ollama, vLLM,
    #: TGI) - prompts never leave the tenant, so the #462 values rung may use it.
    in_tenant = True

    def __init__(self, base_url: str, model: str, api_key: str = "not-needed") -> None:
        from openai import OpenAI

        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self._model = model

    # Defensive caps so a giant/legacy chunk can't blow up the prompt or runtime (#49).
    # #911: the per-item prompt budget must hold a LEGITIMATE #257 per-DOCUMENT block -
    # up to top_k (5) ingest chunks of CHUNK_MAX_CHARS (1200) joined - not just one chunk.
    # At the old 1500 the cap fired on ordinary retrieval and cut Clause 14.1 out of a
    # 4,661-char Letter Of Employment block AFTER it had ranked into the final five, so the
    # model declined over a corpus that held the answer. 8000 fits the largest legitimate
    # block (~6.1K) with headroom while staying a real backstop for broken upstream items
    # (#498's 200KB one-line chunk is the family). Pinned by selftest_911.
    _MAX_CHARS_PER_CHUNK = 8000
    _MAX_OUTPUT_TOKENS = 512

    def _chat(self, system: str, user: str, *, max_tokens: int = 1024,
              temperature: float = 0.0) -> str:
        """One-shot system+user completion over the OpenAI-compatible endpoint.

        Lifted here from GroqLlm (#461) so both share it: it only ever touches
        `self._client` and `self._model`, so it was never Groq-specific, and keeping it
        in the subclass is what left the base class unable to grow `generate_sql`.
        """
        if not (user or "").strip():
            return ""
        resp = self._client.chat.completions.create(
            model=self._model, temperature=temperature, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        text = (resp.choices[0].message.content or "").strip()
        # #496 measured live: a reasoning-mode model (qwen3's default) wraps replies in
        # <think>...</think>, which poisoned EVERY capability at once - the golden warm
        # run scored 6/38 and runs tripled in length. Stripped here, at the one seam all
        # capabilities share, so any reasoning-mode model works out of the box.
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        return text

    def generate_sql(self, question: str, schema: list, dialect: str = "") -> str:
        """Schema-grounded NL2SQL for a self-hosted model (#461).

        WITHOUT this method `router_api._model_wiring`'s capability gate
        (`hasattr(llm, "generate_sql")`) returns None and every SQL store silently keeps
        `keyword_sql_generator` — a regex stub that answered "total marketing spend in
        the US on paid search" with `SELECT SUM(region) FROM marketing_spend`, summing a
        TEXT column with no filter. That failure is invisible: the store still returns
        rows and the chat model still writes confident prose over them. On the golden
        suite it accounted for 18 of 26 synthesis failures.

        This is also the LAW 1 path that matters most — a self-hosted model means real
        NL2SQL with nothing leaving the tenant. The model only PROPOSES SQL; the
        read-only guard, the CANNOT_ANSWER decline and the keyword fallback all live in
        `llm_sql_generator`, so raw text is exactly what this must return.
        """
        from dbsearch.adapters.anthropic import _SQL_SYSTEM, sql_user_prompt

        return self._chat(_SQL_SYSTEM, sql_user_prompt(question, schema, dialect),
                          max_tokens=512)

    _PICK_VALUE_SYSTEM = (
        "You map a user's wording onto ONE stored database value. Reply with exactly one "
        "value from the candidate list, character for character, or NONE if no candidate "
        "means the same thing. No explanation, no punctuation, no quotes.")

    def pick_value(self, written: str, candidates: list) -> str:
        """#462, ADR 0015 amendment: the literal-resolution disambiguation rung.

        Embeddings provably cannot resolve abbreviations (nomic-embed-text ranks Curacao
        above `D.R.` for "dominican republic"); a language model can. The candidates are
        stored VALUES - customer data in a prompt - which is legal here and only here
        because this adapter is in-tenant (`in_tenant = True`, gated again in
        `dictionary._llm_pick`, absent-means-no). The reply is returned raw: the
        verbatim-member check and the decline-on-anything-else both belong to the
        caller, same division of labour as generate_sql/llm_sql_generator."""
        listing = "\n".join(f"- {c}" for c in candidates)
        return self._chat(self._PICK_VALUE_SYSTEM,
                          f"User's wording: {written}\nCandidates:\n{listing}",
                          max_tokens=64)

    _PLAN_SYSTEM = (
        "You decide whether ONE question needs data from TWO different stores: a FILTER "
        "condition whose column lives in one store, and a MEASURE whose column lives in "
        "another. If so, reply with STRICT JSON on one line - the questions are plain "
        "ENGLISH, never SQL:\n"
        '{"filter_store": "<store id>", "filter": "...", '
        '"measure_store": "<store id>", "measure": "..."}\n'
        "Rules:\n"
        "- Find the SHARED KEY: a column name printed in the column list of BOTH stores. "
        "Read the two lists and pick a name that literally appears in each. The filter "
        "question asks to list that key column's values for rows matching the filter. "
        "Never list the filter column itself.\n"
        "- If the filter condition and the shared key sit in DIFFERENT tables of the "
        "filter store, that is fine - the filter question may combine that store's own "
        "tables to reach the key. What it must never do is reach into the other store.\n"
        "- The measure question computes the measure restricted to those key values, and "
        "names the key column.\n"
        "- filter_store / measure_store are ids copied exactly from the store list, and "
        "must be the stores whose tables hold the filter and measure columns.\n"
        "- Check the measure store before you answer: its own column list must contain "
        "the shared key AND the column being measured. If it contains neither, it is the "
        "wrong store - pick the one that does.\n"
        "- No SQL keywords (SELECT, SUM, WHERE, JOIN) anywhere.\n"
        "Example 1, the key sits beside the filter - stores crm: "
        "customers(customer_id, customer_state) and sales: orders(order_id, "
        "customer_id), order_items(order_id, price); question 'What is the total item "
        "price from customers in state XX?' ->\n"
        '{"filter_store": "crm", "filter": "List the distinct customer_id values where '
        'customer_state is XX. One column only.", "measure_store": "sales", '
        '"measure": "What is the total order_items price for those customer_id '
        'values?"}\n'
        "Example 2, the key is one table away and a third store is a decoy - stores ref: "
        "items(item_id, kind_code), kinds(kind_code, kind_label); sales: lines(line_id, "
        "item_id, amount); media: plays(play_id, track_id, score); question 'How many "
        "lines were for items of kind LABEL?'. media is rejected because it shares NO "
        "column name with ref, however much its subject sounds related - only sales "
        "does. kind_label is in ref but item_id is the only name in BOTH lists, so the "
        "filter question walks kinds to items inside ref ->\n"
        '{"filter_store": "ref", "filter": "List the distinct item_id values whose '
        'kind_label is LABEL. One column only.", "measure_store": "sales", '
        '"measure": "How many lines were for those item_id values?"}\n'
        "Always phrase the filter question exactly in that style: 'List the distinct "
        "<key column> values where <filter>. One column only.'\n"
        "If the question can be answered inside one store - or you are not sure - reply "
        "with exactly: SINGLE. No explanation, no markdown.")

    def plan_cross_store(self, question: str, stores: list) -> str:
        """#474 (ADR 0014-B): propose a filter-half/measure-half split for a question
        whose columns span two stores. `stores` is caller-visible METADATA - ids, table
        and column names, the same LAW 1 class as the NL2SQL schema payload; no value is
        ever present. The reply is returned raw: strict-JSON parsing, the SINGLE
        fallthrough and every guard live in `llm_cross_store_planner`."""
        lines = []
        for s in stores:
            tables = "; ".join(
                f"{t.get('table')}({', '.join(t.get('columns', []))})"
                for t in s.get("tables", []))
            lines.append(f"- store {s.get('id')} ({s.get('title', '')}): {tables}")
        return self._chat(self._PLAN_SYSTEM,
                          f"Question: {question}\nStores:\n" + "\n".join(lines),
                          max_tokens=200)

    _EXTRACT_SYSTEM = (
        "You copy text, you never write it. From the passage, copy WORD FOR WORD the "
        "sentence or line fragments that help answer the question - one span per line, "
        "exactly as they appear, no rewording, no commentary, no quotes around them. "
        "If nothing in the passage helps, reply with exactly: NONE")

    def extract_relevant(self, question: str, chunk: str) -> str:
        """#493: the condensed-pass extraction capability (findings s15 - the model
        answers from the single fact-bearing chunk and drowns at five). Extraction is
        an easier task than QA for a small model, and its output is mechanically
        verifiable: the synthesizer discards any span the source chunk does not contain
        verbatim, so a hallucinated extract can never reach the second pass. Raw reply;
        NONE handling and verification live in synthesizer._condensed_answer."""
        return self._chat(self._EXTRACT_SYSTEM,
                          f"Question: {question}\nPassage:\n{chunk}", max_tokens=300)

    def answer(self, question: str, context_chunks: list[str]) -> dict:
        if not context_chunks:
            return {"answer": NO_EVIDENCE_ANSWER, "citations": []}
        from dbsearch.adapters.anthropic import cap_chunks_disclosed
        capped = cap_chunks_disclosed(context_chunks, self._MAX_CHARS_PER_CHUNK)
        context = "\n\n".join(f"[{i + 1}] {c}" for i, c in enumerate(capped))
        resp = self._client.chat.completions.create(
            model=self._model,
            temperature=0,
            max_tokens=self._MAX_OUTPUT_TOKENS,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Question: {question}\n\nContext:\n{context}"},
            ],
        )
        return {"answer": resp.choices[0].message.content, "citations": []}

    def answer_stream(self, question: str, context_chunks: list[str]):
        if not context_chunks:
            yield NO_EVIDENCE_ANSWER
            return
        from dbsearch.adapters.anthropic import cap_chunks_disclosed
        capped = cap_chunks_disclosed(context_chunks, self._MAX_CHARS_PER_CHUNK)
        context = "\n\n".join(f"[{i + 1}] {c}" for i, c in enumerate(capped))
        stream = self._client.chat.completions.create(
            model=self._model,
            temperature=0,
            max_tokens=self._MAX_OUTPUT_TOKENS,
            stream=True,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Question: {question}\n\nContext:\n{context}"},
            ],
        )
        for ch in stream:
            delta = ch.choices[0].delta.content if ch.choices else None
            if delta:
                yield delta

    def condense_question(self, question: str, history: list[dict]) -> str:
        if not history:
            return question
        convo = "\n".join(f"User: {h['question']}\nAssistant: {h['answer']}" for h in history)
        resp = self._client.chat.completions.create(
            model=self._model,
            temperature=0,
            messages=[
                {"role": "system", "content": self._CONDENSE},
                {"role": "user", "content": f"{convo}\n\nLast message: {question}"},
            ],
        )
        return (resp.choices[0].message.content or question).strip()


class LlamaEmbedding(EmbeddingPort):
    def __init__(self, base_url: str, model: str, api_key: str = "not-needed") -> None:
        from openai import OpenAI

        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=self._model, input=texts)
        return [item.embedding for item in resp.data]
