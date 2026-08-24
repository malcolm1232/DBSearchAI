"""#219 - federated semi-join: bind half A's shown join-key values into half B's query.

"tickets vs revenue by product" decomposes correctly but each half answered over a DISJOINT
key set (24 ticket rows vs 295 product rows, each its own top-5), so the synthesizer honestly
refused to correlate two samples that never intersect. The fix runs the compound halves in
sequence (they already did), takes the join-key values half A SHOWED, and constrains half B to
them - a federated semi-join - so the halves line up row-for-row.

The values are CUSTOMER DATA from store A spliced into SQL for store B, so injection is the
design constraint. It is handled MECHANICALLY (a strict allowlist + quote-safe literalization),
never by an LLM prompt, so no value ever reaches the model and #230's schema-only property holds.

Run: PYTHONPATH=src python3 tests/selftest_router_semijoin.py
     python3 -m pytest tests/selftest_router_semijoin.py -q
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch.router.catalog import STORE, CatalogNode, StoreCatalog  # noqa: E402
from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402
from dbsearch.router.evidence import CHUNK, Evidence  # noqa: E402
from dbsearch.router.executor import OK, execute  # noqa: E402
from dbsearch.router.router_service import RouterQueryService, _carried_join_values  # noqa: E402
from dbsearch.router.store import (  # noqa: E402
    AccessContext, INDEXED, SEMANTIC, StorePort, StoreProfile,
)
from dbsearch.router.structured import (  # noqa: E402
    FederatedSqlStore, SqliteEngine, bind_literal, bind_values_from_evidence, groupby_column,
    sanitize_bind_values,
)

ACCESS = AccessContext(user_oid="u1", principals=[])


# ── the pure guards: the injection boundary ──────────────────────────────────────────────

def test_sanitizer_drops_every_injection_attempt_and_counts_the_drops():
    """The allowlist is the injection boundary: anything carrying a quote, semicolon, comment
    marker, paren, percent or backslash is DROPPED - never escaped, never passed. The drop is
    counted so it can be disclosed, never silent."""
    dirty = [
        "BK-M68B-38",            # kept: a real SKU (letters, digits, hyphen)
        "Road Bike 700",         # kept: space allowed
        "Blue.Steel_7",          # kept: dot and underscore allowed
        "x' OR '1'='1",          # dropped: single quote
        "a; DROP TABLE t",       # dropped: semicolon
        "c/*x*/",                # dropped: slash + star
        "d) OR (1=1",            # dropped: parens
        "e%wild",                # dropped: percent
        "f\\g",                  # dropped: backslash
        "x" * 65,                # dropped: over 64 chars
    ]
    kept, dropped = sanitize_bind_values(dirty)
    assert kept == ["BK-M68B-38", "Road Bike 700", "Blue.Steel_7"], kept
    assert dropped == 7, dropped
    # not a single survivor carries a quote - so a single-quoted literal cannot be broken out of
    for v in kept:
        assert "'" not in v and '"' not in v and ";" not in v, v


def test_sanitizer_dedupes_and_caps():
    kept, dropped = sanitize_bind_values(["A", "A", "B", "A"])
    assert kept == ["A", "B"], kept
    assert dropped == 2, dropped                        # the two duplicate "A"s counted as drops
    # #474 raised the disclosed cap to KEY_CARRY_CAP (a real filter's key set - D-001
    # matches 296 customers); anything past it still drops, counted, never silent.
    from dbsearch.router.structured import KEY_CARRY_CAP
    many = [f"K{i}" for i in range(KEY_CARRY_CAP + 18)]
    capped, dropped2 = sanitize_bind_values(many)
    assert len(capped) == KEY_CARRY_CAP, len(capped)
    assert dropped2 == 18, dropped2


def test_bind_literal_quotes_text_and_leaves_numbers_bare():
    assert bind_literal("BK-M68B-38") == "'BK-M68B-38'"
    assert bind_literal("42") == "42"
    assert bind_literal("-3.5") == "-3.5"
    assert bind_literal("A1") == "'A1'"                 # alphanumeric is NOT numeric -> quoted


def test_groupby_column_strips_the_alias_prefix_and_is_none_without_grouping():
    assert groupby_column("SELECT p.ProductNumber, SUM(x) t FROM a GROUP BY p.ProductNumber") \
        == "ProductNumber"
    assert groupby_column("SELECT product_number, COUNT(*) FROM t GROUP BY product_number") \
        == "product_number"
    assert groupby_column("SELECT * FROM t WHERE x = 1") is None      # no GROUP BY -> no key


def test_bind_values_from_evidence_reads_the_shown_rows_of_the_grouped_column():
    """The values to carry are the GROUP-BY column's value in each row half A actually SHOWED."""
    sql = "SELECT product_number, COUNT(*) AS ticket_count FROM t GROUP BY product_number"
    evs = [
        Evidence("s", "b", CHUNK, "product_number=BK-M68B-38, ticket_count=8",
                 provenance={"sql": sql}),
        Evidence("s", "b", CHUNK, "product_number=BK-M68S-42, ticket_count=5",
                 provenance={"sql": sql}),
    ]
    col, values = bind_values_from_evidence(evs)
    assert col == "product_number", col
    assert values == ["BK-M68B-38", "BK-M68S-42"], values


