"""#221/#222 - FederatedSqlStore retrieval wiring: bounded prompts, widen-once, honest
decline, and NO retrieval-induced fabrication.
Run: python3 -m pytest tests/selftest_structured_retrieval.py -q
     PYTHONPATH=src python3 tests/selftest_structured_retrieval.py
"""
import re  # noqa: E402
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import HashingEmbedding  # noqa: E402
from dbsearch.router.store import AccessContext  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    CannotAnswerFromSchema, FederatedSqlStore, SqliteEngine, keyword_sql_generator,
    llm_sql_generator,
)


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return
    except Exception as e:
        assert False, f"expected {exc.__name__}, got {type(e).__name__}: {e}"
    assert False, f"expected {exc.__name__}, nothing raised"


def _wide_engine(n=40):
    tables = {f"noise_{i:03d}": {"columns": ["k", "v"], "rows": []} for i in range(n)}
    tables["sales"] = {"columns": ["region", "amount"],
                       "rows": [["emea", 100], ["apac", 60]]}
    return SqliteEngine.from_tables(tables)


def _store(gen, engine=None, **kw):
    return FederatedSqlStore("s1", "bu", "Sales warehouse", "sales data",
                             engine or _wide_engine(), sql_generator=gen, **kw)


ACCESS = AccessContext(user_oid="u1", principals=[])

# --- AdventureWorksLT, the shape of the live Azure SQL demo database -------------------
# 12 tables, so retrieval_k=8 is genuinely exceeded and the store MUST narrow.
AWLT = {
    "Customer": {"columns": ["CustomerID", "FirstName", "LastName", "CompanyName",
                             "EmailAddress"],
                 "rows": [[1, "Orlando", "Gee", "A Bike Store", "orlando@x.com"],
                          [2, "Keith", "Harris", "Progressive Sports", "keith@x.com"]]},
    "SalesOrderHeader": {"columns": ["SalesOrderID", "CustomerID", "OrderDate",
                                     "TotalDue", "SubTotal"],
                         "rows": [[71774, 1, "2024-06-01", 972.79, 880.35],
                                  [71776, 2, "2024-06-02", 87.09, 78.81]]},
    "SalesOrderDetail": {"columns": ["SalesOrderDetailID", "SalesOrderID", "ProductID",
                                     "OrderQty", "LineTotal"],
                         "rows": [[110562, 71774, 836, 1, 356.90],
                                  [110563, 71776, 822, 2, 178.45]]},
    "Product": {"columns": ["ProductID", "Name", "ProductNumber", "ListPrice",
                            "ProductCategoryID"],
                "rows": [[836, "Touring-2000 Blue", "BK-T44U-60", 1214.85, 18],
                         [822, "ML Road Frame", "FR-R72R-44", 594.83, 18]]},
    "ProductCategory": {"columns": ["ProductCategoryID", "Name",
                                    "ParentProductCategoryID"],
                        "rows": [[18, "Road Bikes", 2], [19, "Touring Bikes", 2]]},
    "ProductModel": {"columns": ["ProductModelID", "Name"],
                     "rows": [[1, "Classic Vest"]]},
    "ProductDescription": {"columns": ["ProductDescriptionID", "Description"],
                           "rows": [[1, "Chromoly steel."]]},
    "ProductModelProductDescription": {
        "columns": ["ProductModelID", "ProductDescriptionID", "Culture"],
        "rows": [[1, 1, "en"]]},
    "Address": {"columns": ["AddressID", "AddressLine1", "City", "StateProvince",
                            "PostalCode"],
                "rows": [[9, "8713 Yosemite Ct.", "Bothell", "Washington", "98011"]]},
    "CustomerAddress": {"columns": ["CustomerID", "AddressID", "AddressType"],
                        "rows": [[1, 9, "Main Office"]]},
    "BuildVersion": {"columns": ["SystemInformationID", "DatabaseVersion",
                                 "VersionDate"],
                     "rows": [[1, "9.04.02.00", "2024-01-01"]]},
    "ErrorLog": {"columns": ["ErrorLogID", "ErrorTime", "ErrorMessage"],
                 "rows": [[1, "2024-01-01", "boom"]]},
}

TOP_CUSTOMERS = "who are our top 5 customers by total due"
TOP_CATEGORY = "which product category has the highest total revenue"

