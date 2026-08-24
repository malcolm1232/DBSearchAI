"""#474 - cross-store key-carry rescue (ADR 0014, option B).

"What is the total item revenue from customers located in the state of RJ?" is ONE
sentence whose filter column (customer_state) lives in one store and whose measure
(order_items.price) lives in another. Fan-out asks each store the whole question alone,
so the measure store went looking for 'rj' inside a timestamp column and the product
declined (capability D, 0/5 on the real pack).

The rescue runs ONLY when the plain path produced no evidence - so a question that
answers today can never regress through it - and its result is trusted ONLY when the
measure half is genuinely BOUND to the filter half's carried keys (aligned). An
unaligned rescue is rejected and the decline stands: this is the same trust rule that
stopped the #495 reprompt from resurrecting the G-001 fabrication.

The planner sees METADATA only - store ids, table and column names - never a value
(LAW 1), and only the CALLER's visible stores (LAW 2).

Run: PYTHONPATH=src python3 tests/selftest_cross_store_rescue.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch.router.catalog import STORE, CatalogNode, StoreCatalog  # noqa: E402
from dbsearch.router.decompose import llm_cross_store_planner  # noqa: E402
from dbsearch.router.router_service import RouterQueryService  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    CannotAnswerFromSchema, FederatedSqlStore, SqliteEngine,
)

ORIGINAL_Q = "What is the total item revenue from customers located in the state of RJ?"
FILTER_Q = "Which customer ids are located in the state of RJ?"
MEASURE_Q = "What is the total item revenue for those customers?"

CRM_SQL = "SELECT customer_id FROM customers WHERE LOWER(customer_state) = 'rj'"
ORDERS_SQL = ("SELECT SUM(i.price) AS total FROM order_items AS i "
              "INNER JOIN orders AS o ON i.order_id = o.order_id")


class _AnswerLlm:
    def answer(self, question, context):
        return {"answer": "ok", "context": context}


def _gen(mapping):
    """A scripted generator: known question -> its SQL, anything else -> the honest
    decline. This is exactly what the real generator does on a store that does not hold
    the question's columns."""
    def gen(question, schema):
        if question in mapping:
            return mapping[question]
        raise CannotAnswerFromSchema("this store holds nothing of that kind")
    return gen


def _fixture(planner, crm_map=None, orders_map=None):
    crm = FederatedSqlStore(
        "crm", "sales", "Customers", "customers location state region resident ids",
        SqliteEngine.from_tables({"customers": {
            "columns": ["customer_id", "customer_state"],
            "rows": [["c1", "RJ"], ["c2", "RJ"], ["c3", "SP"]]}}),
        sql_generator=_gen(crm_map if crm_map is not None else {FILTER_Q: CRM_SQL}))
    orders = FederatedSqlStore(
        "orders", "sales", "Orders", "orders item revenue price totals amounts",
        SqliteEngine.from_tables({
            "orders": {"columns": ["order_id", "customer_id"],
                       "rows": [["o1", "c1"], ["o2", "c2"], ["o3", "c3"]]},
            "order_items": {"columns": ["order_id", "price"],
                            "rows": [["o1", 100.0], ["o2", 50.0], ["o3", 30.0]]}}),
        sql_generator=_gen(orders_map if orders_map is not None else {MEASURE_Q: ORDERS_SQL}))
    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind="tenant", parent_id=None, acl=["p"]))
    for s in (crm, orders):
        cat.register(CatalogNode(id=s._store_id, kind=STORE, parent_id="t", acl=["p"],
                                 profile=s.profile(), store=s))
    svc = RouterQueryService(cat, InMemoryIdentity({"u1": ["p"]}), HashingEmbedding(),
                             cross_store_planner=planner)
    return svc, crm, orders


def test_the_rescue_carries_filter_keys_into_a_bound_measure_half():
    """The D-001 shape end to end: plain path declines -> planner splits on the schema ->
    filter half projects customer ids -> measure half is BOUND to them -> 150, not a
    decline and not the unfiltered 180."""
    seen = {}

    def planner(question, stores):
        seen["question"] = question
        seen["stores"] = stores
        return [("crm", FILTER_Q), ("orders", MEASURE_Q)]

    svc, _crm, orders = _fixture(planner)
    result = svc.ask("u1", ORIGINAL_Q, _AnswerLlm())

    bound_sql = orders.audit_trail[-1]["sql"]
    assert "IN ('c1', 'c2')" in bound_sql, bound_sql
    totals = [e["content"] for e in result.evidence if e["store_id"] == "orders"]
    assert any("150" in c for c in totals), (totals, result.outcomes)

    # LAW 1 + LAW 2: the planner saw the caller-visible stores' METADATA, never a value.
    assert seen["question"] == ORIGINAL_Q
    assert {s["id"] for s in seen["stores"]} == {"crm", "orders"}, seen["stores"]
    flat = str(seen["stores"])
    assert "customer_state" in flat and "order_items" in flat, flat
    assert "RJ" not in flat and "c1" not in flat, f"a VALUE leaked into the planner: {flat}"