def test_bind_values_from_evidence_returns_nothing_when_half_a_did_not_group():
    evs = [Evidence("s", "b", CHUNK, "total_amount=900",
                    provenance={"sql": "SELECT SUM(amount) AS total_amount FROM t"})]
    col, values = bind_values_from_evidence(evs)
    assert col is None and values == [], (col, values)


def test_a_plain_projection_carries_its_column_474_gate_2():
    """#474 gate 2: 'which customers are in RJ' generates a plain single-column
    projection - no GROUP BY - and that IS the filter half of every ordinary
    filter-here-measure-there question. Its shown values carry."""
    sql = "SELECT customer_id FROM customers WHERE LOWER(customer_state) = 'rj'"
    evs = [Evidence("s", "b", CHUNK, "customer_id=c1",
                    provenance={"sql": sql, "total_rows": 2}),
           Evidence("s", "b", CHUNK, "customer_id=c2",
                    provenance={"sql": sql, "total_rows": 2})]
    col, values = bind_values_from_evidence(evs)
    assert col == "customer_id", col
    assert values == ["c1", "c2"], values


def test_a_distinct_projection_and_an_aliased_projection_carry_too():
    sql = "SELECT DISTINCT c.customer_id FROM customers AS c WHERE c.state = 'rj'"
    evs = [Evidence("s", "b", CHUNK, "customer_id=c9",
                    provenance={"sql": sql, "total_rows": 1})]
    assert bind_values_from_evidence(evs) == ("customer_id", ["c9"])


def test_a_multi_column_projection_does_not_carry():
    """Two columns = no unambiguous key. Guessing which one is the join key would bind
    half B to the wrong values - a wrong total, not a decline."""
    sql = "SELECT customer_id, customer_city FROM customers WHERE state = 'rj'"
    evs = [Evidence("s", "b", CHUNK, "customer_id=c1, customer_city=rio",
                    provenance={"sql": sql, "total_rows": 1})]
    assert bind_values_from_evidence(evs) == (None, [])


def test_a_partial_projection_carry_is_refused_the_adr_0014_cliff():
    """ADR 0014, settled up front: a filter matching more rows than half A SHOWED must
    not carry - half B would silently aggregate a SUBSET and assert it as the total,
    the exact confidently-wrong class this codebase exists to remove. Fail closed."""
    sql = "SELECT customer_id FROM customers WHERE state = 'sp'"
    evs = [Evidence("s", "b", CHUNK, f"customer_id=c{i}",
                    provenance={"sql": sql, "total_rows": 2000}) for i in range(5)]
    assert bind_values_from_evidence(evs) == (None, [])


def test_a_grouped_partial_carry_is_still_allowed_the_219_shape():
    """The #219 breakdown-to-breakdown alignment deliberately carries only the shown
    top-N of a RANKED breakdown - that is its contract ('line up the halves row for
    row'), not a subset-total bug. The cliff guard applies to projections only."""
    sql = "SELECT product, COUNT(*) AS n FROM t GROUP BY product ORDER BY n DESC"
    evs = [Evidence("s", "b", CHUNK, "product=A, n=8",
                    provenance={"sql": sql, "total_rows": 295})]
    col, values = bind_values_from_evidence(evs)
    assert col == "product" and values == ["A"]


# ── the store: retrieve_bound wraps, guards, executes, and fails open ─────────────────────

def _sales_engine():
    return SqliteEngine.from_tables({"sales": {
        "columns": ["product", "amount"],
        "rows": [["A", 100], ["A", 50], ["B", 30], ["C", 200], ["C", 10], ["D", 5]]}})


GROUPED_SQL = ("SELECT product, SUM(amount) AS total FROM sales "
               "GROUP BY product ORDER BY total DESC")


def _store(gen, engine=None):
    return FederatedSqlStore("s1", "sales", "Sales", "sales amounts by product",
                             engine or _sales_engine(), sql_generator=gen)


def test_retrieve_bound_constrains_the_result_to_the_bound_keys_only():
    store = _store(lambda q, s: GROUPED_SQL)
    unbound = {e.content.split(",")[0] for e in store.retrieve(ACCESS, "totals", top_k=10)}
    bound = store.retrieve_bound(ACCESS, "totals", ["A", "B"], top_k=10)
    got = {e.content.split(",")[0] for e in bound}
    # LAW 2 direction: a semi-join can only NARROW - never surface a key the unbound query didn't
    assert got == {"product=A", "product=B"}, got
    assert got.issubset(unbound), (got, unbound)
    prov = bound[0].provenance["bind"]
    assert prov["aligned"] is True and prov["column"] == "product" and prov["values_n"] == 2, prov
    assert "_semi WHERE product IN ('A', 'B')" in bound[0].provenance["sql"], bound[0].provenance