# What a COMPETENT NL2SQL model actually emits for TOP_CUSTOMERS: it joins the two tables
# the question is really about. If retrieval missed either one, validate_sql inside
# llm_sql_generator rejects this - and that rejection is the signal that retrieval failed.
COMPETENT_SQL = (
    "SELECT c.CompanyName, SUM(soh.TotalDue) AS total_due "
    "FROM Customer c JOIN SalesOrderHeader soh ON c.CustomerID = soh.CustomerID "
    "GROUP BY c.CompanyName ORDER BY total_due DESC"
)


class _CompetentLlm:
    """Emits correct, honest SQL over the tables the question is about."""

    def __init__(self, sql=COMPETENT_SQL):
        self.sql = sql
        self.schemas_seen = []

    def generate_sql(self, question, schema):
        self.schemas_seen.append([t["table"] for t in schema])
        return self.sql


def _awlt_store(gen, **kw):
    return FederatedSqlStore("awlt", "sales", "AdventureWorksLT", "sales database",
                             SqliteEngine.from_tables(AWLT), sql_generator=gen, **kw)


def test_generator_receives_bounded_subset_not_the_whole_warehouse():
    seen = {}

    def gen(question, schema):
        seen["n"] = len(schema)
        seen["tables"] = [t["table"] for t in schema]
        return "SELECT SUM(amount) AS total_amount FROM sales"

    _store(gen).retrieve(ACCESS, "total sales amount by region")
    assert seen["n"] <= 16                      # max_tables bound, never 41
    assert "sales" in seen["tables"]


def test_small_schema_is_identity_passthrough():
    eng = SqliteEngine.from_tables({"sales": {"columns": ["region", "amount"],
                                              "rows": [["emea", 1]]}})
    seen = {}

    def gen(question, schema):
        seen["tables"] = [t["table"] for t in schema]
        return "SELECT * FROM sales LIMIT 5"

    _store(gen, engine=eng).retrieve(ACCESS, "show sales")
    assert seen["tables"] == ["sales"]           # exactly today's behavior


def test_widen_once_then_decline():
    calls = []

    def gen(question, schema):
        calls.append(len(schema))
        raise CannotAnswerFromSchema("not here")

    _raises(CannotAnswerFromSchema, lambda: _store(gen).retrieve(
        ACCESS, "total sales amount by region"))
    assert len(calls) == 2                       # first pass + ONE widen, never a loop
    # #222 Fix 5: the retry must see STRICTLY MORE tables. `>=` was satisfied by 1 >= 1,
    # and it was: `widen_k` raised k from 8 to 30, but k was never the binding constraint
    # (SchemaIndex's relative floor_frac cut first), so both passes returned the IDENTICAL
    # set and "widening" was a no-op. A reviewer deleted the widen entirely and the suite
    # stayed green. It cannot now.
    assert calls[1] > calls[0], calls


def test_first_pass_success_does_not_widen():
    calls = []

    def gen(question, schema):
        calls.append(1)
        return "SELECT SUM(amount) AS total_amount FROM sales"

    _store(gen).retrieve(ACCESS, "total sales amount")
    assert len(calls) == 1


def test_irrelevant_question_declines_without_generating():
    """Retrieval floor: nothing relevant -> the generator must never run against a
    guessed subset (spec hole 7 - retrieval-induced fabrication).

    #222: this test used to pass `embedder=HashingEmbedding(dim=4096)`, with a docstring
    explaining that the 128-dim default hash-collides an irrelevant question into a nonzero
    score. That override was not dead weight - it was LOAD-BEARING, and it was the proof:
    at the shipped default this exact fixture DID NOT DECLINE and the generator ran. A
    decline test that only passes on an embedder the product does not use is not testing
    the product. The override is gone; the store now runs on its real lazy default, and
    the decline is real.
    """
    def gen(question, schema):                   # pragma: no cover - must not be called
        raise AssertionError("generator ran against a guessed subset")

    _raises(CannotAnswerFromSchema, lambda: _store(gen).retrieve(
        ACCESS, "zzqx wobble frequency of the flux capacitor please"))


# --- warehouse scale: 160 tables, where the 12-table fixture was hiding the leak --------
_DOMAINS = ("sales", "customer", "product", "inventory", "shipment", "invoice",
            "payment", "employee", "payroll", "vendor", "purchase", "warehouse",
            "ledger", "budget", "forecast", "campaign")
