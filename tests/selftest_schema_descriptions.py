"""#486 - the NL2SQL prompt carries names and types only, so the model guesses MEANING.

Verbatim, this is everything the generator was given for "typically, how satisfied were
shoppers once their purchase arrived":

    orders(order_id TEXT, customer_id TEXT, order_status TEXT, ...)
    reviews(review_id TEXT, order_id TEXT, review_score INTEGER, ...)

Nothing says what a table is FOR, nothing says `review_score` is a 1-5 satisfaction
rating, nothing connects "satisfied" to it. Measured on the #473 real pack, every one of
the five remaining confidently-wrong answers is a meaning failure of this kind, and the
routing was correct in 31 of 32 - the model reached the right store and then guessed.

**The LAW 1 line runs straight through this feature.** An AUTHORED description ("order
lifecycle state") is metadata: someone wrote it about the schema, and it may go into a
prompt. A description DERIVED from rows ("contains delivered, shipped, canceled") is
customer data wearing a label, and putting it in a prompt would ship customer values to
Azure OpenAI / Anthropic / Groq - the exact thing #462 and #479 exist to avoid. So the
channel accepts authored text and nothing else, and the tests below pin that.

Run: PYTHONPATH=src python3 tests/selftest_schema_descriptions.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.anthropic import sql_user_prompt  # noqa: E402
from dbsearch.router.structured import CsvSqlProvider, describe_schema  # noqa: E402

PLAIN = [
    {"table": "orders", "columns": [{"name": "order_id", "type": "TEXT"},
                                    {"name": "order_status", "type": "TEXT"}]},
    {"table": "reviews", "columns": [{"name": "review_id", "type": "TEXT"},
                                     {"name": "review_score", "type": "INTEGER"}]},
]

DESCRIPTIONS = {
    "orders": {
        "": "customer purchase transactions, one row per order",
        "order_status": "lifecycle state the order reached",
    },
    "reviews": {
        "": "buyer satisfaction, one row per reviewed order",
        "review_score": "satisfaction rating, 1 worst to 5 best",
    },
}


# --- the prompt channel -----------------------------------------------------------------

def test_without_descriptions_the_prompt_is_byte_identical_to_today():
    """No description configured must change nothing at all - every existing store, every
    existing baseline."""
    assert sql_user_prompt("q", PLAIN) == (
        "Schema:\norders(order_id TEXT, order_status TEXT)\n"
        "reviews(review_id TEXT, review_score INTEGER)\n\nQuestion: q")


def test_a_table_description_reaches_the_prompt():
    prompt = sql_user_prompt("q", describe_schema(PLAIN, DESCRIPTIONS))
    assert "customer purchase transactions, one row per order" in prompt, prompt


def test_a_column_description_reaches_the_prompt():
    prompt = sql_user_prompt("q", describe_schema(PLAIN, DESCRIPTIONS))
    assert "satisfaction rating, 1 worst to 5 best" in prompt, prompt


def test_the_column_description_is_attached_to_its_own_column():
    prompt = sql_user_prompt("q", describe_schema(PLAIN, DESCRIPTIONS))
    assert "  reviews.review_score: satisfaction rating, 1 worst to 5 best" in prompt, prompt
    assert "  orders.order_status: lifecycle state the order reached" in prompt, prompt


def test_the_signature_lines_are_unchanged_and_carry_no_sql_comment_marker():
    """A model shown `--` in its schema copies it into the SQL it writes, and validate_sql
    refuses any query containing a comment - the #477 failure mode, self-inflicted."""
    prompt = sql_user_prompt("q", describe_schema(PLAIN, DESCRIPTIONS))
    assert "orders(order_id TEXT, order_status TEXT)" in prompt, prompt
    assert "reviews(review_id TEXT, review_score INTEGER)" in prompt, prompt
    assert "--" not in prompt, prompt


def test_a_described_schema_still_carries_names_and_types():
    prompt = sql_user_prompt("q", describe_schema(PLAIN, DESCRIPTIONS))
    assert "review_score INTEGER" in prompt, prompt
    assert "order_id TEXT" in prompt, prompt


def test_describe_schema_never_mutates_the_caller_s_schema():
    before = [{"table": t["table"], "columns": [dict(c) for c in t["columns"]]} for t in PLAIN]
    describe_schema(PLAIN, DESCRIPTIONS)
    assert PLAIN == before, PLAIN


def test_partial_descriptions_are_fine():
    described = describe_schema(PLAIN, {"orders": {"": "just the table"}})
    prompt = sql_user_prompt("q", described)
    assert "just the table" in prompt
    assert "reviews(review_id TEXT, review_score INTEGER)" in prompt, prompt


def test_an_unknown_table_or_column_is_ignored_rather_than_invented():
    described = describe_schema(PLAIN, {"ghost": {"": "nope"}, "orders": {"ghost_col": "nope"}})
    assert "nope" not in sql_user_prompt("q", described)


# --- the LAW 1 line ---------------------------------------------------------------------

def test_descriptions_are_authored_config_never_derived_from_rows():
    """The store takes descriptions from its CONFIG, alongside title and description -
    text a human wrote about the schema. Nothing in this path reads a row, so no value can
    reach the prompt through it. If this ever changes, LAW 1 is broken and #479's whole
    server-side-resolution design was pointless."""
    store = CsvSqlProvider().build({
        "id": "s1", "title": "Olist", "description": "orders",
        "tables": {"orders": {"columns": ["order_id", "order_status"],
                              "rows": [["o1", "delivered"], ["o2", "canceled"]]}},
        "schema_descriptions": {"orders": {"": "purchase transactions",
                                           "order_status": "lifecycle state"}},
    })
    prompt = sql_user_prompt("q", store.described_schema())
    assert "purchase transactions" in prompt and "lifecycle state" in prompt, prompt
    # the values are RIGHT THERE in the same store, and none of them may appear
    for value in ("delivered", "canceled", "o1", "o2"):
        assert value not in prompt, f"{value!r} leaked into the prompt: {prompt}"


def test_a_store_without_descriptions_behaves_exactly_as_before():
    store = CsvSqlProvider().build({
        "id": "s1", "title": "Olist", "description": "orders",
        "tables": {"orders": {"columns": ["order_id"], "rows": [["o1"]]}}})
    assert store.described_schema() == store._engine.schema()


def main():
    test_without_descriptions_the_prompt_is_byte_identical_to_today()
    test_a_table_description_reaches_the_prompt()
    test_a_column_description_reaches_the_prompt()
    test_the_column_description_is_attached_to_its_own_column()
    test_the_signature_lines_are_unchanged_and_carry_no_sql_comment_marker()
    test_a_described_schema_still_carries_names_and_types()
    print("  PASS  #486 authored table/column descriptions reach the NL2SQL prompt, "
          "attached to the right table, alongside names and types")
    test_describe_schema_never_mutates_the_caller_s_schema()
    test_partial_descriptions_are_fine()
    test_an_unknown_table_or_column_is_ignored_rather_than_invented()
    print("  PASS  #486 partial and unknown descriptions degrade cleanly, and the caller's "
          "schema is never mutated")
    test_descriptions_are_authored_config_never_derived_from_rows()
    test_a_store_without_descriptions_behaves_exactly_as_before()
    print("  PASS  #486 LAW 1: descriptions are AUTHORED config, and no row value can "
          "reach the prompt through this path")
    print("\nSCHEMA-DESCRIPTIONS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