def test_retrieve_bound_never_sends_a_key_value_to_the_generator_law1():
    """The join-key values are spliced in MECHANICALLY. The generator (the only thing that would
    talk to an LLM) sees the question and schema, never a value - so #230's schema-only property
    is preserved even through the semi-join."""
    seen = []

    def spy_gen(question, schema):
        seen.append((question, schema))
        return GROUPED_SQL

    _store(spy_gen).retrieve_bound(ACCESS, "totals", ["ZZK-9917", "ZZK-9918"], top_k=10)
    blob = repr(seen)
    assert "ZZK-9917" not in blob and "ZZK-9918" not in blob, blob


def test_retrieve_bound_fails_open_when_the_wrapped_query_errors():
    """If this half aliases its key so the outer column name misses, the wrapped query errors.
    Retry UNBOUND and mark it unaligned - an unaligned answer is honest, an error is not."""
    aliased = "SELECT product AS sku, SUM(amount) AS total FROM sales GROUP BY product"
    store = _store(lambda q, s: aliased)
    out = store.retrieve_bound(ACCESS, "totals", ["A", "B"], top_k=10)
    assert out, "fail-open must still return the unbound result, not nothing"
    assert out[0].provenance["bind"]["aligned"] is False, out[0].provenance
    assert out[0].provenance["bind"]["reason"] == "the key-bound query failed", out[0].provenance
    # unbound -> all products present, proving it really ran the un-wrapped query
    assert {e.content.split(",")[0] for e in out} >= {"sku=C", "sku=A"}, out


def test_retrieve_bound_runs_unbound_when_this_half_has_no_group_key():
    store = _store(lambda q, s: "SELECT * FROM sales LIMIT 5")
    out = store.retrieve_bound(ACCESS, "list", ["A"], top_k=10)
    assert out[0].provenance["bind"]["aligned"] is False, out[0].provenance
    assert out[0].provenance["bind"]["reason"] == "this half does not group on a key", \
        out[0].provenance


def test_retrieve_bound_runs_unbound_and_discloses_when_every_key_was_dropped():
    store = _store(lambda q, s: GROUPED_SQL)
    out = store.retrieve_bound(ACCESS, "totals", ["x'y", "a;b"], top_k=10)   # both unsafe
    prov = out[0].provenance["bind"]
    assert prov["aligned"] is False and prov["dropped"] == 2, prov
    assert prov["reason"] == "no key value survived sanitization", prov
    assert {e.content.split(",")[0] for e in out} == {"product=C", "product=A", "product=B",
                                                      "product=D"}, out


# a CTE query - `SELECT * FROM (WITH ... )` is INVALID SQL (T-SQL and others forbid it); caught
# live on real AdventureWorks, where the revenue half generated exactly this shape.
CTE_SQL = ("WITH agg AS (SELECT product, SUM(amount) AS total FROM sales GROUP BY product) "
           "SELECT product, total FROM agg ORDER BY total DESC")


# ── #474 gate 3: a scalar aggregate accepts a carried-column bind ────────────────────────

def _olist_engine():
    """The D-001 shape in miniature: the measure store holds orders + order_items; the
    carried key column (customer_id) lives on orders, one join away from the measure."""
    return SqliteEngine.from_tables({
        "orders": {"columns": ["order_id", "customer_id", "order_status"],
                   "rows": [["o1", "c1", "delivered"], ["o2", "c2", "delivered"],
                            ["o3", "c3", "delivered"]]},
        "order_items": {"columns": ["order_id", "price"],
                        "rows": [["o1", 100.0], ["o2", 50.0], ["o3", 30.0]]},
    })


def _measure_store(gen):
    return FederatedSqlStore("orders", "sales", "Orders", "orders and item revenue",
                             _olist_engine(), sql_generator=gen)


def test_a_scalar_aggregate_binds_directly_when_its_sql_touches_the_key_table():
    """Half B is `SUM(price)` over a join that already includes orders - the carried
    customer_id list is pushed into its WHERE mechanically. Gold here: c1+c2 = 150."""
    sql = ("SELECT SUM(i.price) AS total FROM order_items AS i "
           "INNER JOIN orders AS o ON i.order_id = o.order_id")
    bound = _measure_store(lambda q, s: sql).retrieve_bound(
        ACCESS, "total item revenue for those customers", ["c1", "c2"], top_k=5,
        column="customer_id")
    assert len(bound) == 1, bound
    assert "150" in bound[0].content, bound[0].content
    prov = bound[0].provenance["bind"]
    assert prov["aligned"] is True and prov["column"] == "customer_id", prov
    assert "IN ('c1', 'c2')" in bound[0].provenance["sql"], bound[0].provenance["sql"]


def test_a_scalar_aggregate_binds_one_hop_away_through_a_shared_key():
    """Half B's SQL never touches orders - but orders holds customer_id and shares
    order_id with order_items, so the bind wraps one hop: order_id IN (SELECT order_id
    FROM orders WHERE customer_id IN ...). Deterministic, schema-derived, no LLM."""
    sql = "SELECT SUM(price) AS total FROM order_items"
    bound = _measure_store(lambda q, s: sql).retrieve_bound(
        ACCESS, "total item revenue for those customers", ["c1", "c2"], top_k=5,
        column="customer_id")
    assert len(bound) == 1, bound
    assert "150" in bound[0].content, bound[0].content
    assert bound[0].provenance["bind"]["aligned"] is True, bound[0].provenance


