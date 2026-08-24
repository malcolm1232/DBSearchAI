"""#461 — LlamaLlm must expose generate_sql, or every self-host rig silently answers
structured questions with a regex.

`router_api._model_wiring` gates the LLM NL2SQL path on `hasattr(llm, "generate_sql")`.
LlamaLlm did not implement it, so on ANY Ollama / vLLM / TGI deployment the gate
returned None and every SQL store fell back to `keyword_sql_generator` — a stub whose
own docstring calls it "loudly not a real generator". Measured cost on the golden
suite: it emitted `SELECT SUM(region)` over a TEXT column and `SELECT * FROM t LIMIT 5`
for questions asking for a total, which accounted for 18 of 26 synthesis failures.

The Groq adapter has had this exact test since #135 (selftest_groq_adapter.py:79) and
its comment predicted this failure; LlamaLlm — the adapter self-hosters actually use —
never got one. This is that test.

No network: we bypass __init__ and inject a fake OpenAI-compatible client.

    python3 tests/selftest_llama_sql.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.anthropic import _SQL_SYSTEM  # noqa: E402
from dbsearch.adapters.llama import LlamaLlm  # noqa: E402


class _Msg:
    def __init__(self, content): self.content = content


class _Choice:
    def __init__(self, content): self.message = _Msg(content)


class _Resp:
    def __init__(self, content): self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, reply="SELECT SUM(amount) FROM marketing_spend"): self.calls = []; self._reply = reply

    def create(self, *, model, temperature, max_tokens, messages, stream=False):
        self.calls.append({"system": messages[0]["content"], "user": messages[1]["content"],
                           "temperature": temperature, "model": model})
        return _Resp(self._reply)


class _Chat:
    def __init__(self, comp): self.completions = comp


class _Client:
    def __init__(self, comp): self.chat = _Chat(comp)


def _mk(reply="SELECT SUM(amount) FROM marketing_spend", model="llama3.1:8b"):
    llm = LlamaLlm.__new__(LlamaLlm)          # bypass __init__ so no openai import/network
    comp = _Completions(reply)
    llm._client = _Client(comp)
    llm._model = model
    return llm, comp


SCHEMA = [{"table": "marketing_spend",
           "columns": [{"name": "region", "type": "TEXT"},
                       {"name": "channel", "type": "TEXT"},
                       {"name": "amount", "type": "INTEGER"}]}]


def test_generate_sql_exists():
    """The capability gate in router_api is a hasattr check — this IS the bug."""
    assert hasattr(LlamaLlm, "generate_sql"), (
        "LlamaLlm has no generate_sql: router_api._model_wiring's capability gate "
        "returns None and every SQL store falls back to keyword_sql_generator")
    print("  PASS  LlamaLlm exposes generate_sql (capability gate can fire)")


def test_generate_sql_uses_sql_system_and_grounds_on_schema():
    llm, comp = _mk()
    out = llm.generate_sql("What was the total marketing spend in the US on paid search?", SCHEMA)
    assert out == "SELECT SUM(amount) FROM marketing_spend", out
    call = comp.calls[-1]
    assert call["system"] == _SQL_SYSTEM, "must reuse the shared, hard-won SQL system prompt"
    assert "marketing_spend" in call["user"], call["user"]
    for col in ("region", "channel", "amount"):
        assert col in call["user"], f"{col} missing from schema payload: {call['user']}"
    assert "paid search" in call["user"], call["user"]
    assert call["temperature"] == 0, "NL2SQL must be deterministic (#254)"
    print("  PASS  generate_sql: shared SQL system prompt, schema + question grounded, temp 0")


def test_generate_sql_strips_markdown_fence():
    """Local models fence their output far more often than hosted ones. llm_sql_generator
    calls _strip_sql_fence on the way out, but the adapter must not add its own noise."""
    llm, _ = _mk(reply="```sql\nSELECT 1\n```")
    assert llm.generate_sql("q", SCHEMA) == "```sql\nSELECT 1\n```", (
        "adapter returns RAW model text; fence-stripping belongs to llm_sql_generator")
    print("  PASS  generate_sql returns raw text (guarding stays in llm_sql_generator)")


def test_router_capability_gate_now_selects_the_llm_generator():
    """End of the chain: the gate router_api uses must now pick the LLM path."""
    llm, _ = _mk()
    assert hasattr(llm, "generate_sql"), "gate would return None"
    from dbsearch.router import llm_sql_generator, memoized_sql_generator
    gen = memoized_sql_generator(llm_sql_generator(llm))
    assert gen("total spend", SCHEMA) == "SELECT SUM(amount) FROM marketing_spend", (
        "validate_sql rejects any table outside the visible schema and falls back")
    print("  PASS  llm_sql_generator wraps LlamaLlm end to end")


def test_schema_payload_carries_column_types():
    """#468: _SQL_SYSTEM tells the model 'You are given names and types', but the payload
    emitted names ONLY. Told it has type information it never received, a model cannot
    tell REAL from TEXT, and the defensible guess - cast everything to text before
    comparing - silently breaks every numeric filter:

        WHERE CAST(customer_id AS TEXT) = '29485'   ->  '29485.0' != '29485'  ->  no rows

    Measured with a context-free generator over the golden pack's 18 failing structured
    items: names only scored 11/18, names+types scored 18/18. All seven recovered items
    were exactly this cast. Types are METADATA, so including them is LAW 1 clean - unlike
    values, which are customer data (#462).
    """
    from dbsearch.adapters.anthropic import sql_user_prompt

    payload = sql_user_prompt("total sales for customer 29485", [
        {"table": "orders", "columns": [{"name": "customer_id", "type": "REAL"},
                                        {"name": "item", "type": "TEXT"}]}])
    assert "customer_id REAL" in payload, (
        f"column type missing from the payload the model actually receives:\n{payload}")
    assert "item TEXT" in payload, payload
    assert "orders(" in payload, payload
    print("  PASS  schema payload carries column TYPES, not just names (#468)")


def test_schema_payload_survives_a_typeless_column():
    """Connector-introspected schemas default to TEXT, but a hand-built or partial schema
    may omit `type` entirely. That must degrade to the old name-only rendering rather than
    emitting a dangling 'name ' with trailing space or raising."""
    from dbsearch.adapters.anthropic import sql_user_prompt

    payload = sql_user_prompt("q", [{"table": "t", "columns": [{"name": "a"},
                                                               {"name": "b", "type": "INTEGER"}]}])
    assert "t(a, b INTEGER)" in payload, payload
    print("  PASS  a column with no declared type degrades to its bare name")


def test_in_tenant_flags_fail_closed_across_the_adapter_family():
    """#462: the literal-resolution LLM rung sends stored VALUES in a prompt, which is
    legal ONLY when the prompt never leaves the tenant. The flag is a class-level
    capability, absent-means-no - and the inheritance trap is the point of this test:
    GroqLlm SUBCLASSES LlamaLlm, so without its own override it would inherit
    in_tenant=True and ship customer values to a third-party cloud API."""
    from dbsearch.adapters.groq import GroqLlm
    from dbsearch.ports.base import LlmPort

    assert getattr(LlmPort, "in_tenant", None) is False, "base must default closed"
    assert LlamaLlm.in_tenant is True, "self-host endpoint IS the tenant"
    assert GroqLlm.in_tenant is False, (
        "GroqLlm inherits LlamaLlm - without an override the values rung would leak")
    print("  PASS  in_tenant: base False, LlamaLlm True, GroqLlm overridden back to False")


def test_pick_value_asks_once_and_returns_the_models_reply():
    """#462: the disambiguation rung. The prompt must carry the user's wording and every
    candidate; the reply comes back raw (verbatim-member checking belongs to
    dictionary._llm_pick, same division of labour as generate_sql/llm_sql_generator)."""
    llm, comp = _mk(reply="D.R.")
    out = llm.pick_value("Dominican Republic", ["D.R.", "USA", "Curacao"])
    assert out == "D.R.", out
    call = comp.calls[-1]
    assert "Dominican Republic" in call["user"], call["user"]
    for candidate in ("D.R.", "USA", "Curacao"):
        assert candidate in call["user"], f"{candidate} missing: {call['user']}"
    assert call["temperature"] == 0, "resolution must be deterministic"
    assert len(comp.calls) == 1
    print("  PASS  pick_value: one deterministic call, wording + candidates in the prompt")


def test_plan_cross_store_shows_metadata_only_and_returns_raw():
    """#474: the cross-store planner capability. The prompt carries the question and the
    visible stores' ids/tables/columns - METADATA, the same LAW 1 class as the schema
    payload - and the reply comes back raw (the strict-JSON guard lives in
    llm_cross_store_planner, the usual division of labour)."""
    llm, comp = _mk(reply='{"filter": "f?", "measure": "m?"}')
    stores = [{"id": "crm", "title": "Customers",
               "tables": [{"table": "customers",
                           "columns": ["customer_id", "customer_state"]}]},
              {"id": "orders", "title": "Orders",
               "tables": [{"table": "order_items", "columns": ["order_id", "price"]}]}]
    out = llm.plan_cross_store("total item revenue from customers in RJ?", stores)
    assert out == '{"filter": "f?", "measure": "m?"}', out
    call = comp.calls[-1]
    for token in ("crm", "orders", "customers", "customer_state", "order_items", "price",
                  "total item revenue"):
        assert token in call["user"], f"{token} missing: {call['user']}"
    assert call["temperature"] == 0, "planning must be deterministic"
    print("  PASS  plan_cross_store: metadata-only prompt, raw reply, temp 0")


def test_extract_relevant_asks_once_and_returns_raw():
    """#493: the condensed-pass extraction capability. The prompt carries the question
    and the passage; the reply comes back raw - the verbatim verification and the
    NONE/discard logic belong to synthesizer._condensed_answer (the usual division)."""
    llm, comp = _mk(reply="Request for Letter of Guarantee")
    out = llm.extract_relevant("What can be requested from the homepage tools?",
                               "Homepage tools: clinic search and Request for Letter of "
                               "Guarantee.")
    assert out == "Request for Letter of Guarantee", out
    call = comp.calls[-1]
    assert "homepage tools" in call["user"], call["user"]
    assert "clinic search" in call["user"], call["user"]
    assert call["temperature"] == 0, "extraction must be deterministic"
    assert len(comp.calls) == 1
    print("  PASS  extract_relevant: one deterministic call, question + passage in prompt")


def test_a_think_block_is_stripped_from_every_chat_reply():
    """#496 measured live: qwen3's default thinking mode wraps replies in
    <think>...</think>, which poisoned every output path at once - the warm run scored
    6/38 and runs tripled in length. The strip lives in _chat so ALL capabilities
    (generate_sql, pick_value, plan_cross_store, extract_relevant, answer) are protected
    from any reasoning-mode model, not just qwen3."""
    llm, _ = _mk(reply="<think>\nthe user wants a sum, the column is amount\n</think>\n"
                       "SELECT SUM(amount) FROM marketing_spend")
    assert llm.generate_sql("total spend?", SCHEMA) == \
        "SELECT SUM(amount) FROM marketing_spend"
    llm2, _ = _mk(reply="<think>only musings, no answer</think>")
    assert llm2.generate_sql("q", SCHEMA) == "", "a think-only reply is an empty reply"
    llm3, _ = _mk(reply="no think block at all")
    assert llm3.generate_sql("q", SCHEMA) == "no think block at all"
    print("  PASS  <think> blocks stripped in _chat - every capability protected")


def main():
    print("LlamaLlm NL2SQL self-test (#461):")
    test_generate_sql_exists()
    test_generate_sql_uses_sql_system_and_grounds_on_schema()
    test_generate_sql_strips_markdown_fence()
    test_router_capability_gate_now_selects_the_llm_generator()
    test_schema_payload_carries_column_types()
    test_schema_payload_survives_a_typeless_column()
    test_in_tenant_flags_fail_closed_across_the_adapter_family()
    test_pick_value_asks_once_and_returns_the_models_reply()
    test_plan_cross_store_shows_metadata_only_and_returns_raw()
    test_extract_relevant_asks_once_and_returns_raw()
    test_a_think_block_is_stripped_from_every_chat_reply()
    print("ALL PASS")


if __name__ == "__main__":
    main()
