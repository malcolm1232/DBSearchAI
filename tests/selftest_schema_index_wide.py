"""#221 wide-schema proof (unit tier): 1,200 tables, decoys, junctions - recall@K,
bounded prompts, decoy demotion, decline-not-fabricate. This is the success bar locked at
design time: unit fixtures over a handful of tables do not prove the feature, because a
handful of tables never exercises the connectivity prior, the max_fanout selectivity cap,
or the decline floor's separation signal the way a real warehouse does. The live-engine
tier (a real cloud database at this scale) is a later task; this file is the in-memory
proof that the mechanism itself holds at scale, not just on a toy schema.

Run: PYTHONPATH=src python3 tests/selftest_schema_index_wide.py
     python3 -m pytest tests/selftest_schema_index_wide.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import HashingEmbedding  # noqa: E402
from dbsearch.router.profiles import cosine  # noqa: E402
from dbsearch.router.schema_index import SchemaIndex, normalize_text  # noqa: E402
from dbsearch.router.store import AccessContext  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    CannotAnswerFromSchema, FederatedSqlStore, SqliteEngine,
)


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return
    except Exception as e:
        assert False, f"expected {exc.__name__}, got {type(e).__name__}: {e}"
    assert False, f"expected {exc.__name__}, nothing raised"


NOISE_COUNT = 1193     # + 4 entity tables + 3 decoys = 1,200


def _wide_schema() -> list:
    """1,200 tables: 1,193 staging/noise tables that all share only a generic
    high-fanout column (batch_id - capped by max_fanout, #221 Task 1, so they infer NO
    edges among themselves and cannot invert the connectivity prior), a real connected
    entity cluster (orders / order_details / products / customers), and three DECOYS
    that cosine-match the 'orders' vocabulary but carry ZERO join edges - the class of
    look-alike table (a `_backup`, a `_scratch`, a reporting extract) every real
    warehouse accumulates, and that must never outrank the connected original.

    The decoys' columns are deliberately built so `infer_edges`' name+type match cannot
    connect them to the real cluster either: `orders_2023_backup.order_total` and
    `order_summary_v2.order_id` reuse real column NAMES but with a mismatched TYPE
    (varchar instead of decimal/int), which is exactly the shape a stale export or a
    reporting flatten takes in practice.
    """
    schema = [{"table": f"stg.load_{i:04d}",
               "columns": [{"name": "batch_id", "type": "int"},
                           {"name": "payload", "type": "varchar"}]}
              for i in range(NOISE_COUNT)]
    schema += [
        {"table": "dw.orders", "columns": [
            {"name": "order_id", "type": "int"},
            {"name": "customer_id", "type": "int"},
            {"name": "order_total", "type": "decimal"},
            {"name": "order_date", "type": "date"}]},
        {"table": "dw.order_details", "columns": [
            {"name": "order_id", "type": "int"},
            {"name": "product_id", "type": "int"},
            {"name": "line_total", "type": "decimal"},
            {"name": "quantity", "type": "int"}]},
        {"table": "dw.products", "columns": [
            {"name": "product_id", "type": "int"},
            {"name": "product_number", "type": "varchar"},
            {"name": "product_name", "type": "varchar"}]},
        {"table": "dw.customers", "columns": [
            {"name": "customer_id", "type": "int"},
            {"name": "customer_name", "type": "varchar"},
            {"name": "region", "type": "varchar"}]},
        # decoys: cosine look-alikes, ZERO join edges (no FK, and no inferred edge -
        # see the type-mismatch note above)
        {"table": "dw.orders_2023_backup", "columns": [
            {"name": "old_order_ref", "type": "varchar"},
            {"name": "order_total", "type": "varchar"}]},        # type-mismatched
        {"table": "tmp.orders_scratch", "columns": [
            {"name": "scratch", "type": "varchar"}]},
        {"table": "rpt.order_summary_v2", "columns": [
            {"name": "order_id", "type": "varchar"},             # type-mismatched
            {"name": "summary_total", "type": "varchar"}]},
    ]
    return schema


FK_EDGES = [("dw.order_details", "dw.orders"), ("dw.order_details", "dw.products"),
            ("dw.orders", "dw.customers")]

# (question, tables that MUST be in the retrieved set). Phrased the way a real user who
# shares vocabulary with these column/table names actually asks - HashingEmbedding is
# purely lexical (subword-normalized, #222 Fix 3), so a golden question has to NAME the
# schema's words, not just gesture at the domain.
GOLDEN = [
    ("total order_total by region for our customers",
     {"dw.orders", "dw.customers"}),
    ("how many orders did each customer place",
     {"dw.orders", "dw.customers"}),
    ("line_total by product_number",
     {"dw.order_details", "dw.products"}),
    ("total order_total per order",
     {"dw.orders"}),
    ("customers by region",
     {"dw.customers"}),
]

# --- build ONCE at module scope --------------------------------------------------------
# 1,200 embeddings is the whole point of the proof, but re-embedding them per golden
# question (build-the-index-inside-the-test, as the original task-6 brief snippet did)
# makes the suite crawl for no reason - the index is immutable once built. Production
# DEFAULT embedder (dim=4096, #222 Fix 5) - NOT overridden. A test that only passes with
# a different dim would be hiding a real defect, not proving the feature works.
_WIDE_SCHEMA = _wide_schema()
_WIDE_INDEX = SchemaIndex(_WIDE_SCHEMA, HashingEmbedding(dim=4096), fk_edges=FK_EDGES)

# retrieve() at PRODUCTION DEFAULTS for every golden question, computed once and shared
# by both the recall test and the bounded-prompt test below (two properties, one pass).
_GOLDEN_RESULTS = [(q, needed, _WIDE_INDEX.retrieve(q)) for q, needed in GOLDEN]


def test_schema_is_genuinely_wide():
    assert len(_WIDE_SCHEMA) == 1200, len(_WIDE_SCHEMA)


def test_recall_finds_the_tables_each_question_needs():
    """RECALL@K: for every golden question, the tables it genuinely needs are in the
    retrieved subset - at PRODUCTION retrieve() defaults (k=8, max_tables=16,
    floor_frac=0.6, margin_frac=0.15, min_cosine=0.02), on a 1,200-table schema."""
    for question, needed, got_tables in _GOLDEN_RESULTS:
        got = {t["table"] for t in got_tables}
        assert needed <= got, (question, sorted(needed - got), sorted(got))


def test_prompt_stays_bounded_regardless_of_warehouse_width():
    """BOUNDED PROMPT: this is the whole point of the feature. A 1,200-table warehouse
    must never reach the SQL generator - or the validate_sql allowlist - as 1,200
    tables. Every golden question's subset stays inside the production cap (16), and in
    this fixture every one of them is genuinely single-digit."""
    for question, needed, got_tables in _GOLDEN_RESULTS:
        assert len(got_tables) <= 16, (question, len(got_tables))       # the hard bound
        assert len(got_tables) < 10, (question, len(got_tables))        # measured: single-digit


def test_decoy_loses_to_the_connected_real_table():
    """DECOY DEMOTION, asserted UNCONDITIONALLY - the earlier plan snippet wrote
    `... if "dw.orders_2023_backup" in names else True`, an assertion that evaporates
    whenever the decoy is not in the list, i.e. it can never fail. A user ruling killed
    that shape; this test names both tables and compares their position for real.

    `dw.orders_2023_backup` is a genuine cosine look-alike for an 'orders' question -
    measured directly below, its RAW cosine is actually HIGHER than the real
    `dw.orders` table's (its columns are short and generic, so the vector inflates less
    on norm). Cosine alone would rank the backup FIRST. It has zero join edges (no FK,
    and its `order_total` column is deliberately typed varchar so `infer_edges`'
    name+type match cannot connect it either), so the connectivity prior stays at
    baseline 1.0 while `dw.orders` - hub of the real entity cluster - is boosted to 2.0.
    That is what must flip the order back.

    Checked two ways:
    1. With `floor_frac=0.0` (the SAME production widen-path parameter, #222 Fix 5 -
       not a test-only hack) so both tables are guaranteed to appear in the ranked
       output and their real ORDER can be compared.
    2. At true production defaults, where the connected real table wins outright: the
       decoy does not even survive the cut.
    """
    question = "total order_total per order"
    qv = _WIDE_INDEX._embedder.embed([normalize_text(question)])[0]
    raw_orders = cosine(qv, _WIDE_INDEX._vectors["dw.orders"])
    raw_backup = cosine(qv, _WIDE_INDEX._vectors["dw.orders_2023_backup"])
    assert raw_backup > raw_orders, (raw_backup, raw_orders)      # genuine look-alike...
    assert _WIDE_INDEX._prior["dw.orders"] > 1.0                  # ...connected...
    assert _WIDE_INDEX._prior["dw.orders_2023_backup"] == 1.0     # ...decoy stays baseline

    ranked = _WIDE_INDEX.retrieve(question, k=20, max_tables=20, floor_frac=0.0)
    names = [t["table"] for t in ranked]
    assert "dw.orders" in names and "dw.orders_2023_backup" in names, names
    assert names.index("dw.orders") < names.index("dw.orders_2023_backup"), names

    default_names = [t["table"] for t in _WIDE_INDEX.retrieve(question)]
    assert "dw.orders" in default_names, default_names
    assert "dw.orders_2023_backup" not in default_names, default_names


# The decline probe. Fixed in advance and NOT hand-picked for passing - selecting the
# subset of irrelevant questions that happen to decline is fixture-shopping, and it turns
# a broken product into a green test. Two shapes, deliberately:
#   (a) NO lexical overlap with the schema at all - if these fail it is the SEPARATION
#       signal in `_declines` failing.
#   (b) a plausible-but-ABSENT business domain (HR, payroll, warranty, marketing) - the
#       realistic #211 shape: a question that SOUNDS like something a warehouse would
#       hold, asked of a warehouse that does not hold it.
# Nothing in the 1,200-table fixture is about any of these: not the entity cluster, not
# the decoys, and not the 1,193 `stg.load_*` staging tables (only `batch_id`/`payload`).
DECLINE_PROBE = (
    # (a) no lexical overlap
    "what is the airspeed velocity of an unladen swallow",
    "summarize the CEO's resignation letter",
    "what is the weather in Reykjavik",
    "zzqx wobble frequency of the flux capacitor",
    "best hiking trails near Seattle",
    # (b) plausible-but-absent business domain
    "average employee salary by department",
    "how many HR employees are on parental leave",
    "warranty claims filed last month",
    "employee performance review scores",
    "marketing campaign click through rate this quarter",
)


def test_absent_domain_declines_not_fabricates_across_the_whole_probe():
    """DECLINE, NOT FABRICATE - asserted on ALL 10 probe questions, not a hand-picked one.

    This is the property the numeric-token rule was added to restore. `stg.load_{i:04d}`
    gives each of the 1,193 noise tables its own UNIQUE numeric token (`0682`, `0931`,
    ...). Before pure-digit tokens were dropped from the vocabulary, those 1,193 unique
    tokens were pure collision fodder for the hashing embedder: an ordinary irrelevant
    English question landed on one by chance, earned a spurious nonzero cosine, cleared
    the separation signal, and the store ANSWERED. Measured on this exact fixture at
    production defaults:

        before the numeric rule:   5 / 10 declined  ("best hiking trails near Seattle"
                                                     retrieved stg.load_0945)
        after  the numeric rule:  10 / 10 declined

    Break `_is_numeric` (make it `return False`) and this test goes red.

    The assertion is on the SchemaIndex prefilter (retrieve() -> []), which is what the
    numeric rule fixes, and it is exhaustive over the probe. The companion test below
    proves that [] really does become an honest CannotAnswerFromSchema through the real
    store, with the generator never reached.
    """
    leaked = []
    for question in DECLINE_PROBE:
        got = [t["table"] for t in _WIDE_INDEX.retrieve(question)]
        if got:
            leaked.append((question, got))
    assert leaked == [], leaked


def test_decline_reaches_the_user_as_a_decline_through_the_real_store():
    """The prefilter's [] is not the guarantee on its own - it has to reach the caller as
    a refusal. Drive the REAL FederatedSqlStore over all 1,200 tables and prove
    CannotAnswerFromSchema propagates and the generator is NEVER CALLED. A #211-class
    fabrication is a confident SELECT over some guessed `stg.load_0731`; the only way to
    prove that cannot happen is to prove the generator never runs at all.

    Driven end to end for two probe questions (one of each shape). It is the store round
    trip that is expensive here - two schema introspections and an index rebuild on the
    widen path, at 1,200 tables - so the exhaustive sweep lives in the test above, which
    covers the same mechanism on the same fixture.
    """
    tables = {}
    for t in _WIDE_SCHEMA:
        tables[t["table"].replace(".", "_")] = {
            "columns": [c["name"] for c in t["columns"]], "rows": []}
    engine = SqliteEngine.from_tables(tables)
    access = AccessContext(user_oid="u", principals=[])

    def gen(question, schema):                # pragma: no cover - must not be called
        raise AssertionError(f"generator ran on a guessed subset for: {question!r}")

    for question in ("what is the airspeed velocity of an unladen swallow",   # shape (a)
                     "average employee salary by department"):                # shape (b)
        store = FederatedSqlStore("dw", "bu", "warehouse", "orders and customers",
                                  engine, sql_generator=gen)
        _raises(CannotAnswerFromSchema,
                lambda q=question: store.retrieve(access, q))
        assert store.audit_trail == [], question   # nothing executed, nothing cited


def main():
    print("Wide-schema self-test (#221, 1,200 tables):")
    test_schema_is_genuinely_wide()
    print("  PASS  fixture really is 1,200 tables (1,193 noise + 4 entity + 3 decoys)")
    test_recall_finds_the_tables_each_question_needs()
    print(f"  PASS  recall@K: all {len(GOLDEN)} golden questions retrieve the tables "
          "they need, at production retrieve() defaults on a 1,200-table schema")
    test_prompt_stays_bounded_regardless_of_warehouse_width()
    print("  PASS  bounded prompt: every retrieved subset stays single-digit and well "
          "under the 16-table cap, regardless of the 1,200-table warehouse behind it")
    test_decoy_loses_to_the_connected_real_table()
    print("  PASS  decoy demotion: dw.orders_2023_backup out-cosines dw.orders on raw "
          "similarity but loses on rank because it has zero join edges - asserted "
          "unconditionally, and it does not even survive the production cut")
    test_absent_domain_declines_not_fabricates_across_the_whole_probe()
    print(f"  PASS  decline-not-fabricate: ALL {len(DECLINE_PROBE)} probe questions "
          "decline (5/10 before the numeric-token rule, 10/10 after) - not a hand-picked "
          "question; the 1,193 numbered staging tables no longer manufacture cosine")
    test_decline_reaches_the_user_as_a_decline_through_the_real_store()
    print("  PASS  the decline REACHES the caller: CannotAnswerFromSchema through the "
          "real FederatedSqlStore; the generator never runs, nothing is cited")
    print("\nWIDE-SCHEMA SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