def test_a_hallucinated_placeholder_on_the_carry_column_is_neutralized():
    """Measured live: asked for 'the total for those customer_id values', the generator
    invented literal placeholders - `WHERE LOWER(customer_id) IN ('customer_1',
    'customer_2')` - which zero the result even after the real keys are injected. Any
    literal predicate on the CARRY column is a guess by construction (the model never
    saw a value - LAW 1), so the bind replaces it with the authoritative key list."""
    sql = ("SELECT SUM(i.price) AS total FROM order_items AS i "
           "INNER JOIN orders AS o ON i.order_id = o.order_id "
           "WHERE LOWER(customer_id) IN ('customer_1', 'customer_2')")
    bound = _measure_store(lambda q, s: sql).retrieve_bound(
        ACCESS, "total item revenue for those customers", ["c1", "c2"], top_k=5,
        column="customer_id")
    assert len(bound) == 1, bound
    assert "150" in bound[0].content, bound[0].content
    executed = bound[0].provenance["sql"]
    assert "customer_1" not in executed, executed
    assert "IN ('c1', 'c2')" in executed, executed
    assert bound[0].provenance["bind"]["aligned"] is True, bound[0].provenance


def test_a_placeholder_equality_on_the_carry_column_is_neutralized_too():
    sql = ("SELECT SUM(i.price) AS total FROM order_items AS i "
           "INNER JOIN orders AS o ON i.order_id = o.order_id "
           "WHERE o.customer_id = 'those customer_id values'")
    bound = _measure_store(lambda q, s: sql).retrieve_bound(
        ACCESS, "total", ["c1", "c2"], top_k=5, column="customer_id")
    assert len(bound) == 1 and "150" in bound[0].content, bound
    assert "those customer_id values" not in bound[0].provenance["sql"]


def test_a_predicate_on_an_UNRELATED_column_is_never_touched():
    """Neutralization is scoped to the carry column alone - a real filter the question
    asked for (order_status) must survive the bind untouched."""
    sql = ("SELECT SUM(i.price) AS total FROM order_items AS i "
           "INNER JOIN orders AS o ON i.order_id = o.order_id "
           "WHERE o.order_status = 'delivered'")
    bound = _measure_store(lambda q, s: sql).retrieve_bound(
        ACCESS, "total", ["c1", "c2"], top_k=5, column="customer_id")
    assert len(bound) == 1, bound
    executed = bound[0].provenance["sql"]
    assert "order_status = 'delivered'" in executed, executed
    assert "IN ('c1', 'c2')" in executed, executed


def test_an_unbindable_scalar_falls_open_unbound_and_disclosed():
    """No table in this store holds the carried column and no one-hop path exists: the
    bind falls open exactly as before - unbound, aligned False - never an error."""
    bound = _measure_store(lambda q, s: "SELECT SUM(price) AS total FROM order_items") \
        .retrieve_bound(ACCESS, "total", ["z1"], top_k=5, column="warehouse_zone")
    assert len(bound) == 1
    assert "180" in bound[0].content, bound[0].content          # the full, unbound total
    assert bound[0].provenance["bind"]["aligned"] is False, bound[0].provenance


def test_a_scalar_bind_sanitizes_its_values_like_every_other_bind():
    """The injection boundary is the same allowlist: unsafe values DROP (counted), and if
    none survive the query runs unbound + disclosed rather than spliced."""
    sql = ("SELECT SUM(i.price) AS total FROM order_items AS i "
           "INNER JOIN orders AS o ON i.order_id = o.order_id")
    bound = _measure_store(lambda q, s: sql).retrieve_bound(
        ACCESS, "total", ["c1'; DROP TABLE orders;--"], top_k=5, column="customer_id")
    assert len(bound) == 1
    prov = bound[0].provenance["bind"]
    assert prov["aligned"] is False and prov["dropped"] == 1, prov


def test_the_grouped_bind_path_is_unchanged_when_a_column_is_passed():
    """A breakdown half still binds through the #219 wrap even when the caller supplies
    the carried column - the groupby path wins, byte-identical to before."""
    store = _store(lambda q, s: GROUPED_SQL)
    bound = store.retrieve_bound(ACCESS, "totals", ["A", "B"], top_k=10, column="product")
    got = {e.content.split(",")[0] for e in bound}
    assert got == {"product=A", "product=B"}, got
    assert "_semi WHERE product IN ('A', 'B')" in bound[0].provenance["sql"]


