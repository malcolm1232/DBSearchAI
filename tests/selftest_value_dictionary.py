"""#479 / ADR 0015 - in-tenant value dictionary + server-side literal resolution.

By LAW 1 no customer value may enter a prompt, so the generator is structurally blind to
how a value is cased, separated or abbreviated: it writes the literal the way the USER
said it. 'debit cards' misses `debit_card`, 'Dominican Republic' misses `D.R.`, and the
query returns nothing.

The repair runs server-side, after generation, inside the tenant. It fires ONLY when the
#476 probe has already proven the predicate matches no rows, so it can never touch a query
that works, and a failed resolution leaves exactly today's honest decline.

Run: PYTHONPATH=src python3 tests/selftest_value_dictionary.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import HashingEmbedding  # noqa: E402
from dbsearch.router.dictionary import (  # noqa: E402
    normalize_value, predicate_literal, resolve_literal, table_aliases,
    value_matching_embedder,
)
from dbsearch.router.store import AccessContext  # noqa: E402
from dbsearch.router.structured import FederatedSqlStore, SqliteEngine  # noqa: E402

ACCESS = AccessContext(user_oid="u1", principals=[])

TABLES = {
    "payments": {"columns": ["order_id", "payment_type", "payment_value"],
                 "rows": [["o1", "credit_card", 10.0], ["o2", "debit_card", 5.0],
                          ["o3", "boleto", 7.0], ["o4", "debit_card", 3.0]]},
    "orders": {"columns": ["order_id", "order_status"],
               "rows": [["o1", "delivered"], ["o2", "delivered"], ["o3", "canceled"],
                        ["o4", "delivered"]]},
    "players": {"columns": ["playerID", "birthCountry"],
                "rows": [["a1", "D.R."], ["a2", "USA"], ["a3", "D.R."]]},
    "wide": {"columns": ["k"], "rows": [[f"v{i}"] for i in range(300)]},
    # E-002's shape: a pipe-delimited multi-value column whose DISTINCT values are combos.
    "movies": {"columns": ["movieId", "genres"],
               "rows": [["m1", "Action|Adventure|Sci-Fi"], ["m2", "Comedy|Romance"],
                        ["m3", "Sci-Fi|Thriller"], ["m4", "Drama"]]},
}


class _StubEmbed:
    """A deliberately tiny stand-in for a dense embedder: each text maps to a point on a
    line, so 'dominican republic' sits next to 'D.R.' and far from 'USA'. The real
    embedder is exercised by the E2E run, not here - what this test pins is the LADDER."""

    _POINTS = {"dominican republic": 1.0, "d.r.": 0.98, "dr": 0.975, "usa": -1.0,
               "united states": -0.98, "boleto": 0.2,
               "science fiction": 0.9, "sci-fi": 0.88}

    def embed(self, texts: list) -> list:
        out = []
        for t in texts:
            x = self._POINTS.get(t.strip().lower(), 0.0)
            out.append([x, (1.0 - x * x) ** 0.5])
        return out


# --- normalize_value ------------------------------------------------------------------

def test_normalize_folds_case_separators_and_punctuation():
    assert normalize_value("debit cards") == normalize_value("debit_card")
    assert normalize_value("bed, bath and table") == normalize_value("bed_bath_table")
    assert normalize_value("Paid-Search") == normalize_value("paid search")
    assert normalize_value("D.R.") != normalize_value("dominican republic")


# --- resolve_literal ------------------------------------------------------------------

def test_exact_and_case_are_resolved_without_an_embedder():
    assert resolve_literal("debit_card", ["credit_card", "debit_card"], None) == "debit_card"
    assert resolve_literal("DEBIT_CARD", ["credit_card", "debit_card"], None) == "debit_card"


def test_separator_and_plural_differences_are_resolved_without_an_embedder():
    assert resolve_literal("debit cards", ["credit_card", "debit_card"], None) == "debit_card"
    assert resolve_literal("bed, bath and table",
                           ["bed_bath_table", "health_beauty"], None) == "bed_bath_table"


def test_an_abbreviation_needs_the_embedder_and_is_resolved_with_one():
    assert resolve_literal("Dominican Republic", ["D.R.", "USA"], None) is None
    assert resolve_literal("Dominican Republic", ["D.R.", "USA"], _StubEmbed()) == "D.R."


def test_an_ambiguous_embedding_match_declines_rather_than_guessing():
    """Two candidates near-equally close, neither reachable by normalization: resolving to
    either would be a coin flip asserted as fact - the failure class this work removes."""
    assert resolve_literal("Dominican Republic", ["D.R.", "DR"], _StubEmbed()) is None


def test_two_stored_values_sharing_one_notation_are_ambiguous_too():
    """`paid-search` and `paid_search` both normalize to "paid search". Picking one would
    be arbitrary, so the deterministic rung declines rather than taking the first."""
    assert resolve_literal("paid search", ["paid-search", "paid_search"], None) is None


def test_a_distant_embedding_match_declines():
    assert resolve_literal("Dominican Republic", ["boleto"], _StubEmbed()) is None


def test_no_candidates_never_resolves():
    assert resolve_literal("anything", [], _StubEmbed()) is None


# --- the hashing-stub guard -----------------------------------------------------------

def test_the_hashing_stub_is_never_used_for_value_matching():
    """Hash cosine over short strings is noise, and a manufactured collision here would be
    a confident falsehood. #225 raised the schema index's dim for the same reason; value
    matching cannot be rescued by capacity, so it is refused outright."""
    assert value_matching_embedder(HashingEmbedding(dim=128)) is None
    assert value_matching_embedder(HashingEmbedding(dim=4096)) is None
    assert value_matching_embedder(None) is None
    stub = _StubEmbed()
    assert value_matching_embedder(stub) is stub


# --- the in-tenant LLM rung (#462, ADR 0015 amendment) --------------------------------

class _StubLlm:
    """A scripted disambiguator. `in_tenant` and the scripted reply are the whole test
    surface: the rung must never call an out-of-tenant model, and must accept nothing
    but a verbatim member of the candidate list."""

    def __init__(self, reply, in_tenant=True):
        self.reply = reply
        self.in_tenant = in_tenant
        self.calls = 0

    def pick_value(self, written, candidates):
        self.calls += 1
        return self.reply


def test_the_llm_rung_resolves_an_abbreviation_the_embedder_cannot():
    """E-003's class: 'Dominican Republic' vs stored `D.R.` - the embedding rung declines
    (measured: Curacao outranks D.R.), and an LLM knows the abbreviation."""
    llm = _StubLlm("D.R.")
    assert resolve_literal("Dominican Republic", ["D.R.", "USA"], None, llm=llm) == "D.R."
    assert llm.calls == 1


def test_the_llm_rung_never_calls_an_out_of_tenant_model():
    """Values in a prompt are customer data. The rung is the ONE sanctioned exception to
    'no values in prompts', and only because the prompt never leaves the tenant - so an
    out-of-tenant model must never even be asked (LAW 1, fail closed)."""
    llm = _StubLlm("D.R.", in_tenant=False)
    assert resolve_literal("Dominican Republic", ["D.R.", "USA"], None, llm=llm) is None
    assert llm.calls == 0
    naked = _StubLlm("D.R.")
    del naked.in_tenant                       # no flag at all reads as out-of-tenant
    assert resolve_literal("Dominican Republic", ["D.R.", "USA"], None, llm=naked) is None
    assert naked.calls == 0


def test_the_llm_rung_accepts_only_a_verbatim_member():
    """A paraphrase, a hallucinated value, or an explicit NONE all decline. Anything not
    literally in the dictionary would be a fabricated WHERE literal."""
    assert resolve_literal("Dominican Republic", ["D.R.", "USA"], None,
                           llm=_StubLlm("D.R")) is None            # near miss, not member
    assert resolve_literal("Dominican Republic", ["D.R.", "USA"], None,
                           llm=_StubLlm("Dominican Republic")) is None
    assert resolve_literal("Dominican Republic", ["D.R.", "USA"], None,
                           llm=_StubLlm("NONE")) is None


def test_an_llm_failure_is_a_decline_not_an_error():
    class _Boom(_StubLlm):
        def pick_value(self, written, candidates):
            raise RuntimeError("model down")
    assert resolve_literal("Dominican Republic", ["D.R.", "USA"], None,
                           llm=_Boom("x")) is None


def test_the_deterministic_rungs_win_before_the_llm_is_ever_asked():
    """A value the ladder already resolves must not spend an LLM call - and a working
    exact match must never be second-guessed by a model."""
    llm = _StubLlm("credit_card")             # scripted to sabotage, must not be called
    assert resolve_literal("debit cards", ["credit_card", "debit_card"], None,
                           llm=llm) == "debit_card"
    assert llm.calls == 0


# --- predicate_literal ----------------------------------------------------------------

def test_predicate_literal_reads_both_shapes_the_generator_writes():
    assert predicate_literal("payment_type = 'debit card'") == (
        "payment_type", "debit card", "eq")
    assert predicate_literal("LOWER(birthCountry) = 'dominican republic'") == (
        "birthCountry", "dominican republic", "eq")
    assert predicate_literal("LOWER(x) = LOWER('Sci Fi')") == ("x", "Sci Fi", "eq")


def test_predicate_literal_keeps_the_table_qualifier():
    """The real E-001 SQL. An unqualified rewrite would not resolve in a joined query."""
    assert predicate_literal("LOWER(T1.payment_type) = 'debit card'") == (
        "T1.payment_type", "debit card", "eq")


def test_predicate_literal_reads_the_contains_like_the_generator_writes():
    """The real E-002 SQL is `LOWER(genres) LIKE '%science fiction%'`. The literal comes
    back with its wrappers stripped so the ladder sees the user's words, and the shape
    comes back as "like" so the rewrite can put the wrappers back."""
    assert predicate_literal("genres LIKE '%Sci-Fi%'") == ("genres", "Sci-Fi", "like")
    assert predicate_literal("LOWER(genres) LIKE '%science fiction%'") == (
        "genres", "science fiction", "like")
    assert predicate_literal("LOWER(g) LIKE LOWER('%science fiction%')") == (
        "g", "science fiction", "like")
    assert predicate_literal("T1.genres LIKE '%Sci-Fi%'") == ("T1.genres", "Sci-Fi", "like")


def test_predicate_literal_refuses_like_patterns_whose_wildcards_carry_meaning():
    """Only the plain contains shape resolves. A prefix/suffix anchor or an inner
    wildcard is part of what the pattern MEANS - substituting a stored value into it
    would silently change the question."""
    assert predicate_literal("genres LIKE 'Sci%'") is None
    assert predicate_literal("genres LIKE '%Fi'") is None
    assert predicate_literal("genres LIKE '%Sci%Fi%'") is None
    assert predicate_literal("genres LIKE '%Sci_Fi%'") is None
    assert predicate_literal("genres LIKE 'Sci-Fi'") is None


def test_table_aliases_maps_both_the_alias_and_the_bare_name():
    aliases = table_aliases("SELECT SUM(T1.price) FROM payments AS T1 "
                            "INNER JOIN orders AS T2 ON T1.order_id = T2.order_id")
    assert aliases["T1"] == "payments" and aliases["T2"] == "orders"
    assert aliases["payments"] == "payments"
    assert table_aliases("SELECT COUNT(*) FROM players WHERE x = 1")["players"] == "players"


def test_a_keyword_after_a_table_is_not_read_as_an_alias():
    assert table_aliases("SELECT * FROM payments WHERE k = 1") == {"payments": "payments"}
    joined = table_aliases("SELECT * FROM payments INNER JOIN orders ON a = b")
    assert joined == {"payments": "payments", "orders": "orders"}


def test_predicate_literal_refuses_shapes_it_cannot_reason_about():
    assert predicate_literal("amount > 100") is None
    assert predicate_literal("a = b") is None


# --- column_values: the multi-value token dictionary ----------------------------------

def test_a_pipe_delimited_column_is_dictionaried_by_token_not_by_combo():
    """movies.genres stores combos ("Action|Adventure|Sci-Fi"); the retrievable unit is
    the token. A combo dictionary would never contain "Sci-Fi" and E-002 would stay
    unresolvable no matter how good the ladder is."""
    from dbsearch.router.dictionary import column_values
    engine = SqliteEngine.from_tables(TABLES)
    values = column_values(engine, "movies", "genres", ACCESS)
    assert values is not None
    assert "Sci-Fi" in values, values
    assert "Action|Adventure|Sci-Fi" not in values, values
    assert len(values) == len(set(values)), "tokens must be deduped"


def test_the_cardinality_cap_applies_to_tokens_not_combos():
    """A column whose combos blow the cap but whose tokens do not still gets a
    dictionary; a column whose TOKENS blow the cap stays an identifier (LAW 1)."""
    from dbsearch.router.dictionary import MAX_DICTIONARY_VALUES, column_values
    few_tokens = {"t": {"columns": ["c"],
                        "rows": [[f"a{i % 7}|b{i % 9}|c{i % 11}"] for i in range(250)]}}
    values = column_values(SqliteEngine.from_tables(few_tokens), "t", "c", ACCESS)
    assert values is not None and len(values) <= MAX_DICTIONARY_VALUES, values
    many_tokens = {"t": {"columns": ["c"], "rows": [[f"x{i}|y{i}"] for i in range(150)]}}
    assert column_values(SqliteEngine.from_tables(many_tokens), "t", "c", ACCESS) is None


# --- end to end through the store -----------------------------------------------------

def _store(sql: str, embedder=None, value_llm=None):
    return FederatedSqlStore("s1", "bu", "Payments", "payment and player data",
                             SqliteEngine.from_tables(TABLES),
                             sql_generator=lambda *a, **k: sql, embedder=embedder,
                             value_llm=value_llm)


def test_a_missed_literal_is_repaired_and_the_substitution_is_disclosed():
    evidence = _store("SELECT COUNT(*) FROM payments WHERE payment_type = 'debit cards'") \
        .retrieve(ACCESS, "how many debit card payments?", top_k=5)
    assert len(evidence) == 1, f"expected the repaired answer, got {evidence}"
    assert "2" in evidence[0].content, evidence[0].content
    resolved = (evidence[0].provenance or {}).get("resolved")
    assert resolved == {"column": "payment_type", "written": "debit cards",
                        "resolved_to": "debit_card"}, resolved


def test_an_unresolvable_literal_still_declines():
    evidence = _store("SELECT COUNT(*) FROM payments WHERE payment_type = 'monopoly money'") \
        .retrieve(ACCESS, "how many?", top_k=5)
    assert evidence == [], f"expected a decline, got {[e.content for e in evidence]}"


def test_a_high_cardinality_column_is_not_dictionaried():
    """300 distinct values is an identifier-like column: no meaningful dictionary, and
    profiling it would materialise PII into a second place (LAW 1)."""
    evidence = _store("SELECT COUNT(*) FROM wide WHERE k = 'nope'").retrieve(
        ACCESS, "how many?", top_k=5)
    assert evidence == [], f"expected a decline, got {[e.content for e in evidence]}"


def test_a_qualified_predicate_in_a_joined_query_is_repaired():
    """The E-001 shape end to end: an aliased column, a join, and a literal the user wrote
    in their own words."""
    evidence = _store(
        "SELECT SUM(T1.payment_value) FROM payments AS T1 "
        "INNER JOIN orders AS T2 ON T1.order_id = T2.order_id "
        "WHERE LOWER(T1.payment_type) = 'debit cards'").retrieve(ACCESS, "how much?", top_k=5)
    assert len(evidence) == 1, f"expected the repaired answer, got {evidence}"
    assert "8" in evidence[0].content, evidence[0].content        # 5.0 + 3.0
    assert (evidence[0].provenance or {}).get("resolved", {}).get("resolved_to") == "debit_card"


def test_a_missed_contains_like_is_repaired_token_wise_and_disclosed():
    """E-002 end to end: the generator writes the user's words inside a contains-LIKE
    against a pipe-delimited column. The repair resolves the TOKEN and keeps the
    wildcards, so the repaired query still means "contains"."""
    evidence = _store("SELECT COUNT(*) FROM movies "
                      "WHERE LOWER(genres) LIKE '%science fiction%'",
                      embedder=_StubEmbed()).retrieve(ACCESS, "how many sci-fi films?",
                                                      top_k=5)
    assert len(evidence) == 1, f"expected the repaired answer, got {evidence}"
    assert "2" in evidence[0].content, evidence[0].content        # m1 and m3
    resolved = (evidence[0].provenance or {}).get("resolved")
    assert resolved == {"column": "genres", "written": "science fiction",
                        "resolved_to": "Sci-Fi"}, resolved


def test_an_abbreviation_is_repaired_through_the_store_by_the_in_tenant_llm():
    """E-003 end to end: no embedder, the ladder declines, the in-tenant model names the
    stored abbreviation, the repair executes and is disclosed."""
    evidence = _store("SELECT COUNT(*) FROM players "
                      "WHERE LOWER(birthCountry) = 'dominican republic'",
                      value_llm=_StubLlm("D.R.")).retrieve(ACCESS, "how many players?",
                                                           top_k=5)
    assert len(evidence) == 1, f"expected the repaired answer, got {evidence}"
    assert "2" in evidence[0].content, evidence[0].content        # a1 and a3
    resolved = (evidence[0].provenance or {}).get("resolved")
    assert resolved == {"column": "birthCountry", "written": "dominican republic",
                        "resolved_to": "D.R."}, resolved


def test_a_working_query_is_never_rewritten():
    evidence = _store("SELECT COUNT(*) FROM payments WHERE payment_type = 'debit_card'") \
        .retrieve(ACCESS, "how many?", top_k=5)
    assert len(evidence) == 1
    assert "resolved" not in (evidence[0].provenance or {}), evidence[0].provenance


# --- #495: a described prompt that fabricates a filter reprompts once, bare -----------

class _SchemaAwareGen:
    """Returns different SQL depending on whether the schema it is shown carries #486
    descriptions - which is exactly the causal lever #495 measured: llama3.1:8b writes
    correct SQL for E-001 against the bare schema and fabricates an extra predicate
    against the described one."""

    def __init__(self, described_sql, bare_sql):
        self.described_sql, self.bare_sql = described_sql, bare_sql
        self.calls = []

    def __call__(self, question, schema):
        described = any("description" in c for t in schema
                        for c in t.get("columns", [])) or any("comment" in t for t in schema)
        self.calls.append("described" if described else "bare")
        return self.described_sql if described else self.bare_sql


_DESCS = {"payments": {"": "payment records", "payment_type": "the method the buyer paid with"}}


def _described_store(gen, descriptions=_DESCS):
    return FederatedSqlStore("s1", "bu", "Payments", "payment and player data",
                             SqliteEngine.from_tables(TABLES), sql_generator=gen,
                             schema_descriptions=descriptions)


def test_a_fabricated_predicate_reprompts_bare_and_the_bare_sql_is_repaired():
    """The E-001 shape: the described generation adds a filter the question never asked
    for; no repair can save a fabricated predicate, so the store reprompts ONCE with the
    bare schema, and the bare generation's own literal miss rides the normal ladder.

    The generator is wrapped in the REAL memoizer on purpose: #254 keys on (question,
    schema fingerprint), and a fingerprint blind to descriptions hands the reprompt the
    CACHED described SQL - the exact way this fix first failed live."""
    from dbsearch.router.structured import memoized_sql_generator

    gen = _SchemaAwareGen(
        described_sql="SELECT SUM(payment_value) FROM payments "
                      "WHERE payment_type = 'debit cards' AND order_id = 'nope'",
        bare_sql="SELECT SUM(payment_value) FROM payments "
                 "WHERE payment_type = 'debit cards'")
    evidence = _described_store(memoized_sql_generator(gen)).retrieve(
        ACCESS, "how much via debit cards?", top_k=5)
    assert gen.calls == ["described", "bare"], gen.calls
    assert len(evidence) == 1, f"expected the reprompted+repaired answer, got {evidence}"
    assert "8" in evidence[0].content, evidence[0].content        # 5.0 + 3.0
    prov = evidence[0].provenance or {}
    assert prov.get("resolved", {}).get("resolved_to") == "debit_card", prov
    assert prov.get("reprompted") == "bare-schema", (
        f"the fallback to the undescribed prompt must be disclosed (LAW 8): {prov}")


def test_without_descriptions_there_is_nothing_to_reprompt_with():
    gen = _SchemaAwareGen(
        described_sql="unused",
        bare_sql="SELECT SUM(payment_value) FROM payments WHERE order_id = 'nope'")
    evidence = FederatedSqlStore("s1", "bu", "P", "d", SqliteEngine.from_tables(TABLES),
                                 sql_generator=gen).retrieve(ACCESS, "q", top_k=5)
    assert evidence == [], evidence
    assert gen.calls == ["bare"], f"one generation, no retry: {gen.calls}"


def test_a_cannot_answer_decline_never_reprompts():
    """G-001 safety. The descriptions are WHY the model correctly declines the hr/home-runs
    trap; reprompting bare would regenerate the fabrication the descriptions just
    prevented. A CannotAnswerFromSchema decline must propagate untouched."""
    from dbsearch.router.structured import CannotAnswerFromSchema

    calls = []

    def gen(question, schema):
        calls.append(1)
        raise CannotAnswerFromSchema("nothing here is about pay or personnel")

    try:
        _described_store(gen).retrieve(ACCESS, "average employee salary?", top_k=5)
        raised = False
    except CannotAnswerFromSchema:
        raised = True
    assert raised, "the honest decline must survive"
    assert len(calls) <= 2, f"widen-once may re-ask, but never with a bare schema: {calls}"


def test_a_bare_answer_without_a_repair_is_rejected_the_g001_resurrection():
    """Measured live (run 462a): on the hr/home-runs trap the described generation
    sometimes writes SQL with an empty-matching filter instead of CANNOT_ANSWER; the
    bare reprompt - stripped of the very description that prevents the fabrication -
    then answers AVG(HR) and the $3.11 falsehood returns. The reprompt exists to let
    the literal-repair ladder work, NEVER to overturn what the described prompt refused:
    a bare result whose rows carry no repair is rejected, and the decline stands."""
    gen = _SchemaAwareGen(
        described_sql="SELECT AVG(payment_value) FROM payments "
                      "WHERE payment_type = 'employee salary'",
        bare_sql="SELECT AVG(payment_value) FROM payments")     # runs fine, no repair
    evidence = _described_store(gen).retrieve(ACCESS, "average employee salary?", top_k=5)
    assert gen.calls == ["described", "bare"], gen.calls
    assert evidence == [], (
        f"an unrepaired bare answer is the resurrected fabrication: "
        f"{[(e.content, e.provenance) for e in evidence]}")


def test_a_working_described_query_never_reprompts():
    gen = _SchemaAwareGen(
        described_sql="SELECT COUNT(*) FROM payments WHERE payment_type = 'debit_card'",
        bare_sql="unused")
    evidence = _described_store(gen).retrieve(ACCESS, "how many?", top_k=5)
    assert len(evidence) == 1 and "2" in evidence[0].content
    assert gen.calls == ["described"], gen.calls
    assert "reprompted" not in (evidence[0].provenance or {}), evidence[0].provenance


def test_an_unrepairable_miss_after_the_bare_retry_still_declines():
    gen = _SchemaAwareGen(
        described_sql="SELECT SUM(payment_value) FROM payments WHERE order_id = 'nope'",
        bare_sql="SELECT SUM(payment_value) FROM payments WHERE order_id = 'still nope'")
    evidence = _described_store(gen).retrieve(ACCESS, "q", top_k=5)
    assert evidence == [], evidence
    assert gen.calls == ["described", "bare"], gen.calls


def main():
    test_normalize_folds_case_separators_and_punctuation()
    test_exact_and_case_are_resolved_without_an_embedder()
    test_separator_and_plural_differences_are_resolved_without_an_embedder()
    print("  PASS  #479 the deterministic rungs: exact, case, separators, plurals")
    test_an_abbreviation_needs_the_embedder_and_is_resolved_with_one()
    test_an_ambiguous_embedding_match_declines_rather_than_guessing()
    test_two_stored_values_sharing_one_notation_are_ambiguous_too()
    test_a_distant_embedding_match_declines()
    test_no_candidates_never_resolves()
    test_the_hashing_stub_is_never_used_for_value_matching()
    print("  PASS  #479 the embedding rung resolves abbreviations, declines on ambiguity "
          "or distance, and is refused to the hashing stub")
    test_the_llm_rung_resolves_an_abbreviation_the_embedder_cannot()
    test_the_llm_rung_never_calls_an_out_of_tenant_model()
    test_the_llm_rung_accepts_only_a_verbatim_member()
    test_an_llm_failure_is_a_decline_not_an_error()
    test_the_deterministic_rungs_win_before_the_llm_is_ever_asked()
    print("  PASS  #462 the LLM rung: in-tenant only, verbatim member or decline, never "
          "asked when the ladder already resolved")
    test_predicate_literal_reads_both_shapes_the_generator_writes()
    test_predicate_literal_refuses_shapes_it_cannot_reason_about()
    test_predicate_literal_keeps_the_table_qualifier()
    test_predicate_literal_reads_the_contains_like_the_generator_writes()
    test_predicate_literal_refuses_like_patterns_whose_wildcards_carry_meaning()
    test_table_aliases_maps_both_the_alias_and_the_bare_name()
    test_a_keyword_after_a_table_is_not_read_as_an_alias()
    print("  PASS  #479/#462 predicate_literal reads =, LOWER(=) and contains-LIKE incl. "
          "qualified columns, refuses meaning-bearing wildcards; aliases resolve")
    test_a_pipe_delimited_column_is_dictionaried_by_token_not_by_combo()
    test_the_cardinality_cap_applies_to_tokens_not_combos()
    print("  PASS  #462 multi-value columns dictionary by TOKEN, cap applies to tokens")
    test_a_missed_literal_is_repaired_and_the_substitution_is_disclosed()
    test_a_missed_contains_like_is_repaired_token_wise_and_disclosed()
    test_an_abbreviation_is_repaired_through_the_store_by_the_in_tenant_llm()
    test_a_qualified_predicate_in_a_joined_query_is_repaired()
    test_an_unresolvable_literal_still_declines()
    test_a_high_cardinality_column_is_not_dictionaried()
    test_a_working_query_is_never_rewritten()
    test_a_fabricated_predicate_reprompts_bare_and_the_bare_sql_is_repaired()
    test_without_descriptions_there_is_nothing_to_reprompt_with()
    test_a_cannot_answer_decline_never_reprompts()
    test_a_bare_answer_without_a_repair_is_rejected_the_g001_resurrection()
    test_a_working_described_query_never_reprompts()
    test_an_unrepairable_miss_after_the_bare_retry_still_declines()
    print("  PASS  #495 a described generation whose filter provably matched nothing and "
          "could not be repaired reprompts ONCE bare - disclosed, never on CannotAnswer")
    print("  PASS  #479 end to end: a missed literal is repaired and DISCLOSED, an "
          "unresolvable one still declines, and a working query is untouched")
    print("\nVALUE-DICTIONARY SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