_SUFFIXES = ("header", "detail", "history", "summary", "archive", "staging",
             "audit", "lookup", "bridge", "snapshot")


def _warehouse_engine():
    """16 domains x 10 suffixes = 160 tables, the shape of a real warehouse. The 12-table
    AdventureWorksLT fixture is what HID the collision leak: a bag-of-words embedder's
    hash-collision floor RISES with table count, so the decline threshold gets easier to
    clear the bigger the schema - i.e. exactly backwards."""
    tables = {}
    for d in _DOMAINS:
        for sfx in _SUFFIXES:
            tables[f"{d}_{sfx}"] = {
                "columns": [f"{d}_id", f"{d}_{sfx}_key", "created_at", "amount"],
                "rows": []}
    return SqliteEngine.from_tables(tables)


# Four with NO lexical anchor at all - not one content word appears anywhere in the 160
# tables. If these do not decline it is the SEPARATION signal failing, not the anchor.
_NO_ANCHOR_QUESTIONS = (
    "zzqx wobble frequency of the flux capacitor",
    "what is the airspeed velocity of an unladen swallow",
    "what is the weather in Reykjavik tomorrow",   # NB: not "forecast" - a real domain here
    "summarize the resignation letter written by our chief executive",
)


def test_warehouse_scale_irrelevant_questions_decline_at_the_shipped_default():
    """#222 IMPORTANT-1. At 160 tables and the SHIPPED default embedder, an irrelevant
    question must still decline without the generator ever running. Measured before the
    fix: 0 of these declined - the 128-bucket lazy default invented enough collision
    signal to clear `baseline + 0.15 * (1 - baseline)` on every one of them."""
    def gen(question, schema):                   # pragma: no cover - must not be called
        raise AssertionError(f"generator ran on a guessed subset for: {question!r}")

    engine = _warehouse_engine()
    assert len(engine.schema()) == 160, len(engine.schema())
    for question in _NO_ANCHOR_QUESTIONS:
        store = _store(gen, engine=engine)
        _raises(CannotAnswerFromSchema, lambda q=question: store.retrieve(ACCESS, q))


def _subset_seen(question, embedder=None):
    """The tables actually handed to the generator for `question` at warehouse scale."""
    seen = {}

    def gen(q, schema):
        seen["tables"] = [t["table"] for t in schema]
        return "SELECT * FROM " + schema[0]["table"]

    kw = {"embedder": embedder} if embedder is not None else {}
    _store(gen, engine=_warehouse_engine(), **kw).retrieve(ACCESS, question)
    return set(seen["tables"])


def test_warehouse_scale_relevant_questions_still_retrieve():
    """The other half, and the thing that makes the decline honest rather than a store that
    simply refuses everything: a question that genuinely names this warehouse's domains
    must still REACH the generator, with the tables it named.

    This is also the zero-recall-cost proof for raising the lazy default to dim=4096: the
    subset retrieved at 4096 is byte-for-byte the subset retrieved at 128. The extra
    buckets removed collisions (which only ever ADDED junk); they took no signal away."""
    for question, needed in (
            ("total invoice amount by vendor", {"invoice_header"}),
            ("shipment history for each purchase order", {"shipment_history",
                                                          "purchase_header"})):
        got = _subset_seen(question)                        # SHIPPED default embedder
        assert needed <= got, (question, sorted(needed - got), sorted(got))
        assert len(got) <= 16, len(got)                     # still BOUNDED at 160 tables
        # raising the default dimension cost NOTHING: same tables as the old 128-dim one
        assert got == _subset_seen(question, HashingEmbedding(dim=128)), question


def test_keyword_only_store_declines_instead_of_guessing_a_table():
    """#222 IMPORTANT-2. router_api explicitly supports a deployment with NO chat model
    ("absent the capability, SQL stores keep the deterministic default"). There, the
    keyword generator IS the NL2SQL layer - no model can say CANNOT_ANSWER, so the
    retrieval floor is the only guard. Measured on such a store before the fix:

        "how many HR employees are on parental leave" -> SELECT COUNT(*) FROM ProductModel
        "which products have the most support tickets" -> SELECT * FROM Product LIMIT 5

    A confident, cited number about nothing. In retrieval mode the keyword generator is
    now forbidden outright: a store that narrowed and has no LLM must DECLINE."""
    store = FederatedSqlStore("kw", "bu", "AdventureWorksLT", "sales database",
                              SqliteEngine.from_tables(AWLT))   # NO sql_generator -> keyword
    assert store._gen is keyword_sql_generator                  # the no-chat-model config
    for question in ("how many HR employees are on parental leave",
                     "which products have the most support tickets"):
        _raises(CannotAnswerFromSchema, lambda q=question: store.retrieve(ACCESS, q))
    assert store.audit_trail == []               # nothing executed, nothing cited