def test_semijoin_wrap_plain_vs_cte_keeps_the_with_at_the_top():
    from dbsearch.router.structured import semijoin_wrap
    plain = semijoin_wrap("SELECT a, SUM(b) t FROM x GROUP BY a", "a", ["P1", "P2"])
    assert plain == ("SELECT * FROM (SELECT a, SUM(b) t FROM x GROUP BY a) "
                     "AS _semi WHERE a IN ('P1', 'P2')"), plain
    cte = semijoin_wrap("WITH c AS (SELECT a FROM x) SELECT a FROM c", "a", ["P1"])
    assert cte == ("WITH c AS (SELECT a FROM x) SELECT * FROM (SELECT a FROM c) "
                   "AS _semi WHERE a IN ('P1')"), cte
    # the naive `FROM (WITH ...)` shape must NOT appear - that is the invalid one
    assert "FROM (WITH" not in cte, cte


def test_semijoin_wrap_strips_a_trailing_order_by_illegal_in_a_derived_table():
    """`SELECT * FROM (... ORDER BY x) AS t` is invalid in T-SQL (and SQL subqueries generally)
    without TOP/OFFSET - caught live on Azure SQL, where the revenue half ended in ORDER BY. The
    wrap must drop the trailing top-level ORDER BY (cosmetic once filtered), but NEVER an ORDER BY
    inside a window function or a CTE body."""
    from dbsearch.router.structured import semijoin_wrap
    w = semijoin_wrap("SELECT a, SUM(b) t FROM x GROUP BY a ORDER BY t DESC", "a", ["P1"])
    assert w == "SELECT * FROM (SELECT a, SUM(b) t FROM x GROUP BY a) AS _semi WHERE a IN ('P1')", w
    assert "ORDER BY" not in w, w
    # a window-function ORDER BY (inside parens) must SURVIVE - it is not the trailing clause
    keep = semijoin_wrap(
        "SELECT a, ROW_NUMBER() OVER (ORDER BY b) rn FROM x", "a", ["P1"])
    assert "OVER (ORDER BY b)" in keep, keep


def test_retrieve_bound_binds_a_cte_query_that_the_naive_wrap_could_not():
    store = _store(lambda q, s: CTE_SQL)
    bound = store.retrieve_bound(ACCESS, "totals", ["A", "C"], top_k=10)
    got = {e.content.split(",")[0] for e in bound}
    assert got == {"product=A", "product=C"}, got                 # genuinely constrained
    prov = bound[0].provenance
    assert prov["bind"]["aligned"] is True, prov                  # NOT a fail-open
    assert prov["sql"].startswith("WITH agg AS"), prov["sql"]     # WITH stayed at the top
    assert "AS _semi WHERE product IN ('A', 'C')" in prov["sql"], prov["sql"]


# ── the executor: binds a capable store, discloses one that cannot ────────────────────────

class _FakeStore(StorePort):
    def __init__(self, sid, bu, evidence):
        self._id, self._bu, self._ev = sid, bu, evidence

    def profile(self):
        return StoreProfile(store_id=self._id, title=self._id, description="",
                            kind=INDEXED, capabilities={SEMANTIC}, business_unit=self._bu)

    def authorize(self, user_oid):
        return AccessContext(user_oid=user_oid, principals=["p"])

    def retrieve(self, access, question, top_k=5):
        return self._ev[:top_k]


class _BindableStore(_FakeStore):
    def __init__(self, sid, bu, bind_prov):
        super().__init__(sid, bu, [])
        self._bind_prov = bind_prov
        self.bound_with = None

    def retrieve_bound(self, access, question, values, top_k=5, column=None):
        self.bound_with = list(values)
        self.bound_column = column
        return [Evidence(self._id, self._bu, CHUNK, "x",
                         provenance={"total_rows": 1, "bind": self._bind_prov})]


def _catalog(*stores):
    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind="tenant", parent_id=None, acl=["p"]))
    for s in stores:
        cat.register(CatalogNode(id=s._id, kind=STORE, parent_id="t", acl=["p"],
                                 profile=s.profile(), store=s))
    return cat


def _decision(*stores):
    routed = [RoutedStore(s._id, s._bu, 1.0) for s in stores]
    return RoutingDecision(query_type="semantic", stores=routed, candidates=routed)


def test_executor_binds_a_capable_store_to_the_carried_values():
    b = _BindableStore("revenue", "sales", {"aligned": True, "dropped": 0})
    report = execute(_catalog(b), _decision(b), "u1", "revenue by product",
                     bind_values=["P1", "P2"])
    assert b.bound_with == ["P1", "P2"], b.bound_with
    note = next(o.note for o in report.outcomes if o.store_id == "revenue")
    assert note == "", note                              # aligned, nothing dropped -> no note


def test_executor_discloses_a_store_that_cannot_align_or_that_dropped_keys():
    # a store WITHOUT retrieve_bound gets the values but can't use them -> disclosed
    plain = _FakeStore("reviews", "voice",
                       [Evidence("reviews", "voice", CHUNK, "r", provenance={"total_rows": 1})])
    report = execute(_catalog(plain), _decision(plain), "u1", "q", bind_values=["P1"])
    note = next(o.note for o in report.outcomes if o.store_id == "reviews")
    assert "could not be aligned" in note, note

    # a store that aligned but had to drop unsafe keys discloses the drop
    b = _BindableStore("revenue", "sales", {"aligned": True, "dropped": 3})
    report2 = execute(_catalog(b), _decision(b), "u1", "q", bind_values=["P1"])
    note2 = next(o.note for o in report2.outcomes if o.store_id == "revenue")
    assert "3 key value(s) dropped" in note2, note2


