"""#62 — GroqLlm adapter (OpenAI-compatible). Proves the proposal/gather overrides compose the
right prompts and parse Groq's chat-completions response, with NO network and WITHOUT importing
the openai client (we bypass __init__ and inject a fake client).

    python3 tests/selftest_groq_adapter.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.anthropic import (  # noqa: E402
    _DRAFT_SYSTEM, _PLAN_SYSTEM, _SQL_SYSTEM, _SUMMARY_SYSTEM,
)
from dbsearch.adapters.groq import GroqLlm  # noqa: E402


class _Msg:
    def __init__(self, content): self.content = content


class _Delta:
    def __init__(self, content): self.content = content


class _Choice:
    def __init__(self, content, stream): self.message = _Msg(content); self.delta = _Delta(content)


class _Resp:
    def __init__(self, content, stream=False): self.choices = [_Choice(content, stream)]


class _Completions:
    def __init__(self): self.calls = []

    def create(self, *, model, temperature, max_tokens, messages, stream=False):
        self.calls.append({"system": messages[0]["content"], "user": messages[1]["content"], "stream": stream})
        if stream:
            return [_Resp("REPLY ", True), _Resp(model, True)]          # two delta chunks
        text = "sub one\nsub two\nsub three\nsub four" if messages[0]["content"] == _PLAN_SYSTEM else f"REPLY::{model}"
        return _Resp(text)


class _Client:
    def __init__(self): self.chat = type("C", (), {"completions": _Completions()})()


def _mk(model="llama-3.3-70b-versatile"):
    gl = object.__new__(GroqLlm)            # bypass __init__ (no openai import / no network)
    gl._client = _Client()
    gl._model = model
    return gl, gl._client.chat.completions


def test_plan_and_draft():
    gl, comp = _mk()
    plan = gl.plan_subquestions("a retail bank acquisition", ["A", "B", "C", "D"])
    assert plan == ["sub one", "sub two", "sub three", "sub four"], plan
    assert comp.calls[-1]["system"] == _PLAN_SYSTEM
    prose = gl.draft_section("Approach", "brief", ["past work chunk"])
    assert prose == "REPLY::llama-3.3-70b-versatile", prose
    assert comp.calls[-1]["system"] == _DRAFT_SYSTEM
    assert gl.draft_section("X", "b", []) == "No authorized source material found for this section."
    print("  PASS  plan_subquestions splits/pads; draft_section uses DRAFT system")


def test_stream_and_summary():
    gl, comp = _mk("llama-3.1-8b-instant")
    toks = list(gl.draft_section_stream("Approach", "brief", ["chunk"]))
    assert toks == ["REPLY ", "llama-3.1-8b-instant"], toks
    assert comp.calls[-1]["stream"] is True
    gl.summarize_requirements([{"question": "client is a bank", "answer": ""}])
    assert comp.calls[-1]["system"] == _SUMMARY_SYSTEM
    assert gl._chat("sys", "") == "", "empty user content -> no call"
    print("  PASS  draft_section_stream yields deltas; summarize uses SUMMARY system; empty guard")


def test_generate_sql_uses_sql_system_and_schema():
    # #135: Groq is the default fleet, so it must expose generate_sql for the LLM
    # NL2SQL path to actually fire (else stores silently stay on the keyword default).
    gl, comp = _mk()
    schema = [{"table": "sales", "columns": [{"name": "region", "type": "TEXT"},
                                             {"name": "amount", "type": "INTEGER"}]}]
    out = gl.generate_sql("total by region", schema)
    assert out == "REPLY::llama-3.3-70b-versatile", out
    call = comp.calls[-1]
    assert call["system"] == _SQL_SYSTEM
    assert "sales" in call["user"] and "region" in call["user"], call["user"]
    assert "total by region" in call["user"], call["user"]
    print("  PASS  generate_sql: SQL system prompt, schema + question grounded")


def main():
    print("Groq adapter self-test (#62):")
    test_plan_and_draft()
    test_stream_and_summary()
    test_generate_sql_uses_sql_system_and_schema()
    print("\nALL GROQ ADAPTER TESTS PASSED — OpenAI-compatible prompts routed, responses parsed, no network.")


if __name__ == "__main__":
    main()
