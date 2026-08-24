"""#482 - when generation fails and degrades, say so.

`llm_sql_generator` catches any generation failure outside retrieval mode and returns
`keyword_sql_generator`'s naive query instead. The degraded query is then executed, cited
and answered over as though the model had written it, and NOTHING records that a failure
happened or what it was.

That silence had a cost. #477 was a one-token bug - `REPLACE(` read as `REPLACE INTO` -
and it went unseen because every symptom looked like a model that could not handle
paraphrase: the store answered a question about salaries with `SELECT * FROM batting
LIMIT 5` and no surface anywhere said why.

This does not change WHETHER the fallback runs (that is a separate, deliberately-pinned
decision). It changes whether anyone can see it: the reason reaches the audit record, the
evidence provenance, and the store outcome the disclosure is built from.

Run: PYTHONPATH=src python3 tests/selftest_generation_degraded_disclosure.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402
from dbsearch.router.executor import execute  # noqa: E402
from dbsearch.router.store import AccessContext  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    FederatedSqlStore, SqliteEngine, generation_degraded, llm_sql_generator,
)

ACCESS = AccessContext(user_oid="u1", principals=[])
SALES = {"sales": {"columns": ["region", "amount"], "rows": [["emea", 100], ["apac", 60]]}}


class _RejectedLlm:
    """Writes valid-looking SQL the guard refuses - the #477 shape."""

    def generate_sql(self, question, schema):
        return "SELECT * FROM table_that_does_not_exist"


class _BoomLlm:
    def generate_sql(self, question, schema):
        raise RuntimeError("api down")


def _store(llm):
    return FederatedSqlStore("sales-csv", "sales", "Sales", "revenue by region",
                             SqliteEngine.from_tables(SALES),
                             sql_generator=llm_sql_generator(llm))


def test_the_reason_reaches_the_audit_record():
    store = _store(_RejectedLlm())
    evidence = store.retrieve(ACCESS, "show sales", top_k=5)
    assert evidence, "the fallback still runs - this card is about disclosure, not behaviour"
    degraded = store.audit_trail[-1].get("degraded")
    assert degraded, store.audit_trail[-1]
    assert "table_that_does_not_exist" in degraded or "visible schema" in degraded, degraded


def test_the_reason_reaches_the_evidence_provenance():
    store = _store(_BoomLlm())
    evidence = store.retrieve(ACCESS, "show sales", top_k=5)
    assert "api down" in (evidence[0].provenance or {}).get("degraded", ""), evidence[0].provenance


def test_a_successful_generation_is_not_marked_degraded():
    class _GoodLlm:
        def generate_sql(self, question, schema):
            return "SELECT region, amount FROM sales"

    store = _store(_GoodLlm())
    evidence = store.retrieve(ACCESS, "show sales", top_k=5)
    assert "degraded" not in (evidence[0].provenance or {}), evidence[0].provenance
    assert "degraded" not in store.audit_trail[-1], store.audit_trail[-1]


def test_the_flag_does_not_leak_from_one_question_into_the_next():
    """A contextvar that is only ever SET would mark every later query degraded."""
    class _Flaky:
        def __init__(self):
            self.calls = 0

        def generate_sql(self, question, schema):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("api down")
            return "SELECT region, amount FROM sales"

    store = _store(_Flaky())
    first = store.retrieve(ACCESS, "show sales", top_k=5)
    second = store.retrieve(ACCESS, "list the regions", top_k=5)
    assert (first[0].provenance or {}).get("degraded"), first[0].provenance
    assert "degraded" not in (second[0].provenance or {}), second[0].provenance


def test_the_store_outcome_carries_it_so_the_disclosure_can():
    """The executor builds the per-store disclosure record. A degraded answer must be
    visible THERE, not only in an audit log nobody reads mid-answer."""
    store = _store(_RejectedLlm())

    class Catalog:
        def get(self, sid):
            class Node:
                pass
            node = Node()
            node.store = store
            return node

    catalog = Catalog()
    decision = RoutingDecision(
        query_type="analytical",
        stores=[RoutedStore(store_id="sales-csv", business_unit="sales", score=1.0, why="")])
    report = execute(catalog, decision, "u1", "show sales", top_k=5)
    outcome = report.outcomes[0]
    assert outcome.status == "ok", outcome
    assert "degraded" in outcome.note.lower(), outcome.note

    # and it reaches the READER, not just the record: disclosure_from already surfaces any
    # outcome carrying a note, which is the reason this rides on `note` rather than a new
    # field nothing renders.
    from dbsearch.router.synthesizer import disclosure_from
    text = disclosure_from(report.outcomes)
    assert "degraded" in text.lower(), text
    assert "sales-csv" in text, text


def test_generation_degraded_is_empty_by_default():
    assert generation_degraded() is None


def main():
    test_generation_degraded_is_empty_by_default()
    test_the_reason_reaches_the_audit_record()
    test_the_reason_reaches_the_evidence_provenance()
    print("  PASS  #482 the generation-failure reason reaches the audit record and the "
          "evidence provenance")
    test_a_successful_generation_is_not_marked_degraded()
    test_the_flag_does_not_leak_from_one_question_into_the_next()
    print("  PASS  #482 a successful generation is untouched, and the flag does not leak "
          "into the next question")
    test_the_store_outcome_carries_it_so_the_disclosure_can()
    print("  PASS  #482 the store outcome carries it, so the disclosure can show it")
    print("\nGENERATION-DEGRADED-DISCLOSURE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