def test_executor_without_bind_values_is_byte_identical_to_before():
    """The whole feature is inert on a non-compound query - no bind_values, plain retrieve."""
    b = _BindableStore("revenue", "sales", {"aligned": True})
    report = execute(_catalog(b), _decision(b), "u1", "q")     # no bind_values
    assert b.bound_with is None                                # retrieve_bound never called
    assert all(o.note == "" for o in report.outcomes), report.outcomes


# ── #232: order a semi-join carry-source half by the measure, not the key ─────────────────

class _RankableStore(_FakeStore):
    def __init__(self, sid, bu):
        super().__init__(sid, bu, [])
        self.ranked = False

    def retrieve_ranked(self, access, question, top_k=5):
        self.ranked = True
        return [Evidence(self._id, self._bu, CHUNK, "x", provenance={"total_rows": 1})]


def test_rank_by_measure_rewrites_a_key_ordered_breakdown():
    from dbsearch.router.structured import rank_by_measure
    assert rank_by_measure(
        "SELECT product_number, COUNT(*) AS ticket_count FROM t GROUP BY product_number "
        "ORDER BY product_number"
    ) == ("SELECT product_number, COUNT(*) AS ticket_count FROM t GROUP BY product_number "
          "ORDER BY ticket_count DESC")
    # no existing ORDER BY -> appended
    assert rank_by_measure("SELECT region, SUM(amount) AS total_amount FROM s GROUP BY region") \
        == "SELECT region, SUM(amount) AS total_amount FROM s GROUP BY region ORDER BY total_amount DESC"
    # no alias -> order by the aggregate expression itself
    assert rank_by_measure("SELECT p, COUNT(*) FROM t GROUP BY p ORDER BY p") \
        == "SELECT p, COUNT(*) FROM t GROUP BY p ORDER BY COUNT(*) DESC"
    # fail-safe no-ops: not a breakdown, and a CTE (outer ORDER BY may not see the inner alias)
    assert rank_by_measure("SELECT * FROM t LIMIT 5") == "SELECT * FROM t LIMIT 5"
    assert rank_by_measure("WITH c AS (SELECT a, COUNT(*) n FROM t GROUP BY a) SELECT a FROM c") \
        == "WITH c AS (SELECT a, COUNT(*) n FROM t GROUP BY a) SELECT a FROM c"


def test_retrieve_ranked_shows_the_top_by_measure_not_the_alphabetical_slice():
    eng = SqliteEngine.from_tables({"sales": {"columns": ["product", "amount"],
                                              "rows": [["A", 1], ["B", 2], ["C", 10], ["D", 20]]}})
    store = FederatedSqlStore(
        "s", "bu", "S", "sales", eng,
        sql_generator=lambda q, s: "SELECT product, SUM(amount) AS total FROM sales "
                                   "GROUP BY product ORDER BY product")     # KEY-ordered (the bug)
    plain = [e.content.split(",")[0] for e in store.retrieve(ACCESS, "totals", top_k=2)]
    ranked = [e.content.split(",")[0] for e in store.retrieve_ranked(ACCESS, "totals", top_k=2)]
    # #207 CHANGED THIS EXPECTATION: plain retrieve() used to return the alphabetical head
    # ["product=A", "product=B"] — that WAS the bug, and this line used to assert it. Now both
    # paths rank a key-ordered breakdown, so both show the true top-2.
    assert plain == ["product=D", "product=C"], plain
    assert ranked == ["product=D", "product=C"], ranked


def test_retrieve_and_retrieve_ranked_differ_on_a_DELIBERATE_ordering():
    """The two paths are still not the same, and the difference matters. #207's default only
    fixes the key-ordered accident and otherwise respects the query's own ordering; #232's
    carry-source ranking overrides it, because for a semi-join the top-N by measure IS the
    point (those keys are what the other half gets bound to)."""
    eng = SqliteEngine.from_tables({"sales": {"columns": ["product", "amount"],
                                              "rows": [["A", 1], ["B", 2], ["C", 10], ["D", 20]]}})
    store = FederatedSqlStore(
        "s", "bu", "S", "sales", eng,
        sql_generator=lambda q, s: "SELECT product, SUM(amount) AS total FROM sales "
                                   "GROUP BY product ORDER BY total ASC")   # deliberate: smallest first
    plain = [e.content.split(",")[0] for e in store.retrieve(ACCESS, "totals", top_k=2)]
    ranked = [e.content.split(",")[0] for e in store.retrieve_ranked(ACCESS, "totals", top_k=2)]
    assert plain == ["product=A", "product=B"], plain       # ASC respected — intent preserved
    assert ranked == ["product=D", "product=C"], ranked     # carry source still forces top-by-measure