def test_keyword_generator_still_serves_the_identity_path():
    """The other half of IMPORTANT-2, and the invariant: the keyword generator is banned
    only where the store NARROWED. At small N the caller sees the whole schema and the
    naive query is exactly what the five demo stores advertise - byte-identical."""
    eng = SqliteEngine.from_tables({"sales": {"columns": ["region", "amount"],
                                              "rows": [["emea", 100]]}})
    store = FederatedSqlStore("demo", "bu", "Sales", "sales", eng)   # keyword default
    assert store._gen is keyword_sql_generator
    evidence = store.retrieve(ACCESS, "total sales amount by region")
    executed = store.audit_trail[-1]["sql"]
    # The invariant this test exists for: the generated query is still byte-identical to what
    # the demo stores advertise — it survives untouched as an exact PREFIX.
    assert executed.startswith(
        "SELECT region, SUM(amount) AS total_amount FROM sales GROUP BY region"), executed
    # #207 appends a measure ordering before running it: the result is truncated to top_k, and
    # an arbitrary sample of a breakdown is useless. Generated and executed are deliberately no
    # longer the same string — the augmentation is a layer over the generator, not a rewrite of it.
    assert executed.endswith("ORDER BY total_amount DESC"), executed
    assert evidence and evidence[0].content == "region=emea, total_amount=100"


def test_validate_sql_uses_the_retrieved_subset_not_the_full_schema():
    """The safety property (#221 task brief): validate_sql's table allowlist must be
    the SAME narrowed subset handed to the generator, not the full 41-table warehouse.
    A generator that references a table OUTSIDE the retrieved subset - even one that
    genuinely exists elsewhere in the schema - must be rejected."""
    def gen(question, schema):
        # sales is not in this subset's tables the generator is fabricating a
        # reference to a real-but-not-retrieved table.
        return "SELECT * FROM noise_000"

    store = _store(gen)
    # noise_000 legitimately exists in the engine's full schema, so if validate_sql
    # were (wrongly) checking against the full schema this SQL would pass. It must
    # instead be checked against the retrieved subset, and noise_000 was not seeded
    # by a "total sales amount by region" question (sales dominates the ranking),
    # so this raises ValueError from validate_sql, not CannotAnswerFromSchema.
    try:
        store.retrieve(ACCESS, "total sales amount by region")
        assert False, "expected ValueError from validate_sql"
    except ValueError:
        pass
    except CannotAnswerFromSchema:
        assert False, "widened and declined instead of validating against the subset"


def test_retrieval_miss_never_fabricates_a_keyword_guess():
    """#222 Fix 1 - THE regression. This is a REAL, reproduced fabrication, not a
    hypothetical.

    Ask a live AdventureWorksLT store "who are our top 5 customers by total due" with a
    COMPETENT model wired in (it emits the correct Customer x SalesOrderHeader join). At
    8a9c2fd the chain was:

      retrieval (schema narrowed to 8 of 12) MISSES Customer and SalesOrderHeader
        -> validate_sql inside llm_sql_generator rejects the model's correct SQL
        -> `except Exception` swallows it
        -> keyword_sql_generator emits `SELECT SUM(SalesOrderDetailID) AS
           total_SalesOrderDetailID FROM SalesOrderDetail` over the top-ranked NOISE table
        -> the NARROWED allowlist blesses it (the noise table is in the allowlist)
        -> the user gets a number that is the sum of a PRIMARY KEY, labelled "total",
           with a confident citation.

    The store's only honest options are: answer with SQL that really is about the
    question's tables, or decline. It must NEVER answer from a table it merely guessed.
    """
    llm = _CompetentLlm()
    store = _awlt_store(llm_sql_generator(llm))
    try:
        evidence = store.retrieve(ACCESS, TOP_CUSTOMERS)
    except CannotAnswerFromSchema:
        return                              # an honest decline is an acceptable outcome
    sql = store.audit_trail[-1]["sql"]
    # the fabrication, named exactly: an aggregate over the noise table's primary key
    assert not re.search(r"SUM\s*\(\s*\w*\.?\s*SalesOrderDetailID\s*\)", sql, re.I), sql
    assert "SalesOrderDetail" not in sql, sql
    # ...and what it DID answer is genuinely about the question's tables
    assert "Customer" in sql and "SalesOrderHeader" in sql, sql
    assert evidence and "total_due" in evidence[0].content, evidence


