"""#254 — the same question must not produce a different row universe each time it is asked.

Reproduced live, 6 identical asks of "What is the total revenue for each product sku?" through
one identity against the fleet AdventureWorks:

    run 1: join=LEFT  total=295      run 4: join=INNER total=142
    run 2: join=INNER total=142      run 5: join=LEFT  total=295
    run 3: join=LEFT  total=295      run 6: join=INNER total=142

3/6 each way. The model already runs at temperature 0.0 — LLMs are simply not bit-deterministic,
so sampling config cannot fix this. Both queries are DEFENSIBLE (a never-sold product has no
revenue, or has zero revenue), and after #207 both surface the same top-5, because the null/zero
rows sort to the bottom. What differs is the DENOMINATOR the user is told: "5 of 295" vs
"5 of 142" for one identical question. Asking twice and being told two different things is a
trust failure regardless of which number is defensible.

So the fix is determinism, not picking a join: memoize the generated SQL per (question, schema)
so a repeat of the same question over the same schema returns the SAME query.

Run: python3 tests/selftest_sql_generator_stability.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.structured import memoized_sql_generator  # noqa: E402

SCHEMA = [{"table": "sales", "columns": [{"name": "region"}, {"name": "amount"}]}]
OTHER_SCHEMA = [{"table": "sales", "columns": [{"name": "region"}]}]


def _flapping_generator():
    """Stands in for the live model: same input, alternating output."""
    calls = {"n": 0}

    def gen(question, schema):
        calls["n"] += 1
        return ("SELECT a FROM sales JOIN p ON x" if calls["n"] % 2
                else "SELECT a FROM sales LEFT JOIN p ON x")

    return gen, calls


def test_the_same_question_returns_the_same_sql():
    gen, calls = _flapping_generator()
    stable = memoized_sql_generator(gen)
    first = stable("total revenue per sku", SCHEMA)
    for _ in range(5):
        assert stable("total revenue per sku", SCHEMA) == first, \
            "the same question over the same schema produced different SQL"
    assert calls["n"] == 1, f"the underlying generator ran {calls['n']}x — memoization is not working"


def test_a_different_question_or_schema_is_generated_afresh():
    """Memoizing must not answer a DIFFERENT question with a cached query — that would be far
    worse than the instability it fixes."""
    gen, calls = _flapping_generator()
    stable = memoized_sql_generator(gen)
    stable("total revenue per sku", SCHEMA)
    stable("how many orders per region", SCHEMA)          # different question
    stable("total revenue per sku", OTHER_SCHEMA)         # same question, different schema
    assert calls["n"] == 3, f"expected 3 distinct generations, got {calls['n']}"


def test_question_matching_ignores_case_and_surrounding_whitespace_only():
    gen, calls = _flapping_generator()
    stable = memoized_sql_generator(gen)
    a = stable("Total Revenue Per SKU", SCHEMA)
    b = stable("  total revenue per sku  ", SCHEMA)
    assert a == b and calls["n"] == 1, (a, b, calls)
    # ...but a genuinely different wording is NOT treated as the same question
    stable("total revenue per sku last year", SCHEMA)
    assert calls["n"] == 2, calls


def test_a_failed_generation_is_never_cached():
    """Caching an empty/failed generation would freeze the failure for the whole session."""
    outs = ["", None, "SELECT 1"]
    calls = {"n": 0}

    def gen(question, schema):
        v = outs[calls["n"]]
        calls["n"] += 1
        return v

    stable = memoized_sql_generator(gen)
    assert stable("q", SCHEMA) == ""
    assert stable("q", SCHEMA) is None          # retried, not served from cache
    assert stable("q", SCHEMA) == "SELECT 1"
    assert stable("q", SCHEMA) == "SELECT 1"    # now cached
    assert calls["n"] == 3, calls


def test_the_cache_is_bounded():
    """An unbounded cache on a long-lived server is a leak."""
    calls = {"n": 0}

    def gen(question, schema):
        calls["n"] += 1
        return f"SELECT {calls['n']}"

    stable = memoized_sql_generator(gen, max_entries=3)
    for i in range(5):
        stable(f"q{i}", SCHEMA)
    before = calls["n"]
    stable("q4", SCHEMA)                        # most recent — still cached
    assert calls["n"] == before, "the most recent entry was evicted"
    stable("q0", SCHEMA)                        # oldest — evicted, regenerated
    assert calls["n"] == before + 1, "the cache is not bounded / not LRU"


def main():
    print("#254 SQL generator stability (same question -> same query):")
    test_the_same_question_returns_the_same_sql()
    test_a_different_question_or_schema_is_generated_afresh()
    test_question_matching_ignores_case_and_surrounding_whitespace_only()
    print("  PASS  repeats are stable; a different question or schema still generates afresh")
    test_a_failed_generation_is_never_cached()
    test_the_cache_is_bounded()
    print("  PASS  failures are never cached, and the cache is bounded + LRU")
    print("\nSQL-GENERATOR-STABILITY SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