def test_junk_evidence_that_the_synthesizer_declines_still_rescues():
    """Measured live (474a/b/c: D 0/5 despite the rescue): fan-out stores emit DEGENERATE
    rows - `SUM(product_category_name)=0.0`, a text column summed to zero - so
    'no evidence' never triggers, the synthesizer correctly refuses the junk, and the
    decline ships with the rescue never consulted. The trigger has to be the product's
    OWN final judgment: a canonical decline answer, evidence or not."""
    from dbsearch.core.copy import NO_EVIDENCE_ANSWER

    class _DecliningLlm:
        def answer(self, question, context):
            return {"answer": NO_EVIDENCE_ANSWER}      # the synthesizer refusing junk

    def planner(question, stores):
        return [("crm", FILTER_Q), ("orders", MEASURE_Q)]

    svc, _crm, orders = _fixture(
        planner,
        # the plain path produces a junk aggregate ROW on crm - evidence exists
        crm_map={ORIGINAL_Q: "SELECT SUM(customer_state) AS total FROM customers",
                 FILTER_Q: CRM_SQL})
    result = svc.ask("u1", ORIGINAL_Q, _DecliningLlm())
    bound_sql = orders.audit_trail[-1]["sql"]
    assert "IN ('c1', 'c2')" in bound_sql, bound_sql
    totals = [e["content"] for e in result.evidence if e["store_id"] == "orders"]
    assert any("150" in c for c in totals), (totals, result.outcomes)


def test_an_llm_authored_refusal_triggers_the_rescue_too():
    """THE bug that kept D at 0/5 live while the offline proof reached gold: the
    synthesizer's ANSWER prompt tells the model to 'say plainly you do not have that
    information', so live declines are LLM-AUTHORED ('I do not have that information.')
    - and ask()'s trigger only matched the canonical constants. The rescue never fired
    on the served path, on any model. The trigger must catch the refusal FAMILY, same
    as the condensed pass learned in c2f0834."""
    class _RefusingLlm:
        def answer(self, question, context):
            if any("150" in c or "IN (" in str(c) for c in context):
                return {"answer": "The total is 150."}
            return {"answer": "I do not have that information."}

    def planner(question, stores):
        return [("crm", FILTER_Q), ("orders", MEASURE_Q)]

    svc, _crm, orders = _fixture(
        planner,
        # plain path: junk evidence row -> the LLM refuses in its own words
        crm_map={ORIGINAL_Q: "SELECT SUM(customer_state) AS total FROM customers",
                 FILTER_Q: CRM_SQL})
    result = svc.ask("u1", ORIGINAL_Q, _RefusingLlm())
    bound_sql = orders.audit_trail[-1]["sql"]
    assert "IN ('c1', 'c2')" in bound_sql, bound_sql
    assert "150" in result.answer, result.answer