def test_retrieval_recall_on_adventureworkslt_under_the_default_embedder():
    """#222 Fix 3 - what the subword split buys. Under the DEFAULT (lazy HashingEmbedding)
    embedder, the tables a question is really about must actually be RETRIEVED.

    Before the split, `"SalesOrderHeader".lower()` was the single token
    `salesorderheader`, which shares nothing with the question word "sales"; and
    "customers" could not match "Customer" (no stemming). The ranking was therefore hash
    noise: measured at 8a9c2fd, TOP_CUSTOMERS retrieved ONLY `SalesOrderDetail` - the
    wrong table, and the one the fabrication above was built on.
    """
    for question, needed in ((TOP_CUSTOMERS, {"Customer", "SalesOrderHeader"}),
                             (TOP_CATEGORY, {"Product", "ProductCategory"})):
        seen = {}

        def gen(q, schema, _seen=seen):
            _seen["tables"] = [t["table"] for t in schema]
            return "SELECT * FROM " + schema[0]["table"]

        _awlt_store(gen).retrieve(ACCESS, question)
        got = set(seen["tables"])
        assert needed <= got, (question, sorted(needed - got), sorted(got))
        assert len(got) <= 16, len(got)          # still BOUNDED, not "retrieve it all"


def test_narrowed_generation_failure_declines_and_never_calls_the_keyword_fallback():
    """#222 Fix 1, isolated. In RETRIEVAL MODE a generation failure is EVIDENCE OF A
    RETRIEVAL MISS - the highest-quality signal available that the subset was wrong - so
    it must reach the widen/decline machinery. The keyword fallback must not run at all:
    a `SELECT ... FROM schema[0]` over the top-ranked table is a guess, and a guess that
    the narrowed allowlist then blesses is precisely the #211 fabrication class."""
    fallback_calls = []

    def spy_fallback(question, schema):
        fallback_calls.append([t["table"] for t in schema])
        return keyword_sql_generator(question, schema)

    class _BrokenLlm:
        def generate_sql(self, question, schema):
            return "SELECT * FROM table_that_does_not_exist"

    store = _store(llm_sql_generator(_BrokenLlm(), fallback=spy_fallback))
    _raises(CannotAnswerFromSchema,
            lambda: store.retrieve(ACCESS, "total sales amount by region"))
    assert fallback_calls == [], fallback_calls
    assert store.audit_trail == []               # nothing was executed, nothing cited


def test_identity_path_still_falls_back_to_the_keyword_generator():
    """The other half of Fix 1: the keyword fallback is FORBIDDEN only when the store
    narrowed. At small N the full schema passes through, nothing was retrieved, so a
    generation failure is NOT evidence of a retrieval miss - and the five demo stores
    depend on degrading to the naive query instead of erroring. Byte-identical to
    pre-#221 behavior."""
    fallback_calls = []

    def spy_fallback(question, schema):
        fallback_calls.append([t["table"] for t in schema])
        return keyword_sql_generator(question, schema)

    class _BrokenLlm:
        def generate_sql(self, question, schema):
            return "SELECT * FROM table_that_does_not_exist"

    eng = SqliteEngine.from_tables({"sales": {"columns": ["region", "amount"],
                                              "rows": [["emea", 100]]}})
    store = _store(llm_sql_generator(_BrokenLlm(), fallback=spy_fallback), engine=eng)
    evidence = store.retrieve(ACCESS, "show sales")
    assert fallback_calls == [["sales"]], fallback_calls      # it DID degrade
    assert store.audit_trail[-1]["sql"] == "SELECT * FROM sales LIMIT 5"
    assert evidence and evidence[0].content == "region=emea, amount=100"


