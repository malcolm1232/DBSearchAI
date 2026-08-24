"""#160 (cosmos_db slice) — CosmosEngine/CosmosStore/CosmosProvider: Azure Cosmos DB (NoSQL /
Core API) as a FEDERATED_DOC store, proven WITHOUT network or azure-cosmos (a fake container is
injected). Cosmos is schemaless JSON docs, so it has its OWN StorePort (not FederatedSqlStore):
the query runs IN the container (pushdown) and matching items come back as RECORD evidence.

Run: python3 tests/selftest_router_cosmos.py
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router import (  # noqa: E402
    CosmosEngine, CosmosProvider, CosmosStore, keyword_cosmos_query, llm_cosmos_generator,
    validate_cosmos_query,
)
from dbsearch.router.store import AccessContext, FEDERATED_DOC  # noqa: E402

_ITEMS = [
    {"id": "1", "region": "apac", "status": "open", "priority": "high", "hours": 5, "_ts": 1},
    {"id": "2", "region": "emea", "status": "open", "priority": "low", "hours": 2, "_ts": 2},
    {"id": "3", "region": "apac", "status": "closed", "priority": "high", "hours": 8, "_ts": 3},
]


class _FakeContainer:
    def __init__(self, items):
        self._items = items
        self.queries = []

    def query_items(self, query, enable_cross_partition_query=False):
        self.queries.append(query)
        q = query.lower()
        if "group by" in q and "count(1)" in q:
            c = Counter(i["status"] for i in self._items)
            return [{"status": k, "count": v} for k, v in c.items()]
        if "select value avg(" in q:
            field = query.split("AVG(c.")[1].split(")")[0]
            vals = [i[field] for i in self._items if field in i]
            return [sum(vals) / len(vals)] if vals else []
        if "select value sum(" in q:
            field = query.split("SUM(c.")[1].split(")")[0]
            return [sum(i[field] for i in self._items if field in i)]
        if "group by" in q and "sum(" in q:
            agg = {}
            for i in self._items:
                agg[i["region"]] = agg.get(i["region"], 0) + i["hours"]
            return [{"region": k, "total_hours": v} for k, v in agg.items()]
        return list(self._items)


def _engine():
    return CosmosEngine(lambda: _FakeContainer(_ITEMS), container_name="tickets")


def test_schema_infers_fields_and_skips_system_keys():
    fields = [f["name"] for f in _engine().schema()[0]["fields"]]
    assert fields == ["region", "status", "priority", "hours"], fields   # no id / _ts


def test_generator_count_by_group():
    q = keyword_cosmos_query("how many tickets by status", _engine().schema())
    assert q == "SELECT c.status, COUNT(1) AS count FROM c GROUP BY c.status", q


def test_generator_sum_by_group_picks_mentioned_field():
    q = keyword_cosmos_query("total hours by region", _engine().schema())
    assert q == "SELECT c.region, SUM(c.hours) AS total_hours FROM c GROUP BY c.region", q


def test_generator_default_is_select_star():
    q = keyword_cosmos_query("show me some tickets", _engine().schema())
    assert q.startswith("SELECT TOP") and "FROM c" in q, q


def test_guard_allows_select_blocks_writes():
    validate_cosmos_query("SELECT c.region FROM c")
    for bad in ("DELETE FROM c", "SELECT 1; DROP c", "UPSERT INTO c"):
        try:
            validate_cosmos_query(bad)
            raise AssertionError(f"guard let through: {bad}")
        except ValueError:
            pass


def test_provider_builds_federated_doc_store():
    prov = CosmosProvider(engine_factory=lambda cfg: _engine())
    store = prov.build({"id": "tk", "business_unit": "support", "title": "Tickets",
                        "description": "support tickets"})
    p = store.profile()
    assert p.kind == FEDERATED_DOC and p.store_id == "tk", p
    assert [f["name"] for f in p.schema[0]["fields"]] == ["region", "status", "priority", "hours"]
    assert prov.probe({"id": "x"}).freshness == "live"


def test_retrieve_returns_record_evidence_with_provenance():
    prov = CosmosProvider(engine_factory=lambda cfg: _engine())
    store = prov.build({"id": "tk", "business_unit": "support", "title": "T", "description": "d"})
    ev = store.retrieve(AccessContext(user_oid="alice"), "how many tickets by status")
    assert [e.kind for e in ev] == ["record", "record"], ev
    assert {e.content for e in ev} == {"status=open, count=2", "status=closed, count=1"}, ev
    assert ev[0].provenance["query"].startswith("SELECT c.status"), ev[0].provenance
    assert ev[0].provenance["container"] == "tickets", ev[0].provenance


class _CrossPartitionContainer:
    """Emulates Cosmos serverless: rejects cross-partition GROUP BY projections, but serves
    plain projections — so the engine's rollup fallback (projection pushdown + in-tenant
    aggregate) is exercised. Also serves VALUE-scalar aggregates."""

    def __init__(self, items):
        self._items = items
        self.queries = []

    def query_items(self, query, enable_cross_partition_query=False):
        self.queries.append(query)
        q = query.lower()
        if "group by" in q:
            raise RuntimeError("(BadRequest) Cross partition query only supports "
                               "'VALUE <AggregateFunc>' for aggregates.")
        if q.startswith("select value count(1)"):
            return [len(self._items)]
        # a plain projection (the fallback runs this) — return the requested fields
        return [dict(i) for i in self._items]


def test_engine_rolls_up_groupby_when_cross_partition_rejected():
    eng = CosmosEngine(lambda: _CrossPartitionContainer(_ITEMS), container_name="tickets")
    rows = eng.query("SELECT c.status, COUNT(1) AS count FROM c GROUP BY c.status")
    got = {r["status"]: r["count"] for r in rows}
    assert got == {"open": 2, "closed": 1}, got     # rolled up in-tenant after projection pushdown
    sums = eng.query("SELECT c.region, SUM(c.hours) AS total FROM c GROUP BY c.region")
    assert {r["region"]: r["total"] for r in sums} == {"apac": 13, "emea": 2}, sums


def test_retrieve_handles_scalar_value_results():
    # a VALUE COUNT(1) query returns bare ints, not dicts - retrieve must not choke.
    # #231 UPDATED THE ASSERTION, deliberately: this used to assert the content was the bare
    # string "6". That WAS the defect. An unlabelled scalar tells the synthesizer nothing about
    # what was counted, and it answered "I don't have that information" to a question the database
    # had answered correctly. The scalar is now labelled from the aggregate in the query. The
    # not-choking-on-a-bare-int property this test was written for is unchanged and still asserted.
    class _Scalar:
        def query_items(self, query, enable_cross_partition_query=False):
            if query.lower().startswith("select value"):
                return [6]                      # scalar aggregate
            return [{"id": "1", "n": 1}]         # schema-sampling SELECT * returns docs
    store = CosmosProvider(engine_factory=lambda cfg: CosmosEngine(
        lambda: _Scalar(), container_name="c")).build(
        {"id": "s", "business_unit": "b", "title": "t", "description": "d"})
    store._gen = lambda q, s: "SELECT VALUE COUNT(1) FROM c"
    ev = store.retrieve(AccessContext(user_oid="alice"), "how many total")
    assert ev and ev[0].content == "count=6", ev


def test_default_engine_requires_connection_fields():
    try:
        CosmosProvider._default_engine({"endpoint": "e", "key": "k"})
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "database" in str(e) and "container" in str(e), e


def test_ungrouped_aggregate_is_pushed_down_not_sampled():
    """#228: "what is the average X" has NO "by Y", so `group` was None and the whole aggregate
    branch was skipped - the query fell through to `SELECT TOP k * FROM c`, a blind SAMPLE. The
    synthesizer then averaged the handful of docs it was handed and stated THAT as the population
    figure. Measured live on the fleet: it answered "the average customer review rating is 4.2"
    when the true average over all 84 reviews was 3.33. A wrong number, cited, stated as fact.
    Cosmos SQL supports the ungrouped aggregate natively; emit it."""
    schema = _engine().schema()
    assert keyword_cosmos_query("what is the average hours", schema) == \
        "SELECT VALUE AVG(c.hours) FROM c"
    assert keyword_cosmos_query("what is the total hours", schema) == \
        "SELECT VALUE SUM(c.hours) FROM c"
    # the GROUPED form still works - this is an addition, not a replacement
    assert keyword_cosmos_query("total hours by region", schema) == \
        "SELECT c.region, SUM(c.hours) AS total_hours FROM c GROUP BY c.region"


def test_ungrouped_aggregate_never_guesses_which_field_to_average():
    """The field must be NAMED in the question. Guessing which field to average is the #211
    fabrication in a different costume - it is exactly how a SUM over an arbitrary column gets
    labelled as the answer. An unnamed field falls through to the sample, where total_rows now
    discloses that it IS a sample."""
    schema = _engine().schema()
    q = keyword_cosmos_query("what is the average", schema)     # names no field
    assert q == "SELECT TOP 20 * FROM c", q
    assert "AVG" not in q


def test_evidence_carries_the_true_row_count_so_a_sample_cannot_pass_as_the_whole():
    """#206 parity, finally applied to Cosmos (#228). structured.py has carried total_rows since
    #206 precisely so nobody downstream can mistake 5-of-84 for 5-of-5; cosmos.py never did, and
    that silence is what let the sample average ship as the population average."""
    store = CosmosStore("reviews", "voice", "Reviews", "customer reviews", _engine())
    ev = store.retrieve(AccessContext(user_oid="alice", principals=[]), "show me reviews", top_k=2)
    assert len(ev) == 2, len(ev)                       # only 2 rows reach the synthesizer
    for e in ev:
        assert e.provenance["total_rows"] == 3, e.provenance   # ...out of 3 the query really saw


class _FakeLlm:
    """A stand-in for the edition chat model. Records the schema it was handed so the test can
    assert LAW 1 (names/types only, never a document value)."""
    def __init__(self, reply):
        self.reply = reply
        self.seen_schema = None

    def generate_cosmos_query(self, question, schema):
        self.seen_schema = schema
        return self.reply


def test_llm_generator_proposes_the_query_and_the_guard_validates_it():
    """#229: Cosmos was the ONLY engine with no LLM seam - every SQL store got
    llm_sql_generator while Cosmos answered real questions with a regex. Same seam now."""
    llm = _FakeLlm("```sql\nSELECT VALUE AVG(c.hours) FROM c\n```")
    gen = llm_cosmos_generator(llm)
    assert gen("average hours", _engine().schema()) == "SELECT VALUE AVG(c.hours) FROM c"


def test_llm_generator_sends_field_names_and_types_only_never_document_values():
    """LAW 1: Cosmos's pseudo-schema is INFERRED by sampling documents, so this is the one place
    a value could leak to the model. It keeps type(v).__name__, never v."""
    llm = _FakeLlm("SELECT VALUE COUNT(1) FROM c")
    llm_cosmos_generator(llm)("how many", _engine().schema())
    blob = repr(llm.seen_schema)
    for value in ("apac", "emea", "open", "closed", "high"):    # real values in _ITEMS
        assert value not in blob, (value, blob)
    assert "region" in blob and "status" in blob                # names DO go


def test_llm_cannot_answer_declines_and_never_falls_back_to_a_broad_sample():
    """#211 parity: CANNOT_ANSWER is a RESULT, not a failure. It must NOT degrade to the keyword
    generator - `SELECT TOP k * FROM c` over the wrong container is the same fabrication in
    different clothes."""
    gen = llm_cosmos_generator(_FakeLlm("CANNOT_ANSWER"))
    _raises_cannot_answer(lambda: gen("average employee salary", _engine().schema()))


def test_a_bad_generation_degrades_to_the_keyword_generator_never_errors():
    """Any OTHER failure (empty, API error, an unsafe query) falls back, so a store never errors
    or leaks on a bad generation."""
    class _Boom:
        def generate_cosmos_query(self, q, s):
            raise RuntimeError("api down")
    gen = llm_cosmos_generator(_Boom())
    assert gen("total hours by region", _engine().schema()) == \
        "SELECT c.region, SUM(c.hours) AS total_hours FROM c GROUP BY c.region"

    unsafe = llm_cosmos_generator(_FakeLlm("DELETE FROM c"))
    assert unsafe("how many", _engine().schema()) == "SELECT VALUE COUNT(1) FROM c"   # fell back


def _raises_cannot_answer(fn):
    from dbsearch.router.structured import CannotAnswerFromSchema
    try:
        fn()
    except CannotAnswerFromSchema:
        return
    raise AssertionError("expected CannotAnswerFromSchema")


def test_scalar_aggregate_evidence_is_labelled_not_a_naked_number():
    """#231: `SELECT VALUE COUNT(1)` returns a bare `2`. Handing the synthesizer the string "2"
    told it nothing about WHAT was counted, so it answered "I don't have that information" to a
    question the database had answered correctly. Label it at the STORE, so both generators
    (keyword and the #229 LLM) benefit."""
    class _ScalarContainer:
        def query_items(self, query, enable_cross_partition_query=False):
            return [2]
    store = CosmosStore("reviews", "voice", "Reviews", "reviews",
                        CosmosEngine(lambda: _ScalarContainer(), container_name="reviews"),
                        generator=lambda q, sch: "SELECT VALUE COUNT(1) FROM c WHERE c.rating = 5")
    ev = store.retrieve(AccessContext(user_oid="alice", principals=[]), "how many 5 star reviews")
    assert ev[0].content == "count=2", ev[0].content       # not the naked "2"


def main():
    print("#160 cosmos_db engine+store+provider self-test:")
    test_schema_infers_fields_and_skips_system_keys()
    test_generator_count_by_group()
    test_generator_sum_by_group_picks_mentioned_field()
    test_generator_default_is_select_star()
    print("  PASS  schema inference (skips system keys) / Cosmos-SQL generator (count/sum by, default)")
    test_guard_allows_select_blocks_writes()
    print("  PASS  read-only guard (SELECT only, blocks writes/multi-statement)")
    test_provider_builds_federated_doc_store()
    test_retrieve_returns_record_evidence_with_provenance()
    test_engine_rolls_up_groupby_when_cross_partition_rejected()
    test_retrieve_handles_scalar_value_results()
    test_default_engine_requires_connection_fields()
    test_ungrouped_aggregate_is_pushed_down_not_sampled()
    test_ungrouped_aggregate_never_guesses_which_field_to_average()
    test_evidence_carries_the_true_row_count_so_a_sample_cannot_pass_as_the_whole()
    test_llm_generator_proposes_the_query_and_the_guard_validates_it()
    test_llm_generator_sends_field_names_and_types_only_never_document_values()
    test_llm_cannot_answer_declines_and_never_falls_back_to_a_broad_sample()
    test_a_bad_generation_degrades_to_the_keyword_generator_never_errors()
    test_scalar_aggregate_evidence_is_labelled_not_a_naked_number()
    print("  PASS  provider -> FEDERATED_DOC store / retrieve -> RECORD evidence / config validation")
    print("  PASS  cross-partition GROUP BY rollup fallback / scalar VALUE-result handling (live-caught)")
    print("\n#160 COSMOS_DB SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