def test_the_rescue_condenses_carry_rows_for_the_synthesizer():
    """Measured live (cap_sql_a/b/c 260805, D 0/5 WITH the rescue dispatching): both
    halves ran, SUM(price)=39180.04 sat in the evidence, and the 8B synthesizer declined
    anyway - one fact row drowned among 11 raw carry-key rows (findings s15: volume
    decisive, order irrelevant). The filter half's key rows are MECHANICS - the bind
    already consumed them - so the synthesizer gets at most 2 sample rows plus the
    measure half's facts, and the condensation is disclosed (LAW 8)."""
    many_rj = [[f"c{i}", "RJ"] for i in range(1, 26)] + [["x1", "SP"]]
    crm = FederatedSqlStore(
        "crm", "sales", "Customers", "customers location state region resident ids",
        SqliteEngine.from_tables({"customers": {
            "columns": ["customer_id", "customer_state"], "rows": many_rj}}),
        sql_generator=_gen({ORIGINAL_Q: "SELECT SUM(customer_state) AS total FROM customers",
                            FILTER_Q: CRM_SQL}))
    orders = FederatedSqlStore(
        "orders", "sales", "Orders", "orders item revenue price totals amounts",
        SqliteEngine.from_tables({
            "orders": {"columns": ["order_id", "customer_id"],
                       "rows": [[f"o{i}", f"c{i}"] for i in range(1, 26)]},
            "order_items": {"columns": ["order_id", "price"],
                            "rows": [[f"o{i}", 10.0] for i in range(1, 26)]}}),
        sql_generator=_gen({MEASURE_Q: ORDERS_SQL}))
    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind="tenant", parent_id=None, acl=["p"]))
    for s in (crm, orders):
        cat.register(CatalogNode(id=s._store_id, kind=STORE, parent_id="t", acl=["p"],
                                 profile=s.profile(), store=s))

    class _RecordingLlm:
        def __init__(self):
            self.contexts = []

        def answer(self, question, context):
            self.contexts.append(list(context))
            return {"answer": "I do not have that information."}

    llm = _RecordingLlm()
    svc = RouterQueryService(cat, InMemoryIdentity({"u1": ["p"]}), HashingEmbedding(),
                             cross_store_planner=lambda q, st: [("crm", FILTER_Q),
                                                                ("orders", MEASURE_Q)])
    result = svc.ask("u1", ORIGINAL_Q, llm)
    rescue_ctx = llm.contexts[-1]
    # Findings s19 (the decisive variants): even TWO key sample rows beside the fact row
    # make llama3.1:8b decline; fact row + readable proof alone converts. Key rows are
    # mechanics: NONE reach the prompt; 2 samples stay in evidence for the reader.
    key_rows = [c for c in rescue_ctx if "customer_id=" in c and not c.startswith("[query]")]
    assert key_rows == [], f"carry keys must stay out of the prompt: {key_rows}"
    assert any("SUM" in c for c in rescue_ctx), rescue_ctx
    assert any("customer_id=" in e["content"] for e in result.evidence), \
        "sample key rows must stay visible to the reader"
    assert "condensed" in (result.disclosure or ""), result.disclosure


def test_a_rejected_rescue_discloses_its_reason():
    """LAW 8: from outside, 'never fired' and 'fired and rejected at stage X' looked
    identical - which cost a day of live-vs-offline confusion. A rejected rescue now
    says why in the disclosure."""
    def planner(question, stores):
        return [("crm", FILTER_Q), ("orders", MEASURE_Q)]

    svc, _crm, _orders = _fixture(
        planner,
        crm_map={FILTER_Q: "SELECT COUNT(*) AS n FROM customers WHERE customer_state = 'RJ'"})
    result = svc.ask("u1", ORIGINAL_Q, _AnswerLlm())
    assert result.evidence == [], result.evidence
    assert "rescue attempted and rejected" in (result.disclosure or ""), result.disclosure


def test_a_real_answer_that_is_not_a_canonical_decline_never_rescues():
    """An LLM answer that actually answers - even a WRONG one - must not trigger the
    rescue: overriding a delivered answer is a different (and dangerous) feature."""
    calls = []

    def planner(question, stores):
        calls.append(1)
        return [("crm", FILTER_Q), ("orders", MEASURE_Q)]

    class _ConfidentLlm:
        def answer(self, question, context):
            return {"answer": "The total is 0.0."}

    svc, _crm, _orders = _fixture(
        planner, crm_map={ORIGINAL_Q: "SELECT SUM(customer_state) AS total FROM customers",
                          FILTER_Q: CRM_SQL})
    svc.ask("u1", ORIGINAL_Q, _ConfidentLlm())
    assert calls == [], f"a delivered answer must never be overridden: {calls}"


def test_the_rescue_never_fires_when_the_plain_path_answered():
    calls = []

    def planner(question, stores):
        calls.append(question)
        return [("crm", FILTER_Q), ("orders", MEASURE_Q)]

    svc, _crm, _orders = _fixture(
        planner, crm_map={ORIGINAL_Q: "SELECT COUNT(*) AS n FROM customers",
                          FILTER_Q: CRM_SQL})
    result = svc.ask("u1", ORIGINAL_Q, _AnswerLlm())
    assert result.evidence, "the plain path answered"
    assert calls == [], f"the planner must not run when evidence exists: {calls}"


def test_an_unaligned_rescue_is_rejected_and_the_decline_stands():
    """The G-001 trust rule, applied here: a filter half that carries nothing leaves the
    measure half unbound, and an unbound rescue answer is exactly the fabrication risk -
    the original decline must stand."""
    def planner(question, stores):
        return [("crm", FILTER_Q), ("orders", MEASURE_Q)]

    svc, _crm, orders = _fixture(
        planner,
        # the filter half generates a scalar - no projection, nothing carries
        crm_map={FILTER_Q: "SELECT COUNT(*) AS n FROM customers WHERE customer_state = 'RJ'"})
    result = svc.ask("u1", ORIGINAL_Q, _AnswerLlm())
    assert result.evidence == [], (
        f"an unbound rescue must be rejected: {result.evidence}")