def test_every_sql_provider_threads_the_edition_embedder_into_the_store():
    """#222 Fix 2: all 7 SQL providers built FederatedSqlStore WITHOUT `embedder=`, so the
    #221 schema index silently fell back to a 128-dim lexical HashingEmbedding in EVERY
    deployment - the edition's real semantic embedder never reached table retrieval, only
    the document rail (LocalIndexProvider, #143). Prove each provider passes it through,
    and that None still means the lazy default (the demo/test path relies on that)."""
    from dbsearch.router.providers.azure_sql import AzureSqlProvider
    from dbsearch.router.providers.bigquery import BigQueryProvider
    from dbsearch.router.providers.databricks import DatabricksProvider
    from dbsearch.router.providers.mysql import MySqlProvider
    from dbsearch.router.providers.postgres import PostgresProvider
    from dbsearch.router.providers.redshift import RedshiftProvider
    from dbsearch.router.providers.synapse import SynapseProvider
    from dbsearch.router.structured import CsvSqlProvider

    sentinel = HashingEmbedding(dim=777)         # identifiable: not the 128-dim default
    engine = SqliteEngine.from_tables({"t": {"columns": ["a"], "rows": []}})
    config = {"id": "s", "tables": {"t": {"columns": ["a"], "rows": []}}}

    csv_store = CsvSqlProvider(embedder=sentinel).build(config)
    assert csv_store._embedder is sentinel
    assert CsvSqlProvider().build(config)._embedder is None      # lazy default preserved

    cloud = (AzureSqlProvider, SynapseProvider, PostgresProvider, MySqlProvider,
             BigQueryProvider, RedshiftProvider, DatabricksProvider)
    assert len(cloud) == 7                       # every SQL provider on the pushdown rail
    for cls in cloud:
        provider = cls(embedder=sentinel, engine_factory=lambda _c, _e=engine: _e)
        assert provider.build(config)._embedder is sentinel, cls.__name__
        plain = cls(engine_factory=lambda _c, _e=engine: _e)
        assert plain.build(config)._embedder is None, cls.__name__


def test_composed_router_api_gives_every_sql_provider_the_edition_embedder():
    """Fix 2, at the WIRING level: the provider accepting an embedder is worthless if
    router_api never hands it one - which is exactly what was wrong (it passed
    edition.embedder to LocalIndexProvider ONLY). Drive the real _State constructor and
    assert every registered SQL provider actually holds the edition's embedder."""
    try:
        from dbsearch.server.router_api import _State
    except ImportError:                          # fastapi not installed (repo pattern)
        print("  SKIP  fastapi not installed (pip install '.[server]') - provider-level "
              "threading is proven by the test above")
        return

    sentinel = HashingEmbedding(dim=777)
    state = _State(embedder=sentinel)
    # every kind that builds a FederatedSqlStore, named explicitly - a provider added to
    # the SQL rail later without its embedder must break this list, not slip past it
    sql_kinds = ("csv", "azure_sql", "synapse", "postgres", "mysql", "bigquery",
                 "redshift", "databricks")
    assert len(sql_kinds) == 8
    for kind in sql_kinds:
        provider = state.registry.get(kind)
        assert provider._embedder is sentinel, kind


def test_rerun_sql_still_validates_against_the_full_schema():
    """rerun_sql is a DIFFERENT code path (server-issued proof re-run) and must stay
    validating against the FULL schema, never narrowed - #221 explicitly leaves it
    alone. Prove a table outside any small retrieved subset, but present in the full
    41-table warehouse, is still accepted by rerun_sql."""
    store = _store(lambda q, s: "SELECT * FROM sales")
    cols, rows, total = store.rerun_sql(ACCESS, "SELECT * FROM noise_003")
    assert cols == ["k", "v"]
    assert rows == []
    assert total == 0


