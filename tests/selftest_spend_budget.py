"""Groq demo spend budget self-test (#342).

Why this exists. Groq offers no per-key hard spend cap, so the only cap available is the
one in this app, and dbsearch.ai is about to be public with the LIVE key in prod.env.
#332's rate limiter bounds THROUGHPUT (per-IP 10/min, global 60/min); it does not bound
CUMULATIVE spend - 60/min sustained is ~86k requests a day. This budget bounds the money.

Two layers, deliberately:

  1. The metering client is the GUARANTEE. Every paid Groq call in the codebase goes
     through `self._client.chat.completions.create` (LlamaLlm builds it, GroqLlm inherits
     it), so metering there covers answer, answer_stream, condense_question, decompose,
     plan/draft, elicit/summarize and anything added later, with no per-method wiring to
     forget. When the budget is gone it RAISES rather than calling out.

  2. BudgetedLlm is the UX. It routes to the free local Extractive model once the budget
     is spent, so an exhausted demo still answers instead of erroring. Layer 1 is what
     stops the spend; layer 2 is what keeps the site usable.

The counter is persisted, because a budget that resets on every container recreate is not
a budget. State lives on the `objects:/data` volume in prod.

    python3 tests/selftest_spend_budget.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.server.spend_budget import (  # noqa: E402
    BudgetExhausted, BudgetedLlm, SpendBudget, meter_client,
)


class _FakeClock:
    """A clock the test drives, so day-rollover is tested without waiting a day."""

    def __init__(self, day="2026-07-27"):
        self.day = day

    def __call__(self):
        return self.day


class _FakeUsage:
    def __init__(self, total):
        self.total_tokens = total


class _FakeResp:
    def __init__(self, total=None):
        self.usage = _FakeUsage(total) if total is not None else None


class _FakeCompletions:
    """Stands in for openai's client.chat.completions."""

    def __init__(self, usage_total=100):
        self.calls = 0
        self.usage_total = usage_total

    def create(self, **kw):
        self.calls += 1
        return _FakeResp(self.usage_total)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeClient:
    def __init__(self, usage_total=100):
        self.completions = _FakeCompletions(usage_total)
        self.chat = _FakeChat(self.completions)


class _FakeLlm:
    """Minimal stand-in for GroqLlm / ExtractiveLlm."""

    def __init__(self, name):
        self.name = name
        self.seen = []

    def answer(self, question, context_chunks):
        self.seen.append(question)
        return {"text": f"{self.name}:{question}"}

    def draft_section(self, title, brief, context_chunks):
        return f"{self.name}:{title}"


def main():
    print("Groq demo spend budget self-test (#342):")
    tmp = Path(tempfile.mkdtemp())

    # --- a fresh budget starts whole ---
    clock = _FakeClock()
    b = SpendBudget(limit=1000, state_path=tmp / "spend.json", day_fn=clock)
    assert b.remaining() == 1000, b.remaining()
    assert not b.exhausted()
    print("  PASS  a fresh budget starts at its full limit")

    # --- spending draws it down and it does not go negative ---
    b.spend(400)
    assert b.remaining() == 600, b.remaining()
    b.spend(900)
    assert b.remaining() == 0, b.remaining()
    assert b.exhausted()
    print("  PASS  spending draws down, clamps at zero, and marks exhausted")

    # --- persistence: a NEW instance on the same path sees the same spend ---
    b2 = SpendBudget(limit=1000, state_path=tmp / "spend.json", day_fn=clock)
    assert b2.exhausted(), "budget reset itself on reload - a container recreate would refill it"
    assert b2.remaining() == 0, b2.remaining()
    print("  PASS  the counter survives a restart (persisted, not in-memory only)")

    # --- a new day refills it ---
    clock.day = "2026-07-28"
    assert not b2.exhausted(), "budget did not roll over to the new day"
    assert b2.remaining() == 1000, b2.remaining()
    print("  PASS  the budget refills on the next day")

    # --- corrupt state must fail CLOSED, not hand out a free day ---
    bad = tmp / "corrupt.json"
    bad.write_text("{not json", encoding="utf-8")
    b3 = SpendBudget(limit=1000, state_path=bad, day_fn=clock)
    assert b3.exhausted(), "corrupt state was read as a fresh budget"
    print("  PASS  unreadable state fails closed (no free budget from a corrupt file)")

    # --- the metering client charges REAL token usage and passes the response through ---
    clock2 = _FakeClock()
    b4 = SpendBudget(limit=200, state_path=tmp / "m.json", day_fn=clock2)
    client = _FakeClient(usage_total=100)
    meter_client(client, b4)
    client.chat.completions.create(model="x", messages=[])
    assert b4.remaining() == 100, b4.remaining()
    client.chat.completions.create(model="x", messages=[])
    assert b4.remaining() == 0, b4.remaining()
    print("  PASS  the metering client charges actual token usage")

    # --- exhausted: the call must NOT reach the provider ---
    before = client.completions.calls
    try:
        client.chat.completions.create(model="x", messages=[])
        raise AssertionError("an exhausted budget still called out to the provider")
    except BudgetExhausted:
        pass
    assert client.completions.calls == before, "the provider was called despite the raise"
    print("  PASS  an exhausted budget raises BEFORE the provider call, so no spend happens")

    # --- a response with no usage must still be charged, not treated as free ---
    b5 = SpendBudget(limit=10_000, state_path=tmp / "n.json", day_fn=clock2)
    silent = _FakeClient(usage_total=None)
    meter_client(silent, b5, default_charge=1024)
    silent.chat.completions.create(model="x", messages=[])
    assert b5.remaining() == 10_000 - 1024, b5.remaining()
    print("  PASS  a usage-less response (streaming) is charged the ceiling, not zero")

    # --- BudgetedLlm routes to the primary while funded ---
    clock3 = _FakeClock()
    b6 = SpendBudget(limit=100, state_path=tmp / "r.json", day_fn=clock3)
    primary, fallback = _FakeLlm("groq"), _FakeLlm("extractive")
    llm = BudgetedLlm(primary, fallback, b6)
    assert llm.answer("q1", [])["text"] == "groq:q1"
    assert llm.draft_section("t", "b", []) == "groq:t"
    print("  PASS  BudgetedLlm uses the paid model while budget remains")

    # --- ...and to the free fallback once it is gone, on EVERY method ---
    b6.spend(100)
    assert llm.answer("q2", [])["text"] == "extractive:q2"
    assert llm.draft_section("t2", "b", []) == "extractive:t2"
    assert "q2" not in primary.seen, "the paid model was still called after exhaustion"
    print("  PASS  BudgetedLlm falls back to the free local model when exhausted")

    # --- the routing is per call, not fixed at construction ---
    clock3.day = "2026-07-29"
    assert llm.answer("q3", [])["text"] == "groq:q3", "did not resume paid service the next day"
    print("  PASS  routing is decided per call, so the next day resumes paid service")

    # --- the state file is readable ops data, not an opaque blob ---
    state = json.loads((tmp / "r.json").read_text(encoding="utf-8"))
    assert "day" in state and "spent" in state, state
    print("  PASS  state on disk is inspectable ({day, spent})")

    print("\nAll spend budget checks passed.")


if __name__ == "__main__":
    main()
