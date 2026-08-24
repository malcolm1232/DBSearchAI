"""LAW-1 demo hardening — DBSEARCH_FORCE_EXTRACTIVE pins the default answer engine to
Extractive even when an LLM API key is present, so a request that omits `model` cannot
egress retrieved content. Hermetic (AnthropicLlm constructs without any network call).

    python3 tests/selftest_force_extractive.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _build(force: bool, key: "str | None"):
    for k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
        os.environ.pop(k, None)
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    if force:
        os.environ["DBSEARCH_FORCE_EXTRACTIVE"] = "1"
    sys.modules.pop("dbsearch.server.edition", None)
    import dbsearch.server.edition as ed
    return ed.build_edition()


def test_key_present_without_flag_defaults_to_llm():
    e = _build(force=False, key="sk-ant-DUMMY")
    assert e.chat_model_default.startswith("Claude Haiku"), e.chat_model_default
    assert "Extractive (fast, local)" in e.chat_models


def test_flag_pins_extractive_even_with_key():
    from dbsearch.adapters.local import ExtractiveLlm
    e = _build(force=True, key="sk-ant-DUMMY")
    assert e.chat_model_default == "Extractive (fast, local)", e.chat_model_default
    # LLM still registered, but omitting the model yields Extractive (no egress):
    assert isinstance(e.resolve_chat_llm(None), ExtractiveLlm)
    assert isinstance(e.resolve_chat_llm(""), ExtractiveLlm)


if __name__ == "__main__":
    test_key_present_without_flag_defaults_to_llm()
    test_flag_pins_extractive_even_with_key()
    # leave env clean for any suite loop
    for k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
        os.environ.pop(k, None)
    print("selftest_force_extractive: OK")
