"""#911 - the prompt-boundary cap must never truncate a LEGITIMATE per-document block.

THE DEFECT, as the owner met it on prod. Ask could not answer "what is the notice period
for resignation" over a corpus whose Letter Of Employment provably contains Clause 14.1
("one (1) month's prior written notice ... two (2) months' written notice"). Retrieval was
healthy: the fact chunk ranked in the FINAL FIVE handed to synthesis. The decline happened
one layer down, at the prompt boundary:

  - #257 merges all retrieved chunks of one document into ONE context block. Four Letter Of
    Employment chunks made a 4,661-char block, the fact at offset 3,001.
  - `cap_chunks_disclosed` then cut every context item to `_MAX_CHARS_PER_CHUNK = 1500`.
    The model NEVER SAW Clause 14.1, and its decline was honest.

The 1500 budget was written for SINGLE ingest chunks (#499: "ingest chunks at ~1200 < 1500,
so a firing means an upstream invariant broke"). #257 changed the unit under it to a
multi-chunk document block without touching the number - a legitimate block is up to
top_k * chunk chars, and the cap fired on ordinary retrieval, silently starving the model.
Measured on prod 260821 (probe A/E): capped, the duration is unstatable; uncapped, the same
model over the same blocks answers "1 month's prior written notice or 2 months' written
notice".

THE RULE this file pins: every LLM adapter's per-item prompt budget must hold the LARGEST
LEGITIMATE #257 block - derived here from the real chunker and retrieval constants, not
quoted - while the cap itself stays alive as a backstop for genuinely broken upstream items.

One assertion per clause of the fix, so either half regressing alone goes red:
  - the llama budget (prod's synthesizer) fits a max block;
  - the anthropic budget fits a max block;
  - the backstop still fires on an absurd item (deleting the cap outright is the WRONG fix);
  - end to end at the adapter seam: a #911-shaped block reaches the MODEL with the fact
    intact (red before the fix - the fact sat beyond char 1500 and was cut).
"""
from __future__ import annotations

import inspect

from dbsearch.adapters.anthropic import AnthropicLlm, cap_chunks_disclosed
from dbsearch.adapters.llama import LlamaLlm
from dbsearch.pipeline.runner import _DEFAULT_MAX_CHARS
from dbsearch.query.service import QueryService


def _max_legit_block() -> str:
    """The largest per-document context block ordinary retrieval can build: every one of the
    top_k retrieved chunks belongs to the same document, each chunk at the chunker's own
    maximum, joined exactly as `_context_and_citations` joins them."""
    top_k = inspect.signature(QueryService.__init__).parameters["top_k"].default
    return "\n\n".join("x" * _DEFAULT_MAX_CHARS for _ in range(top_k))


def test_llama_budget_holds_a_max_legitimate_doc_block():
    block = _max_legit_block()
    out = cap_chunks_disclosed([block], LlamaLlm._MAX_CHARS_PER_CHUNK)
    assert out == [block], (
        f"a legitimate #257 block of {len(block)} chars is truncated by the llama prompt "
        f"budget ({LlamaLlm._MAX_CHARS_PER_CHUNK}) - this is #911: retrieval succeeded and "
        "the model was starved anyway")


def test_anthropic_budget_holds_a_max_legitimate_doc_block():
    block = _max_legit_block()
    out = cap_chunks_disclosed([block], AnthropicLlm._MAX_CHARS_PER_CHUNK)
    assert out == [block], (
        f"a legitimate #257 block of {len(block)} chars is truncated by the anthropic "
        f"prompt budget ({AnthropicLlm._MAX_CHARS_PER_CHUNK})")


def test_backstop_still_fires_on_a_broken_item():
    """Control that fails the WRONG fix. The cap exists for upstream breakage (#498's
    200KB one-line JSON chunk is the family) and must survive the budget raise - an item
    no legitimate pipeline can produce is still cut, and still says so in the prompt."""
    broken = "y" * 50_000
    out = cap_chunks_disclosed([broken], LlamaLlm._MAX_CHARS_PER_CHUNK)
    assert out != [broken], "the backstop is gone - a broken 50K item sailed through whole"
    assert "TRUNCATED" in out[0], "a silent cut is exactly what LAW 8 forbids"


def test_the_911_block_reaches_the_model_with_the_fact_intact():
    """The #911 shape end to end at the adapter seam: four chunk-sized passages merged into
    one document block, the fact in the THIRD (beyond char 1500), through the REAL
    `LlamaLlm.answer` prompt assembly with the client stubbed at the wire."""
    fact = "by either party giving to the other one (1) month's prior written notice"
    chunks = ["a" * 1055, "b" * 1200, (fact + " ").ljust(1200, "c"), "d" * 1200]
    block = "\n\n".join(chunks)
    assert block.index(fact) > 1500, "fixture rot: the fact no longer sits past the old cap"

    sent = {}

    class _Completions:
        @staticmethod
        def create(**kwargs):
            sent["messages"] = kwargs["messages"]

            class _Msg:
                content = "stub"

            class _Choice:
                message = _Msg()

            class _Resp:
                choices = [_Choice()]

            return _Resp()

    class _Client:
        class chat:
            completions = _Completions()

    llm = LlamaLlm.__new__(LlamaLlm)
    llm._client = _Client()
    llm._model = "stub"
    llm.answer("what is the notice period for resignation", [block])
    prompt = sent["messages"][1]["content"]
    assert fact in prompt, (
        "the fact chunk was retrieved, ranked, merged into the document block - and then "
        "cut at the prompt boundary before the model ever saw it (#911)")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("PASS", name)
