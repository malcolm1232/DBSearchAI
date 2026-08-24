"""#280 Task 3 (3c): the DEMO scope's default generation model resolves SEPARATELY from the
live default (ADR 0009 / the hosted-demo hardening the security review called out).

The demo endpoint is anonymous-reachable by design, so it must never drive a paid or
tenant-crossing model by default: the demo default is Groq `llama-3.3-70b-versatile` when a
GROQ key is present (fast, cheap, open), else the local Extractive model - never a hard
failure, and NEVER the live Anthropic default even when an ANTHROPIC key is configured. The
live scope's default is unchanged.

    PYTHONPATH=src python3 tests/selftest_demo_model_default.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.groq import GROQ_VERSATILE, GroqLlm  # noqa: E402
from dbsearch.adapters.local import ExtractiveLlm  # noqa: E402
from dbsearch.server.edition import build_edition  # noqa: E402
from dbsearch.server.spend_budget import BudgetedLlm  # noqa: E402

_KEYS = ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "GROQ_DEMO_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE")


def _env(**over):
    """Build an edition with a clean, explicit key env (unset unless overridden)."""
    saved = {k: os.environ.get(k) for k in _KEYS}
    try:
        for k in _KEYS:
            os.environ.pop(k, None)
        for k, v in over.items():
            os.environ[k] = v
        return build_edition()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_groq_key_present_demo_defaults_to_llama_33_70b():
    ed = _env(GROQ_API_KEY="gsk_fake")
    assert ed.demo_chat_model == f"Groq {GROQ_VERSATILE}", ed.demo_chat_model
    # #342: the demo LLM is a BudgetedLlm wrapping the paid model, so assert on .primary
    # rather than on the wrapper. Asserting through delegation would quietly start reporting
    # on the Extractive fallback the day the budget runs out.
    assert isinstance(ed.demo_chat_llm, BudgetedLlm), type(ed.demo_chat_llm)
    assert isinstance(ed.demo_chat_llm.primary, GroqLlm), type(ed.demo_chat_llm.primary)
    assert isinstance(ed.demo_chat_llm.fallback, ExtractiveLlm), type(ed.demo_chat_llm.fallback)
    assert ed.demo_chat_llm.primary._model == GROQ_VERSATILE, ed.demo_chat_llm.primary._model
    print(f"  PASS  GROQ key -> demo default is Groq {GROQ_VERSATILE} (budget-wrapped)")


def test_separate_demo_key_powers_the_demo_without_a_live_groq():
    """A public host can point the demo at a SEPARATE, spend-capped key (GROQ_DEMO_API_KEY)
    with NO live GROQ key: the demo runs on Groq, the live default stays Extractive/Anthropic."""
    ed = _env(GROQ_DEMO_API_KEY="gsk_demo_capped")
    assert isinstance(ed.demo_chat_llm.primary, GroqLlm), type(ed.demo_chat_llm.primary)
    assert ed.demo_chat_llm.primary._client.api_key == "gsk_demo_capped", \
        "demo must use the separate demo key"
    assert "Groq" not in ed.chat_model_default, ed.chat_model_default   # live has no Groq model
    print("  PASS  GROQ_DEMO_API_KEY powers the demo independently of the live GROQ key")


def test_no_keys_demo_falls_back_to_extractive():
    ed = _env()
    assert ed.demo_chat_model == "Extractive (fast, local)", ed.demo_chat_model
    assert isinstance(ed.demo_chat_llm, ExtractiveLlm)
    assert ed.chat_model_default == "Extractive (fast, local)", ed.chat_model_default
    print("  PASS  no keys -> demo default is the local Extractive model (no hard failure)")


def test_anthropic_present_demo_never_uses_the_paid_default():
    """The security-review property: an ANTHROPIC key makes the LIVE default Claude Haiku, but
    the anonymous demo must NEVER use it - it uses Groq (if present) else Extractive."""
    ed = _env(ANTHROPIC_API_KEY="sk-ant-fake", GROQ_API_KEY="gsk_fake")
    assert ed.chat_model_default.startswith("Claude Haiku"), ed.chat_model_default   # live unchanged
    assert isinstance(ed.demo_chat_llm.primary, GroqLlm), type(ed.demo_chat_llm.primary)
    # #342: the budget fallback must be the LOCAL model too - a spent budget must not silently
    # promote the anonymous demo onto the paid Anthropic default.
    assert isinstance(ed.demo_chat_llm.fallback, ExtractiveLlm), type(ed.demo_chat_llm.fallback)
    assert ed.demo_chat_model == f"Groq {GROQ_VERSATILE}", ed.demo_chat_model

    ed2 = _env(ANTHROPIC_API_KEY="sk-ant-fake")                                      # anthropic, no groq
    assert ed2.chat_model_default.startswith("Claude Haiku"), ed2.chat_model_default
    assert isinstance(ed2.demo_chat_llm, ExtractiveLlm), type(ed2.demo_chat_llm)     # demo falls back local
    print("  PASS  ANTHROPIC key -> live default is Claude Haiku, but the demo default is "
          "Groq/Extractive (the paid key stays off the anonymous demo)")


def test_router_demo_ask_uses_the_demo_model_not_the_broken_live_default():
    """End-to-end wiring: with a (fake, unreachable) ANTHROPIC key as the LIVE default and no
    GROQ, a demo `/router/ask` still answers - because it routes through the demo's Extractive
    model, never the live Anthropic default (which would fail on a real network call)."""
    os.environ.pop("GROQ_API_KEY", None)
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"          # live default -> Claude Haiku (unreachable)
    os.environ.pop("DBSEARCH_FORCE_EXTRACTIVE", None)
    from fastapi.testclient import TestClient

    from dbsearch.server.app import app          # built with the env above (fresh test process)

    client = TestClient(app)
    r = client.post("/router/ask", json={"question": "total amount by region"},
                    headers={"x-dbsearch-demo-user": "alice"})
    assert r.status_code == 200, r.text
    # a successful answer proves the paid/unreachable live default was NOT invoked for the demo.
    print("  PASS  a demo /router/ask answers via the demo model, never the live Anthropic default")


def main():
    print("Demo scope default chat model (#280 Task 3) self-test:")
    test_groq_key_present_demo_defaults_to_llama_33_70b()
    test_separate_demo_key_powers_the_demo_without_a_live_groq()
    test_no_keys_demo_falls_back_to_extractive()
    test_anthropic_present_demo_never_uses_the_paid_default()
    test_router_demo_ask_uses_the_demo_model_not_the_broken_live_default()
    print("\nDEMO MODEL DEFAULT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
