"""#215 — a compound question must hand EACH store a sub-question that still carries the JOIN KEY.

Found live with all five engines composed. Asked:

    "Which products generate the most support tickets, and how much revenue do they bring?"

Routing was right (Postgres for tickets, Azure SQL for revenue) and Postgres got a good
sub-query. But Azure SQL was asked "how much revenue do they bring" — a fragment whose subject
is the pronoun "they" — and duly answered `SELECT SUM(TotalDue)`: TOTAL company revenue, not
revenue BY PRODUCT. The two halves therefore cannot be joined, and the whole point of the fleet
is that both stores key on product_number.

The deterministic decomposer splits on " and ", which is honest but drops the shared entity.
The module always intended an LLM decomposer behind the same signature; this is it.

Run: python3 tests/selftest_router_decompose_joinkey.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.decompose import decompose_query, llm_decomposer  # noqa: E402

COMPOUND = "Which products generate the most support tickets, and how much revenue do they bring?"


class _Llm:
    """A model that splits properly: each half standalone, pronouns resolved, grain kept."""

    def __init__(self, out=None, boom=False):
        self._out = out
        self._boom = boom
        self.seen = []

    def decompose_question(self, question):
        self.seen.append(question)
        if self._boom:
            raise RuntimeError("model unavailable")
        return self._out


def test_the_deterministic_split_loses_the_join_key():
    """Documents the bug — the second half is a subject-less fragment."""
    halves = decompose_query(COMPOUND)
    assert len(halves) == 2, halves
    assert "they" in halves[1].lower(), halves          # pronoun, no referent
    assert "product" not in halves[1].lower(), \
        "the join key survived — this test no longer documents the bug"


def test_llm_decomposer_keeps_the_join_key_in_every_half():
    llm = _Llm(out=["How many support tickets does each product have, by product SKU?",
                    "What is the total revenue for each product, by product SKU?"])
    halves = llm_decomposer(llm)(COMPOUND)
    assert len(halves) == 2, halves
    for h in halves:
        assert "product" in h.lower(), f"half lost the join key: {h!r}"
        assert "they" not in h.lower(), f"half still leans on a pronoun: {h!r}"
    assert llm.seen == [COMPOUND]


def test_falls_back_to_the_deterministic_split_when_the_model_fails():
    """Any model failure must degrade to the honest split — never to nothing, and never to a
    half-formed list. Same discipline as llm_sql_generator."""
    for bad in (_Llm(boom=True), _Llm(out=None), _Llm(out=[]), _Llm(out=["only one half"]),
                _Llm(out=["ok", ""]), _Llm(out="not a list"), _Llm(out=[1, 2])):
        halves = llm_decomposer(bad)(COMPOUND)
        assert halves == decompose_query(COMPOUND), f"bad model output not handled: {halves}"


def test_a_simple_question_is_left_whole():
    llm = _Llm(out=["How many open tickets does the bikes team have?"])
    q = "How many open tickets does the bikes team have?"
    assert llm_decomposer(llm)(q) == [q]


def test_the_fan_out_cap_still_holds():
    """A model must not be able to fan a question out across the whole catalog."""
    llm = _Llm(out=[f"sub question {i} about product" for i in range(9)])
    assert len(llm_decomposer(llm)("a and b and c and d")) <= 3


def main():
    print("#215 compound decomposition — the join key must survive:")
    test_the_deterministic_split_loses_the_join_key()
    print("  PASS  the deterministic ' and ' split DOES lose it (the bug, documented)")
    test_llm_decomposer_keeps_the_join_key_in_every_half()
    print("  PASS  llm_decomposer gives every half a standalone question that keeps the key")
    test_falls_back_to_the_deterministic_split_when_the_model_fails()
    test_a_simple_question_is_left_whole()
    test_the_fan_out_cap_still_holds()
    print("  PASS  degrades to the honest split on ANY bad model output / simple question / cap")
    print("\n#215 DECOMPOSE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