def test_executor_ranks_a_carry_source_only_when_the_store_supports_it():
    r = _RankableStore("tickets", "support")
    execute(_catalog(r), _decision(r), "u1", "q", rank_source=True)
    assert r.ranked is True                                     # rankable store -> retrieve_ranked
    # rank_source but the store has no retrieve_ranked (doc/indexed) -> plain retrieve, never errors
    plain = _FakeStore("hr", "hr", [Evidence("hr", "hr", CHUNK, "c", provenance={"total_rows": 1})])
    rep = execute(_catalog(plain), _decision(plain), "u1", "q", rank_source=True)
    assert any(o.status == OK for o in rep.outcomes), rep.outcomes
    # a non-carry-source half is NOT ranked, even if it could be
    r2 = _RankableStore("tickets", "support")
    execute(_catalog(r2), _decision(r2), "u1", "q", rank_source=False)
    assert r2.ranked is False


# ── the service: the two halves actually line up end-to-end ───────────────────────────────

class _AnswerLlm:
    def answer(self, question, context):
        return {"answer": "ok", "context": context}


def _sql_store(sid, bu, desc, table, gen_sql, rows, cols):
    engine = SqliteEngine.from_tables({table: {"columns": cols, "rows": rows}})
    return FederatedSqlStore(sid, bu, sid, desc, engine, sql_generator=lambda q, s: gen_sql)


def test_service_chains_half_a_shown_keys_into_half_b_semijoin():
    """End to end: 'tickets vs revenue by product' - half A (tickets) shows its top products by
    COUNT; half B (revenue) is then computed for EXACTLY those products, not its own disjoint
    alphabetical slice. This is the whole point of #219."""
    tickets = _sql_store(
        "tickets", "support", "support tickets issues incidents complaints raised counts",
        "tickets",
        "SELECT product_number, COUNT(*) AS ticket_count FROM tickets "
        "GROUP BY product_number ORDER BY ticket_count DESC",
        rows=[["P1"], ["P1"], ["P1"], ["P2"], ["P2"], ["P3"]],   # P1=3, P2=2, P3=1
        cols=["product_number"])
    revenue = _sql_store(
        "revenue", "sales", "revenue sales orders money earned takings receipts",
        "orders",
        "SELECT product_number, SUM(line_total) AS revenue FROM orders "
        "GROUP BY product_number ORDER BY revenue DESC",
        rows=[["P1", 100], ["P2", 50], ["P3", 25], ["P4", 999], ["P4", 999]],
        cols=["product_number", "line_total"])

    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind="tenant", parent_id=None, acl=["p"]))
    for s in (tickets, revenue):
        cat.register(CatalogNode(id=s._store_id, kind=STORE, parent_id="t", acl=["p"],
                                 profile=s.profile(), store=s))
    identity = InMemoryIdentity({"u1": ["p"]})

    def decomposer(q):
        if "versus" in q or " vs " in q:
            return ["support tickets incidents by product", "revenue takings by product"]
        return [q]

    svc = RouterQueryService(cat, identity, HashingEmbedding(), decomposer=decomposer)
    result = svc.ask("u1", "support tickets versus revenue by product", _AnswerLlm())

    # the halves routed to different stores (compound), and BOTH answered
    answered = {o["store_id"] for o in result.outcomes if o["status"] == OK}
    assert answered == {"tickets", "revenue"}, result.outcomes

    # THE ASSERTION: revenue's executed query was a semi-join bound to tickets' shown products.
    bound_sql = revenue.audit_trail[-1]["sql"]
    assert "_semi WHERE product_number IN (" in bound_sql, bound_sql
    for p in ("P1", "P2", "P3"):
        assert f"'{p}'" in bound_sql, (p, bound_sql)
    assert "'P4'" not in bound_sql, bound_sql            # P4 has NO tickets -> not carried

    # and the revenue evidence therefore covers only the products that HAVE tickets
    rev_products = {e["content"].split(",")[0] for e in result.evidence
                    if e["store_id"] == "revenue"}
    assert rev_products == {"product_number=P1", "product_number=P2", "product_number=P3"}, \
        rev_products
    assert "product_number=P4" not in rev_products, rev_products


