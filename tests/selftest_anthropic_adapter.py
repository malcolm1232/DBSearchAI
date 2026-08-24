"""#57 — AnthropicLlm adapter. Proves it composes the right prompts and parses Claude's
Messages-API response, WITHOUT any network or the `anthropic` SDK (a fake client is injected).

    python3 tests/selftest_anthropic_adapter.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.anthropic import (  # noqa: E402
    AnthropicLlm, _ANSWER_SYSTEM, _DRAFT_SYSTEM, _PLAN_SYSTEM, _SUMMARY_SYSTEM, _ELICIT_SYSTEM,
    _SETUP_SYSTEM, _SQL_SYSTEM,
)


class _Block:
    def __init__(self, text): self.text = text; self.type = "text"


class _Resp:
    def __init__(self, text): self.content = [_Block(text)]


class _Messages:
    def __init__(self): self.calls = []

    def create(self, *, model, max_tokens, temperature, system, messages):
        self.calls.append({"model": model, "system": system, "user": messages[0]["content"],
                           "max_tokens": max_tokens})
        if system == _PLAN_SYSTEM:                      # multi-line plan to exercise splitting
            return _Resp("sub one\nsub two\nsub three\nsub four")
        return _Resp(f"REPLY::{model}")


class _Client:
    def __init__(self): self.messages = _Messages()


def _mk(model="claude-haiku-x"):
    c = _Client()
    return AnthropicLlm("dummy-key", model, client=c), c


def test_answer_uses_answer_system_and_context():
    llm, c = _mk()
    out = llm.answer("what is the policy?", ["alpha", "beta"])
    assert out == {"answer": "REPLY::claude-haiku-x", "citations": []}, out
    call = c.messages.calls[-1]
    assert call["system"] == _ANSWER_SYSTEM
    assert "[1] alpha" in call["user"] and "[2] beta" in call["user"]
    # empty context -> abstain, no API call
    c.messages.calls.clear()
    assert "couldn't find" in llm.answer("q", [])["answer"]
    assert c.messages.calls == []
    print("  PASS  answer: ANSWER system + numbered context; empty ctx abstains w/o a call")


def test_context_numbers_only_evidence_not_instruction_lines():
    """#233: a folded-in [query]/[coverage] INSTRUCTION line must NOT get a citable [n], or the
    model can cite it and the prose carries a dangling footnote - observed live, the model cited
    [7] (the [query] line) when only 6 evidence passages existed. Evidence numbering must stay
    1..N in lockstep with the footnotes built from that same evidence."""
    llm, c = _mk()
    llm.answer("q", ["alpha", "[query] the evidence was produced by SELECT ...", "beta"])
    user = c.messages.calls[-1]["user"]
    assert "[1] alpha" in user and "[2] beta" in user, user     # evidence numbered sequentially
    assert "[3]" not in user, user                              # the instruction consumed NO number
    assert "[query] the evidence was produced" in user, user    # instruction still shown, own label
    print("  PASS  context: only evidence gets a citable [n]; instruction lines are un-numbered")


def test_plan_splits_lines_and_pads():
    llm, c = _mk()
    sections = ["A", "B", "C", "D"]
    plan = llm.plan_subquestions("a retail bank app", sections)
    assert plan == ["sub one", "sub two", "sub three", "sub four"], plan
    assert c.messages.calls[-1]["system"] == _PLAN_SYSTEM
    # if the model under-returns, it pads to one-per-section (never fewer)
    short = AnthropicLlm("k", "m", client=_Client())
    short._complete = lambda *a, **k: "only one"   # type: ignore
    assert len(short.plan_subquestions("brief", sections)) == 4
    print("  PASS  plan_subquestions: splits lines, pads to one-per-section")


def test_draft_and_gather_use_their_systems():
    llm, c = _mk("claude-sonnet-x")
    prose = llm.draft_section("Approach", "brief", ["past work chunk"])
    assert prose == "REPLY::claude-sonnet-x"
    assert c.messages.calls[-1]["system"] == _DRAFT_SYSTEM
    assert llm.draft_section("X", "b", []) == "No authorized source material found for this section."

    llm.elicit_requirements([{"question": "hi", "answer": ""}])
    assert c.messages.calls[-1]["system"] == _ELICIT_SYSTEM
    llm.summarize_requirements([{"question": "client is a bank", "answer": ""}])
    assert c.messages.calls[-1]["system"] == _SUMMARY_SYSTEM
    print("  PASS  draft_section / elicit / summarize each use their own system prompt")


def test_draft_section_stream_falls_back_without_stream_client():
    # the fake client has no .messages.stream -> adapter yields the whole section once (still works)
    llm, _ = _mk("claude-sonnet-x")
    out = list(llm.draft_section_stream("Approach", "brief", ["chunk a"]))
    assert out == ["REPLY::claude-sonnet-x"], out
    assert list(llm.draft_section_stream("X", "b", [])) == ["No authorized source material found for this section."]
    print("  PASS  draft_section_stream yields whole section when client can't stream")


def test_extract_setup_entries_uses_setup_system():
    # C3 (#116): the setup-entry extraction prompt — JSON contract + never-invent-ACL rule
    llm, c = _mk()
    out = llm.extract_setup_entries("plug in the folder at /mnt/contracts")
    assert out == "REPLY::claude-haiku-x", out
    call = c.messages.calls[-1]
    assert call["system"] == _SETUP_SYSTEM
    assert "plug in the folder at /mnt/contracts" in call["user"]
    assert "JSON" in _SETUP_SYSTEM and "acl" in _SETUP_SYSTEM
    assert "never" in _SETUP_SYSTEM.lower()               # the don't-invent-ACLs rule (LAW 2)
    print("  PASS  extract_setup_entries: SETUP system prompt, raw text returned")


def test_generate_sql_uses_sql_system_and_grounds_on_schema():
    # #135: schema-grounded NL2SQL. The model gets the visible schema + question and
    # returns raw SQL; safety (guard + fallback) lives in llm_sql_generator, not here.
    llm, c = _mk()
    schema = [{"table": "sales", "columns": [{"name": "region", "type": "TEXT"},
                                             {"name": "amount", "type": "INTEGER"}]}]
    out = llm.generate_sql("total by region", schema)
    assert out == "REPLY::claude-haiku-x", out
    call = c.messages.calls[-1]
    assert call["system"] == _SQL_SYSTEM
    assert "sales" in call["user"] and "region" in call["user"], call["user"]
    assert "total by region" in call["user"], call["user"]
    # the prompt must forbid non-SELECT and pin single-statement read-only
    assert "SELECT" in _SQL_SYSTEM and "read-only" in _SQL_SYSTEM.lower()
    print("  PASS  generate_sql: SQL system prompt, schema + question grounded")


def main():
    print("Anthropic adapter self-test (#57):")
    test_answer_uses_answer_system_and_context()
    test_context_numbers_only_evidence_not_instruction_lines()
    test_plan_splits_lines_and_pads()
    test_draft_and_gather_use_their_systems()
    test_draft_section_stream_falls_back_without_stream_client()
    test_extract_setup_entries_uses_setup_system()
    test_generate_sql_uses_sql_system_and_grounds_on_schema()
    print("\nALL ANTHROPIC ADAPTER TESTS PASSED — prompts routed, responses parsed, no network.")


if __name__ == "__main__":
    main()