def test_no_planner_means_todays_behaviour_exactly():
    svc, _crm, _orders = _fixture(None)
    result = svc.ask("u1", ORIGINAL_Q, _AnswerLlm())
    assert result.evidence == [], result.evidence


def test_a_planner_that_declines_to_split_changes_nothing():
    svc, _crm, _orders = _fixture(lambda q, stores: None)
    result = svc.ask("u1", ORIGINAL_Q, _AnswerLlm())
    assert result.evidence == [], result.evidence


# ── the factory: llm_cross_store_planner guards the model's raw reply ────────────────────

class _RawLlm:
    def __init__(self, reply):
        self._reply = reply

    def plan_cross_store(self, question, stores):
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


# #524: this metadata used to omit the `orders` table, leaving the two stores with NO
# column in common - so every plan written over it was one no join could have executed.
# The stub now mirrors the fixture above (customer_id is the shared key), and the filter
# halves name that key the way _PLAN_SYSTEM mandates ("List the distinct <key> values
# where ..."), because the factory now requires a plan to show what it intends to carry.
STORES_META = [{"id": "crm", "tables": [{"table": "customers",
                                         "columns": ["customer_id", "customer_state"]}]},
               {"id": "orders", "tables": [
                   {"table": "orders", "columns": ["order_id", "customer_id"]},
                   {"table": "order_items", "columns": ["order_id", "price"]}]}]

CLEAN_FILTER = "List the distinct customer_id values where customer_state is RJ."
CLEAN_MEASURE = "Total item revenue for those customer_id values?"


def test_factory_parses_a_clean_plan():
    plan = llm_cross_store_planner(_RawLlm(
        '{"filter_store": "crm", "filter": "' + CLEAN_FILTER + '", '
        '"measure_store": "orders", "measure": "' + CLEAN_MEASURE + '"}'))
    assert plan("q", STORES_META) == [("crm", CLEAN_FILTER), ("orders", CLEAN_MEASURE)]


def test_factory_parses_a_fenced_plan():
    plan = llm_cross_store_planner(_RawLlm(
        '```json\n{"filter_store": "crm", "filter": "' + CLEAN_FILTER + '", '
        '"measure_store": "orders", "measure": "m?"}\n```'))
    assert plan("q", STORES_META) == [("crm", CLEAN_FILTER), ("orders", "m?")]


def test_factory_returns_none_on_single_junk_or_failure():
    for reply in ("SINGLE", "single", "", "not json at all",
                  '{"filter": "only one half"}', '{"filter": "", "measure": "m"}',
                  '["f", "m"]', RuntimeError("model down"),
                  # both halves on ONE store = not cross-store
                  '{"filter_store": "crm", "filter": "f?", '
                  '"measure_store": "crm", "measure": "m?"}',
                  # a store id the caller was never shown must be refused (LAW 2)
                  '{"filter_store": "secret-hr", "filter": "f?", '
                  '"measure_store": "orders", "measure": "m?"}'):
        plan = llm_cross_store_planner(_RawLlm(reply))
        assert plan("q", STORES_META) is None, reply


def main():
    test_the_rescue_carries_filter_keys_into_a_bound_measure_half()
    test_junk_evidence_that_the_synthesizer_declines_still_rescues()
    test_an_llm_authored_refusal_triggers_the_rescue_too()
    test_the_rescue_condenses_carry_rows_for_the_synthesizer()
    test_a_rejected_rescue_discloses_its_reason()
    test_a_real_answer_that_is_not_a_canonical_decline_never_rescues()
    test_the_rescue_never_fires_when_the_plain_path_answered()
    test_an_unaligned_rescue_is_rejected_and_the_decline_stands()
    test_no_planner_means_todays_behaviour_exactly()
    test_a_planner_that_declines_to_split_changes_nothing()
    print("  PASS  #474 rescue: fires only on a decline, trusted only when ALIGNED, "
          "planner sees metadata only")
    test_factory_parses_a_clean_plan()
    test_factory_parses_a_fenced_plan()
    test_factory_returns_none_on_single_junk_or_failure()
    print("  PASS  #474 planner factory: strict parse, None on anything else")
    print("\nCROSS-STORE RESCUE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