def test_service_measure_orders_the_carry_source_half_not_the_last():
    """#232: half A (the carry source) is re-ordered by the MEASURE so the keys it hands to half B
    are its top-N by count, not an alphabetical slice. The last half is bound, never measure-ranked
    by this path."""
    tickets = _sql_store(
        "tickets", "support", "support tickets incidents complaints raised counts", "tickets",
        "SELECT product_number, COUNT(*) AS ticket_count FROM tickets "
        "GROUP BY product_number ORDER BY product_number",           # KEY-ordered (the bug)
        rows=[["P1"], ["P1"], ["P2"], ["P3"], ["P3"], ["P3"]], cols=["product_number"])
    revenue = _sql_store(
        "revenue", "sales", "revenue sales orders money earned takings receipts", "orders",
        "SELECT product_number, SUM(line_total) AS revenue FROM orders "
        "GROUP BY product_number ORDER BY product_number",
        rows=[["P1", 100], ["P2", 50], ["P3", 25]], cols=["product_number", "line_total"])
    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind="tenant", parent_id=None, acl=["p"]))
    for s in (tickets, revenue):
        cat.register(CatalogNode(id=s._store_id, kind=STORE, parent_id="t", acl=["p"],
                                 profile=s.profile(), store=s))

    def decomposer(q):
        return (["support tickets incidents by product", "revenue takings by product"]
                if "versus" in q else [q])

    svc = RouterQueryService(cat, InMemoryIdentity({"u1": ["p"]}), HashingEmbedding(),
                             decomposer=decomposer)
    svc.ask("u1", "support tickets versus revenue by product", _AnswerLlm())

    # half A (tickets, carry source) executed the MEASURE-ordered query, not the key-ordered one
    assert tickets.audit_trail[-1]["sql"].endswith("ORDER BY ticket_count DESC"), \
        tickets.audit_trail[-1]["sql"]
    # half B (revenue, last) is the bound semi-join, not measure-ranked by #232
    assert "_semi WHERE product_number IN (" in revenue.audit_trail[-1]["sql"], \
        revenue.audit_trail[-1]["sql"]


def test_carried_join_values_pulls_the_shown_keys_from_a_sub_report():
    from dbsearch.router.executor import DispatchReport
    sql = "SELECT product_number, COUNT(*) AS c FROM t GROUP BY product_number"
    rep = DispatchReport()
    rep.evidence_by_store["tickets"] = [
        Evidence("tickets", "support", CHUNK, "product_number=P1, c=3", provenance={"sql": sql}),
        Evidence("tickets", "support", CHUNK, "product_number=P2, c=2", provenance={"sql": sql})]
    # #474: the carry is (column, values) - the NAME travels so a scalar half can bind.
    assert _carried_join_values(rep) == ("product_number", ["P1", "P2"]), \
        _carried_join_values(rep)

    empty = DispatchReport()
    assert _carried_join_values(empty) is None


def main():
    test_sanitizer_drops_every_injection_attempt_and_counts_the_drops()
    test_sanitizer_dedupes_and_caps()
    test_bind_literal_quotes_text_and_leaves_numbers_bare()
    test_groupby_column_strips_the_alias_prefix_and_is_none_without_grouping()
    test_bind_values_from_evidence_reads_the_shown_rows_of_the_grouped_column()
    test_bind_values_from_evidence_returns_nothing_when_half_a_did_not_group()
    test_a_plain_projection_carries_its_column_474_gate_2()
    test_a_distinct_projection_and_an_aliased_projection_carry_too()
    test_a_multi_column_projection_does_not_carry()
    test_a_partial_projection_carry_is_refused_the_adr_0014_cliff()
    test_a_grouped_partial_carry_is_still_allowed_the_219_shape()
    test_retrieve_bound_constrains_the_result_to_the_bound_keys_only()
    test_retrieve_bound_never_sends_a_key_value_to_the_generator_law1()
    test_retrieve_bound_fails_open_when_the_wrapped_query_errors()
    test_retrieve_bound_runs_unbound_when_this_half_has_no_group_key()
    test_retrieve_bound_runs_unbound_and_discloses_when_every_key_was_dropped()
    test_a_scalar_aggregate_binds_directly_when_its_sql_touches_the_key_table()
    test_a_scalar_aggregate_binds_one_hop_away_through_a_shared_key()
    test_a_hallucinated_placeholder_on_the_carry_column_is_neutralized()
    test_a_placeholder_equality_on_the_carry_column_is_neutralized_too()
    test_a_predicate_on_an_UNRELATED_column_is_never_touched()
    test_an_unbindable_scalar_falls_open_unbound_and_disclosed()
    test_a_scalar_bind_sanitizes_its_values_like_every_other_bind()
    test_the_grouped_bind_path_is_unchanged_when_a_column_is_passed()
    test_semijoin_wrap_plain_vs_cte_keeps_the_with_at_the_top()
    test_semijoin_wrap_strips_a_trailing_order_by_illegal_in_a_derived_table()
    test_retrieve_bound_binds_a_cte_query_that_the_naive_wrap_could_not()
    test_executor_binds_a_capable_store_to_the_carried_values()
    test_executor_discloses_a_store_that_cannot_align_or_that_dropped_keys()
    test_executor_without_bind_values_is_byte_identical_to_before()
    test_rank_by_measure_rewrites_a_key_ordered_breakdown()
    test_retrieve_ranked_shows_the_top_by_measure_not_the_alphabetical_slice()
    test_retrieve_and_retrieve_ranked_differ_on_a_DELIBERATE_ordering()
    test_executor_ranks_a_carry_source_only_when_the_store_supports_it()
    test_service_chains_half_a_shown_keys_into_half_b_semijoin()
    test_service_measure_orders_the_carry_source_half_not_the_last()
    test_carried_join_values_pulls_the_shown_keys_from_a_sub_report()
    print("  PASS  #219 federated semi-join: sanitize, wrap, fail-open, executor, service chain")
    print("\n#219 SEMI-JOIN SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