def test_a_low_dim_hashing_embedder_is_upgraded_for_the_schema_index():
    """#225: the SERVER threads its edition embedder into every SQL store (#222 Fix 2), and the
    self-host edition's embedder is HashingEmbedding() - the 128-dim default. That ARGUMENT
    silently overrode the dim=4096 DEFAULT, so the two fixes cancelled out and production ran the
    noisy index. Caught live on the canvas: on real AdventureWorks, "who are our top 5 customers
    by total due" MISSED SalesLT.Customer at dim=128 and retrieved it at dim=4096.
    A default is bypassed by an argument; a requirement is not."""
    from dbsearch.adapters.local import HashingEmbedding
    from dbsearch.router.structured import MIN_SCHEMA_HASHING_DIM, schema_index_embedder

    assert schema_index_embedder(HashingEmbedding(dim=128)).dim == MIN_SCHEMA_HASHING_DIM
    assert schema_index_embedder(None).dim == MIN_SCHEMA_HASHING_DIM

    already = HashingEmbedding(dim=MIN_SCHEMA_HASHING_DIM)
    assert schema_index_embedder(already) is already        # no needless rebuild

    class _Dense:                       # a dense embedder is not a hash table: leave it alone
        dim = 1536                      # small dim, but LEARNED features, not buckets
        def embed(self, texts):
            return [[0.0] * self.dim for _ in texts]
    dense = _Dense()
    assert schema_index_embedder(dense) is dense


def test_store_given_the_editions_128_dim_embedder_still_indexes_at_full_capacity():
    """The end-to-end shape of #225: build the store the way router_api does - PASSING the
    edition's 128-dim embedder - and assert the schema index it actually builds is not 128."""
    from dbsearch.adapters.local import HashingEmbedding
    from dbsearch.router.structured import MIN_SCHEMA_HASHING_DIM

    store = FederatedSqlStore("s1", "bu", "Sales warehouse", "sales data", _wide_engine(40),
                              sql_generator=lambda q, s: "SELECT SUM(amount) AS t FROM sales",
                              embedder=HashingEmbedding(dim=128))      # what the server passes
    store.retrieve(ACCESS, "total sales amount by region")
    assert store._embedder.dim == MIN_SCHEMA_HASHING_DIM, store._embedder.dim


def main():
    print("Structured retrieval self-test:")
    test_generator_receives_bounded_subset_not_the_whole_warehouse()
    test_small_schema_is_identity_passthrough()
    print("  PASS  bounded subset handed to generator / small-schema identity passthrough")
    test_widen_once_then_decline()
    test_first_pass_success_does_not_widen()
    print("  PASS  widen-once-then-decline / success on first pass never widens")
    test_irrelevant_question_declines_without_generating()
    test_a_low_dim_hashing_embedder_is_upgraded_for_the_schema_index()
    test_store_given_the_editions_128_dim_embedder_still_indexes_at_full_capacity()
    print("  PASS  irrelevant question declines without ever calling the generator")
    test_validate_sql_uses_the_retrieved_subset_not_the_full_schema()
    print("  PASS  validate_sql checks the RETRIEVED subset, not the full schema")
    test_rerun_sql_still_validates_against_the_full_schema()
    print("  PASS  rerun_sql left alone: still validates against the full schema")
    test_retrieval_miss_never_fabricates_a_keyword_guess()
    print("  PASS  #222 a retrieval miss NEVER becomes SUM(SalesOrderDetailID) from a "
          "guessed noise table (the reproduced fabrication)")
    test_retrieval_recall_on_adventureworkslt_under_the_default_embedder()
    print("  PASS  #222 subword split: AdventureWorksLT questions RETRIEVE the tables "
          "they need under the default embedder")
    test_narrowed_generation_failure_declines_and_never_calls_the_keyword_fallback()
    test_identity_path_still_falls_back_to_the_keyword_generator()
    print("  PASS  #222 narrowed generation failure -> decline (no keyword guess); "
          "identity path still degrades to the keyword generator")
    test_every_sql_provider_threads_the_edition_embedder_into_the_store()
    test_composed_router_api_gives_every_sql_provider_the_edition_embedder()
    print("  PASS  #222 the edition's embedder reaches the schema index in all 8 SQL "
          "providers, and router_api really hands it over")
    test_warehouse_scale_irrelevant_questions_decline_at_the_shipped_default()
    test_warehouse_scale_relevant_questions_still_retrieve()
    print("  PASS  #222 at 160 tables and the SHIPPED default embedder: irrelevant "
          "questions decline (incl. 4 with no lexical anchor), relevant ones still "
          "retrieve - zero recall cost")
    test_keyword_only_store_declines_instead_of_guessing_a_table()
    test_keyword_generator_still_serves_the_identity_path()
    print("  PASS  #222 a keyword-only store (no chat model) DECLINES instead of guessing "
          "a table; the identity path still serves the naive query")
    print("\nSTRUCTURED-RETRIEVAL SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
