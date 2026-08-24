"""Schema index (card #221) - compose-time table embeddings + join graph + retrieval.
Run: python3 -m pytest tests/selftest_schema_index.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import HashingEmbedding  # noqa: E402
from dbsearch.router import (  # noqa: E402
    AzureSqlEngine, BigQueryEngine, CosmosEngine, DatabricksEngine, MySqlEngine,
    PostgresEngine, RedshiftEngine,
)
from dbsearch.router.profiles import cosine  # noqa: E402
from dbsearch.router.schema_index import (  # noqa: E402
    SchemaIndex, _tokens, build_graph, connectivity_prior, infer_edges, normalize_text,
    normalize_tokens, table_embed_text, table_text,
)
from dbsearch.router.structured import SqlEnginePort, SqliteEngine  # noqa: E402

ORDERS = {"table": "dbo.orders", "columns": [
    {"name": "order_id", "type": "int"}, {"name": "customer_id", "type": "int"},
    {"name": "amount", "type": "decimal"}]}
CUSTOMERS = {"table": "dbo.customers", "columns": [
    {"name": "customer_id", "type": "int"}, {"name": "region", "type": "varchar"}]}
BACKUP = {"table": "dbo.sales_2023_backup", "columns": [
    {"name": "sale_id", "type": "int"}, {"name": "amount", "type": "decimal"}]}
SCHEMA = [ORDERS, CUSTOMERS, BACKUP]

ORDER_DETAILS = {"table": "dbo.order_details", "columns": [
    {"name": "order_id", "type": "int"}, {"name": "product_id", "type": "int"},
    {"name": "line_total", "type": "decimal"}]}
PRODUCTS = {"table": "dbo.products", "columns": [
    {"name": "product_id", "type": "int"}, {"name": "product_number", "type": "varchar"},
    {"name": "product_name", "type": "varchar"}]}
TICKETS = {"table": "support.tickets", "columns": [
    {"name": "ticket_id", "type": "int"}, {"name": "product_number", "type": "varchar"},
    {"name": "severity", "type": "varchar"}]}
JOIN_SCHEMA = [ORDERS, CUSTOMERS, ORDER_DETAILS, PRODUCTS, TICKETS, BACKUP]

# --- fixtures that ISOLATE one retrieval mechanism each -------------------------------
# Each of the three below is built so the named mechanism is the ONLY thing that can
# produce the asserted outcome. Verified by mutation: break the mechanism in the source
# and the matching test goes red (see .superpowers/sdd/task-2-report.md).

# (1) path-connection. A LEXICALLY NEUTRAL bridge: `dbo.xk9`'s name and column names
# share NO token with the question, so its cosine is exactly 0.0000 - it can never be a
# seed on its own merit. It is reachable only via FK edges from the two real seeds, so
# the ONLY thing that can put it in the subset is pairwise path connection.
XK9_BRIDGE = {"table": "dbo.xk9", "columns": [
    {"name": "zz1", "type": "int"}, {"name": "zz2", "type": "int"}]}
BRIDGE_SCHEMA = [ORDERS, CUSTOMERS, PRODUCTS, TICKETS, BACKUP, XK9_BRIDGE]
BRIDGE_FK = [("dbo.xk9", "dbo.orders"), ("dbo.xk9", "dbo.products")]

# (3) lexical boost. `dbo.zz_ledger` carries the join key named in the question
# (xk_bolt_key) and NOTHING else the question says, so by cosine alone it scores 0.4903 and
# ranks THIRD - outside k=2. The 1.25x column-name boost lifts it to 0.6129, first place.
# Break the boost (hardcode _lex_boost to 1.0) and it falls back out of the set: the boost is
# decisive, and rank - not the floor - is the gate (it clears the relative floor comfortably
# either way).
#
# The key is spelled `xk_bolt_key`, NOT the original `xk9_key`, because of the numeric-token
# rule (#221 wide-schema proof): a pure-digit token is a partition/version discriminator and
# is now dropped from BOTH sides. `xk9_key` smuggled a third of its distinctiveness through
# the digit `9` - a token the question also carried - so stripping numerics dropped its raw
# cosine 0.4903 -> 0.4364 and the boost stopped being decisive. That is the numeric rule
# working as designed, not a regression in the boost: an identifier's digits must not be what
# makes it findable. `xk_bolt_key` contributes the same THREE tokens (xk/bolt/key) the old key
# contributed (xk/9/key), so the fixture's arithmetic is unchanged and the mechanism this test
# isolates - and every assertion below - is exactly as before.
#
# The two rivals earn their cosine from their table COMMENT, which is embedded but which
# `_lex_boost` never reads - so they score HIGH with a boost of exactly 1.0. That is the
# only way to build this contrast now that #222 Fix 3 splits identifiers into subwords:
# under subword tokenization ANY table whose NAME or COLUMNS lexically overlap the question
# necessarily earns a boost itself, so a rival that is beaten BY the boost cannot get its
# similarity from its name or its columns. (The pre-Fix-3 fixture used `dbo.regional_profit`
# as the unboosted rival, which only worked because `regional_profit` was one opaque token
# that could not match the question word "profit". It matches now - correctly - so the old
# fixture no longer isolates the mechanism it claims to.)
LEX_QUESTION = ("total revenue and profit margin for each region this quarter, "
                "keyed by xk_bolt_key")
LEX_SCHEMA = [
    {"table": "dbo.rollup_q3",
     "comment": "revenue and profit margin by region per quarter",
     "columns": [{"name": "grp_a", "type": "varchar"},
                 {"name": "val_b", "type": "decimal"},
                 {"name": "val_c", "type": "decimal"}]},
    {"table": "dbo.snapshot_b",
     "comment": "revenue profit margin region quarter total figures",
     "columns": [{"name": "grp_d", "type": "varchar"},
                 {"name": "val_e", "type": "decimal"}]},
    {"table": "dbo.wide_dump",
     "comment": "assorted unrelated warehouse staging output",
     "columns": [{"name": "grp_f", "type": "varchar"},
                 {"name": "val_g", "type": "decimal"}]},
    {"table": "dbo.zz_ledger", "columns": [
        {"name": "xk_bolt_key", "type": "varchar"},
        {"name": "amt_raw", "type": "decimal"}]},
]


def _index(schema, fk=None):
    return SchemaIndex(schema, HashingEmbedding(), fk_edges=fk or [])


def test_table_text_names_table_and_columns():
    txt = table_text(ORDERS)
    assert "dbo.orders" in txt and "customer_id" in txt and "amount" in txt


def test_infer_edges_matches_same_name_and_type():
    edges = infer_edges(SCHEMA)
    assert ("dbo.orders", "dbo.customers") in edges or \
           ("dbo.customers", "dbo.orders") in edges
    # 'amount' is decimal in BOTH orders and backup - a name+type match. It is a
    # candidate edge by the rule; the PRIOR is what demotes the backup, not edge absence.
    # But a self-pair or a pair with itself must never appear.
    assert all(a != b for a, b in edges)


def test_infer_edges_requires_type_compatibility():
    a = {"table": "t1", "columns": [{"name": "code", "type": "int"}]}
    b = {"table": "t2", "columns": [{"name": "code", "type": "varchar"}]}
    assert infer_edges([a, b]) == []


def test_build_graph_merges_fk_and_inferred_edges():
    g = build_graph(SCHEMA, fk_edges=[("dbo.orders", "dbo.customers")])
    assert "dbo.customers" in g["dbo.orders"]
    assert "dbo.orders" in g["dbo.customers"]          # undirected
    assert "dbo.sales_2023_backup" in g                 # every table is a node


def test_build_graph_ignores_fk_edges_to_unknown_tables():
    g = build_graph(SCHEMA, fk_edges=[("dbo.orders", "audit.gone")])
    assert "audit.gone" not in g and "audit.gone" not in g["dbo.orders"]


def test_infer_edges_skips_high_fanout_generic_columns():
    """A column name shared by MANY tables (batch_id, id, created_at) is a generic /
    technical column, not a join key. Left uncapped it cliques every staging table
    together and INVERTS the connectivity prior (noise gets max degree, real entity
    tables sit near baseline). Buckets over max_fanout must emit no edges at all."""
    noise = [{"table": f"stg.load_{i:04d}", "columns": [
        {"name": "batch_id", "type": "int"},
        {"name": "payload", "type": "varchar"}]} for i in range(30)]
    assert infer_edges(noise) == []                     # 30 > default cap of 20
    # ...but the cap must DISCRIMINATE, not disable inference: a small bucket still joins.
    real = [{"table": f"dw.t{i}", "columns": [{"name": "customer_id", "type": "int"}]}
            for i in range(3)]
    edges = infer_edges(real)
    assert len(edges) == 3, edges                       # 3 tables -> all 3 pairs
    assert all(a != b for a, b in edges)
    # and a low explicit cap suppresses even that small bucket
    assert infer_edges(real, max_fanout=2) == []


def test_connectivity_prior_not_inverted_by_generic_columns():
    """Regression for the inversion: 30 noise tables sharing batch_id must NOT outrank a
    genuinely connected entity cluster. Uncapped, each noise table has degree ~29 and the
    real tables degree ~1, so the prior ranks the junk highest - the exact reverse of the
    feature's purpose."""
    noise = [{"table": f"stg.load_{i:04d}", "columns": [
        {"name": "batch_id", "type": "int"},
        {"name": "payload", "type": "varchar"}]} for i in range(30)]
    g = build_graph(noise + [ORDERS, CUSTOMERS],
                    fk_edges=[("dbo.orders", "dbo.customers")])
    prior = connectivity_prior(g)
    assert prior["dbo.orders"] > prior["stg.load_0000"], (
        prior["dbo.orders"], prior["stg.load_0000"])
    assert prior["dbo.customers"] > prior["stg.load_0000"]
    assert prior["stg.load_0000"] == 1.0                # generic column -> no edges
    assert all(1.0 <= v <= 2.0 for v in prior.values())


def test_connectivity_prior_boosts_connected_demotes_isolated():
    g = build_graph([ORDERS, CUSTOMERS,
                     {"table": "tmp_orders", "columns": [{"name": "x", "type": "int"}]}],
                    fk_edges=[("dbo.orders", "dbo.customers")])
    prior = connectivity_prior(g)
    assert prior["tmp_orders"] == 1.0                   # zero edges -> baseline
    assert prior["dbo.orders"] > prior["tmp_orders"]    # connected -> boosted
    assert all(1.0 <= v <= 2.0 for v in prior.values())


def test_retrieve_finds_the_named_table():
    subset = _index(JOIN_SCHEMA).retrieve("how many support tickets by severity", k=2)
    assert subset and subset[0]["table"] == "support.tickets"


def test_retrieve_is_ranked_best_first():
    subset = _index(JOIN_SCHEMA).retrieve("customers by region", k=3)
    names = [t["table"] for t in subset]
    assert names[0] == "dbo.customers"


def test_retrieve_connects_seeds_through_junction_tables():
    """orders and products only join THROUGH a bridge table the question never names
    (spec hole 2). Pairwise path connection must pull it in.

    The bridge `dbo.xk9` is LEXICALLY NEUTRAL: it shares no token with the question, so
    its cosine is 0.0 and it can never be a seed on its own merit. It is reachable only
    over FK edges from the two seeds. So its presence in the subset PROVES path
    connection ran - delete that step and this test goes red. This is the mechanism that
    stops the known fabrication class (counting order lines and labelling them
    'support tickets'), so it has to genuinely bite.
    """
    idx = _index(BRIDGE_SCHEMA, BRIDGE_FK)
    subset = idx.retrieve("total order amount by product name", k=3)
    names = [t["table"] for t in subset]
    # the bridge is NOT a seed: zero lexical overlap -> zero cosine
    assert idx._vectors["dbo.xk9"] is not None
    assert cosine(HashingEmbedding().embed(
        ["total order amount by product name"])[0],
        idx._vectors["dbo.xk9"]) == 0.0
    # ...and it is retrieved anyway, which only path connection can do
    assert "dbo.xk9" in names, names
    # the two real seeds are what it bridges
    assert "dbo.orders" in names and "dbo.products" in names, names


def test_retrieve_never_exceeds_max_tables():
    """The cap must actually CUT. In Task 5 this list becomes the SQL guard's table
    ALLOWLIST, so an unbounded list is a safety problem, not just a prompt-size one.

    The fixture keeps each column bucket under Task 1's max_fanout so the tables really
    do all clear the floor (10 candidates), then asks for k=20 with max_tables=5: the
    slice is the only thing standing between 10 and 5. Remove it and this goes red.
    """
    wide = [{"table": f"t{i}", "columns": [{"name": "hub_id", "type": "int"}]}
            for i in range(10)]
    idx = _index(wide)
    pool = idx.retrieve("anything about hub", k=20, max_tables=32)
    capped = idx.retrieve("anything about hub", k=20, max_tables=5)
    assert len(pool) == 10, len(pool)      # the candidate pool genuinely overflows 5...
    assert len(capped) == 5, len(capped)   # ...and the cap cuts it to exactly max_tables


def test_retrieve_empty_when_nothing_relevant():
    """Below the decline floor -> [] -> the caller declines. Never a guessed table.

    #222 Fix 4: this now holds under the DEFAULT parameters. It previously needed
    `min_seed_score=0.4` - a hand-forced floor 8x the 0.05 default - because the real
    default could not decline this question at all: the value being thresholded was
    `cosine x prior x lex_boost`, up to 3x an inflated cosine, and 128-bucket hash
    collisions pushed the junk above 0.05. A decline test that has to override the
    production floor to pass is not testing the production floor.
    """
    subset = _index(JOIN_SCHEMA).retrieve("zzqx wobble frequency of the flux capacitor")
    assert subset == []


def test_retrieve_join_key_lexical_boost():
    """#215: a sub-question that carries the join key must keep the table holding it in
    the set, even when cosine alone ranks that table below the cut.

    Constructed so the boost is DECISIVE, not incidental: by RAW COSINE dbo.zz_ledger
    (0.4903) ranks THIRD, behind dbo.snapshot_b (0.5547) and dbo.rollup_q3 (0.5230), so
    it falls outside k=2. Only the 1.25x column-name boost (it holds xk_bolt_key, which the
    question names) lifts it to 0.6129 and into the set, displacing rollup_q3. Hardcode
    _lex_boost to 1.0 and this test goes red. No OR-escape: every assertion names its
    table unconditionally.
    """
    idx = _index(LEX_SCHEMA)
    names = [t["table"] for t in idx.retrieve(LEX_QUESTION, k=2)]
    assert "dbo.zz_ledger" in names, names
    # the rival that cosine alone would have picked instead is displaced
    assert "dbo.rollup_q3" not in names, names
    # and the boost is what did it: the ledger is the boosted table, the rivals are not
    q_tokens = _tokens(LEX_QUESTION)
    assert idx._lex_boost("dbo.zz_ledger", q_tokens) == 1.25
    assert idx._lex_boost("dbo.rollup_q3", q_tokens) == 1.0
    assert idx._lex_boost("dbo.snapshot_b", q_tokens) == 1.0
    # ...and it really was BEHIND on raw cosine: without the boost it is not in the top 2
    qv = idx._embedder.embed([normalize_text(LEX_QUESTION)])[0]
    raw = sorted(((cosine(qv, idx._vectors[n]), n) for n in idx._by_name), reverse=True)
    assert [n for _, n in raw[:2]] == ["dbo.snapshot_b", "dbo.rollup_q3"], raw


# --- #222 Fix 3: subword normalization -----------------------------------------------

def test_normalize_tokens_splits_camelcase_snake_and_dotted_names():
    """Identifiers must become real WORDS. `HashingEmbedding` is a non-stemming, exact-
    token bag of words, so `SalesOrderHeader`.lower() = the single token
    `salesorderheader` matches the question word "sales" with cosine 0.0."""
    assert normalize_tokens("SalesOrderHeader") == ["sales", "order", "header"]
    assert normalize_tokens("ProductID") == ["product", "id"]
    assert normalize_tokens("HTTPServer") == ["http", "server"]      # acronym run
    assert normalize_tokens("dw.sales_daily") == ["dw", "sales", "daily"]
    assert normalize_tokens("[dbo].[SalesLT]") == ["dbo", "sales", "lt"]
    # NB: the year is GONE - see test_normalize_tokens_drops_pure_numeric_tokens below.
    # This line previously asserted ["sales", "2023", "backup"]; the numeric-token rule is
    # a deliberate spec change, not a regression.
    assert normalize_tokens("sales_2023_backup") == ["sales", "backup"]


def test_normalize_tokens_drops_pure_numeric_tokens():
    """#221 wide-schema proof. A pure-digit token is a PARTITION / VERSION discriminator,
    not vocabulary: `load_0417` says nothing about what the table HOLDS, only which slice
    of an identical family it is.

    This is a HONESTY fix, not a tidy-up. A numbered family of 1,193 staging tables
    contributes 1,193 UNIQUE tokens, which to a hashing embedder is pure collision fodder:
    an irrelevant question lands on one by chance, earns a spurious nonzero cosine, clears
    the separation signal in `_declines`, and the store ANSWERS a question it should have
    declined. Measured on the 1,200-table fixture at production defaults, only 5 of 10
    irrelevant questions declined before this rule; 10 of 10 after. (See
    selftest_schema_index_wide.py, which asserts that rate directly.)

    Applied to BOTH sides - the same both-sides discipline #222 Fix 3 established.
    """
    assert normalize_tokens("stg.load_0417") == ["stg", "load"]
    assert normalize_tokens("events_2024_01") == ["event"]
    assert normalize_tokens("sales_2023_backup") == ["sales", "backup"]
    assert normalize_tokens("Sales2023") == ["sales"]
    # ...but a digit GLUED INSIDE a word is part of the word and must survive: dropping it
    # would destroy the identifier, not clean it up.
    assert normalize_tokens("xk9_key") == ["xk", "key"]      # `9` alone is its own token
    assert normalize_tokens("s3_bucket") == ["s", "bucket"]
    # the accepted trade-off, asserted so nobody "fixes" it back: a question naming a year
    # no longer matches that year in a table name. Both slices of the family retrieve
    # together and the generator picks from the real names it is shown.
    assert normalize_tokens("orders in 2023") == normalize_tokens("orders in 2024")


def test_normalize_tokens_singularizes_without_mangling():
    """Plural question word -> singular table name. The rules must NOT mangle a word whose
    singular already ends in `s`: turning `address` into `addres` would DESTROY the very
    match `addresses -> address` exists to create."""
    assert normalize_tokens("customers") == ["customer"]
    assert normalize_tokens("categories") == ["category"]
    assert normalize_tokens("addresses") == ["address"]
    assert normalize_tokens("address") == ["address"]        # NOT "addres"
    assert normalize_tokens("status") == ["status"]          # NOT "statu"
    assert normalize_tokens("analysis") == ["analysis"]      # NOT "analysi"
    assert normalize_tokens("boxes") == ["box"]
    # the property that matters: both sides land on the SAME token
    assert normalize_tokens("Addresses") == normalize_tokens("Address")
    assert normalize_tokens("customers") == normalize_tokens("Customer")
    assert normalize_tokens("ProductCategories") == normalize_tokens("ProductCategory")


def test_normalization_is_applied_to_both_sides_so_the_match_happens():
    """The whole point: normalizing only ONE side removes the match instead of creating
    it. A question word must share a token with the table it is asking about."""
    q = set(normalize_tokens("who are our top 5 customers by total due"))
    t = set(normalize_tokens(table_embed_text(
        {"table": "Customer",
         "columns": [{"name": "CustomerID", "type": "int"},
                     {"name": "CompanyName", "type": "nvarchar"}]})))
    assert "customer" in q and "customer" in t          # plural question <-> singular table
    soh = set(normalize_tokens(table_embed_text(
        {"table": "SalesOrderHeader",
         "columns": [{"name": "TotalDue", "type": "money"}]})))
    assert {"total", "due"} <= soh                      # CamelCase column is two words


def test_table_embed_text_omits_sql_type_keywords_but_keeps_column_words():
    """`int`/`nvarchar` never appear in a question, so they add no signal - but they DO
    inflate the table vector's norm, unevenly, pushing wordy tables away from every
    question. They are left out of the EMBEDDED text (table_text itself stays raw).
    A column NAMED `OrderDate` still keeps `date`: types are excluded by POSITION, not
    by blacklisting words."""
    t = {"table": "SalesOrderHeader",
         "columns": [{"name": "OrderDate", "type": "datetime"},
                     {"name": "TotalDue", "type": "money"}]}
    words = normalize_tokens(table_embed_text(t))
    assert "datetime" not in words
    assert "date" in words                               # the column WORD survives
    assert {"sales", "order", "header", "total", "due"} <= set(words)
    assert "datetime" in table_text(t)                   # raw form is unchanged


# --- #222 Fix 4: the decline floor is relative and portable ---------------------------

class _DenseLikeEmbedding:
    """A stand-in for a real sentence encoder: cosines live in a COMPRESSED, HIGH band
    (~0.6-0.9), never near zero, and it can match a synonym with no token overlap at all.
    Both properties are what break an absolute cosine floor, and neither can be shown with
    HashingEmbedding. `_BASE` shared dims are the "this is all English database text"
    component that gives every pair a high floor; `earnings`/`revenue` map onto the
    `amount` concept, which is how a dense model hits a table it shares no word with.

    Its synonym keys are in NORMALIZED form (`earning`, not `earnings`) because that is
    what an embedder is actually handed - normalize_text runs on both sides before the
    vector is built."""

    _BASE = 8
    _CONCEPTS = ("order", "amount", "customer", "region", "ticket", "severity")
    _SYNONYMS = {"earning": "amount", "revenue": "amount", "client": "customer"}
    _IGNORE = frozenset(("id",))

    def embed(self, texts):
        import math
        out = []
        for text in texts:
            concepts, unknown = set(), 0
            for w in normalize_tokens(text):
                w = self._SYNONYMS.get(w, w)
                if w in self._CONCEPTS:
                    concepts.add(w)
                elif w not in self._IGNORE:
                    unknown += 1
            vec = [1.0] * self._BASE
            vec += [1.0 if c in concepts else 0.0 for c in self._CONCEPTS]
            vec.append(float(unknown))
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


DENSE_SCHEMA = [
    {"table": "orders", "columns": [{"name": "order_id", "type": "int"},
                                    {"name": "amount", "type": "decimal"}]},
    {"table": "customers", "columns": [{"name": "customer_id", "type": "int"},
                                       {"name": "region", "type": "varchar"}]},
    {"table": "tickets", "columns": [{"name": "ticket_id", "type": "int"},
                                     {"name": "severity", "type": "varchar"}]},
]


def _dense_cosines(question):
    idx = SchemaIndex(DENSE_SCHEMA, _DenseLikeEmbedding())
    qv = _DenseLikeEmbedding().embed([normalize_text(question)])[0]
    return idx, sorted((cosine(qv, v) for v in idx._vectors.values()), reverse=True)


def test_dense_embedder_irrelevant_question_declines():
    """The portability property. A dense embedder's cosines are COMPRESSED and HIGH: this
    irrelevant question scores 0.61 against every table - twelve times the old absolute
    floor of 0.05, which therefore could never have declined it. The relative rule can:
    nothing stands out from the schema's own baseline."""
    idx, cosines = _dense_cosines("flux capacitor wobble")
    assert cosines[0] > 0.5, cosines            # an absolute 0.05 floor is USELESS here...
    assert idx.retrieve("flux capacitor wobble") == []      # ...and yet it declines


def test_dense_embedder_semantic_hit_retrieves_with_no_token_overlap():
    """The other side of the same coin, and why the lexical anchor cannot be the ONLY
    rule: "earnings" appears NOWHERE in this schema (no token overlap at all), but a dense
    embedder maps it onto the `amount` column. Separation from the baseline must retrieve
    it - a schema-vocabulary gate alone would decline exactly the semantic hits a real
    embedder is bought for."""
    idx, cosines = _dense_cosines("total earnings")
    assert not (_tokens("total earnings") & idx._vocab)     # genuinely NO lexical anchor
    names = [t["table"] for t in idx.retrieve("total earnings", k=2)]
    assert "orders" in names, names                          # the table holding `amount`
    assert cosines[0] > cosines[1], cosines                 # it really did stand out


def test_flat_but_relevant_schema_does_not_decline():
    """The trap in a pure separation rule: when a question is relevant to EVERY table, all
    cosines are equally high, so best == median and "nothing stands out" - declining a
    question the schema can obviously answer. High-and-flat and low-and-flat are opposite
    situations; the lexical anchor is what tells them apart."""
    wide = [{"table": f"t{i}", "columns": [{"name": "hub_id", "type": "int"}]}
            for i in range(10)]
    idx = _index(wide)
    qv = idx._embedder.embed([normalize_text("anything about hub")])[0]
    cos = [cosine(qv, v) for v in idx._vectors.values()]
    assert max(cos) == min(cos), cos             # perfectly FLAT: separation sees nothing
    assert len(idx.retrieve("anything about hub", k=20)) == 10   # ...and it still answers


def test_decline_is_decided_on_raw_cosine_not_the_boosted_score():
    """#222 Fix 4, the category error itself. The old gate thresholded
    `cosine x prior x lex_boost` - up to 3x an inflated cosine - against an ABSOLUTE
    constant. Prove the decision now reads the RAW cosine: a table whose boosted score is
    lifted far above the old 0.05 floor by its prior and lexical boost is still declined
    when its raw cosine says the question is not about this schema."""
    idx = _index(JOIN_SCHEMA)
    q = "zzqx wobble frequency of the flux capacitor"
    qv = idx._embedder.embed([normalize_text(q)])[0]
    raw = {n: cosine(qv, v) for n, v in idx._vectors.items()}
    q_tokens = _tokens(q)
    boosted = {n: raw[n] * idx._prior.get(n, 1.0) * idx._lex_boost(n, q_tokens)
               for n in raw}
    assert max(raw.values()) == 0.0, raw        # the honest signal: nothing matches
    assert idx.retrieve(q) == []
    # and the inflation is real - prior/boost multiply, so a nonzero raw cosine here would
    # have been magnified, which is exactly why thresholding the product was meaningless
    assert max(idx._prior.values()) > 1.0
    assert all(boosted[n] >= raw[n] for n in raw)


def test_sqlite_fk_edges_are_introspected():
    """The real behavioral test (#221 Task 3): a genuine FK declared on a live sqlite
    connection must come back as the exact (referencing, referenced) tuple."""
    eng = SqliteEngine.from_tables({
        "customers": {"columns": ["customer_id", "region"], "rows": []}})
    eng._conn.execute(
        "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, "
        "FOREIGN KEY (customer_id) REFERENCES customers(customer_id))")
    assert eng.fk_edges() == [("orders", "customers")]


def test_engine_port_default_fk_edges_is_empty():
    """SqlEnginePort.fk_edges must be a CONCRETE method (not @abstractmethod) so any of
    the seven providers that never override it still instantiate and still work - and
    its default return is EXACTLY [], never just 'some list' (fk_edges is an enrichment
    signal, never a source of failure). Exercise the ABC's own default directly with a
    minimal subclass that implements only the two required abstractmethods."""
    class _BareEngine(SqlEnginePort):
        def schema(self):
            return []

        def execute(self, sql, credential=None):
            return [], []

    eng = _BareEngine()
    assert eng.fk_edges() == []


def test_sqlite_fk_edges_empty_when_no_foreign_keys_declared():
    eng = SqliteEngine.from_tables({"t": {"columns": ["a"], "rows": []}})
    assert eng.fk_edges() == []


def test_cloud_providers_declare_fk_queries():
    """postgres/mysql/azure_sql cannot be hit from a unit test (no network); assert the
    introspection SQL exists and targets the right catalog view - a source-text smoke
    contract. The sqlite test above is the real behavioral proof."""
    import dbsearch.router.providers.postgres as pg
    import dbsearch.router.providers.mysql as my
    import dbsearch.router.providers.azure_sql as az
    for mod, marker in [(pg, "information_schema"), (my, "KEY_COLUMN_USAGE"),
                        (az, "sys.foreign_keys")]:
        src_text = open(mod.__file__).read()
        assert "fk_edges" in src_text and marker in src_text, mod.__name__


def test_refresh_schema_is_available_and_safe_everywhere():
    """refresh_schema() must exist on SqlEnginePort (concrete no-op default) and be
    safe to call on all engines, including sqlite which never caches."""
    eng = SqliteEngine.from_tables({"t": {"columns": ["a"], "rows": []}})
    eng.refresh_schema()                        # must not raise
    assert eng.schema()[0]["table"] == "t"


def test_sqlite_refresh_schema_no_op_safe():
    """SQLiteEngine has no cache - refresh_schema() is a no-op. Verify it exists
    and calling it multiple times does not break repeated schema() calls."""
    eng = SqliteEngine.from_tables({
        "users": {"columns": ["id", "name"], "rows": [[1, "alice"]]}})
    schema1 = eng.schema()
    eng.refresh_schema()
    schema2 = eng.schema()
    assert len(schema1) == 1 and schema1[0]["table"] == "users"
    assert schema1 == schema2


def test_sqlite_with_cache_subclass_refresh_clears():
    """Behavioral test: a caching subclass of SqlEnginePort must have
    refresh_schema() clear the cache so the next schema() call re-introspects.
    Build a minimal caching engine in-process to prove the mechanism."""
    class CachingSqliteEngine(SqlEnginePort):
        def __init__(self, engine):
            self._engine = engine
            self._schema_cache = None

        def schema(self):
            if self._schema_cache is not None:
                return self._schema_cache
            self._schema_cache = self._engine.schema()
            return self._schema_cache

        def execute(self, sql, credential=None):
            return self._engine.execute(sql, credential)

        def refresh_schema(self):
            self._schema_cache = None

    sqlite_eng = SqliteEngine.from_tables({
        "initial": {"columns": ["x"], "rows": []}})
    cached_eng = CachingSqliteEngine(sqlite_eng)

    # First call caches the schema
    schema1 = cached_eng.schema()
    assert schema1[0]["table"] == "initial"
    # Verify it's cached: a direct introspection would see both tables
    sqlite_eng._conn.execute('CREATE TABLE new_table (y)')
    schema_still_cached = cached_eng.schema()
    assert len(schema_still_cached) == 1, "cache must be opaque to new_table"
    # refresh_schema() clears the cache
    cached_eng.refresh_schema()
    # Next schema() call re-introspects and sees the new table
    schema2 = cached_eng.schema()
    tables = {t["table"] for t in schema2}
    assert "initial" in tables and "new_table" in tables


def test_every_caching_engine_refresh_actually_clears_its_cache():
    """All SEVEN cloud engines cache introspection; refresh_schema() must really clear it,
    or the #221 widen retry silently reuses a stale schema and a table created since
    compose stays invisible forever.

    A source-text grep CANNOT prove this: `AzureSqlEngine.__init__` already contains the
    literal string `self._schema_cache = None`, so `"_schema_cache = None" in src_text` is
    satisfied even when the method body is gutted to `pass`. That test cannot fail, and it
    covered only one of the seven engines.

    These engines open a cloud connection in __init__, but the METHOD needs no connection.
    Bypass __init__ with object.__new__ and drive refresh_schema() directly against a
    planted cache. Gut any one engine's body to `pass` and this goes red, naming it.
    """
    engines = (AzureSqlEngine, PostgresEngine, MySqlEngine, CosmosEngine,
               BigQueryEngine, DatabricksEngine, RedshiftEngine)
    assert len(engines) == 7                       # every _schema_cache holder is covered
    for cls in engines:
        eng = object.__new__(cls)                  # no __init__, no cloud connection
        eng._schema_cache = [{"table": "stale", "columns": []}]
        eng.refresh_schema()
        assert eng._schema_cache is None, cls.__name__


def main():
    print("Schema index self-test:")
    test_table_text_names_table_and_columns()
    test_infer_edges_matches_same_name_and_type()
    test_infer_edges_requires_type_compatibility()
    print("  PASS  table_text / infer_edges name+type matching / type-compatibility gate")
    test_build_graph_merges_fk_and_inferred_edges()
    test_build_graph_ignores_fk_edges_to_unknown_tables()
    test_connectivity_prior_boosts_connected_demotes_isolated()
    print("  PASS  build_graph FK+inferred merge / unknown-table FK ignored / "
          "connectivity_prior boosts connected, demotes isolated")
    test_infer_edges_skips_high_fanout_generic_columns()
    test_connectivity_prior_not_inverted_by_generic_columns()
    print("  PASS  selectivity cap: generic high-fanout columns (batch_id) emit no "
          "edges, real join keys still do; prior NOT inverted by staging noise")
    test_retrieve_finds_the_named_table()
    test_retrieve_is_ranked_best_first()
    test_retrieve_connects_seeds_through_junction_tables()
    test_retrieve_never_exceeds_max_tables()
    test_retrieve_empty_when_nothing_relevant()
    test_retrieve_join_key_lexical_boost()
    print("  PASS  SchemaIndex.retrieve: named-table match / ranked-best-first / "
          "junction-table path-connect / max_tables cap / decline floor / "
          "join-key lexical boost")
    test_normalize_tokens_splits_camelcase_snake_and_dotted_names()
    test_normalize_tokens_singularizes_without_mangling()
    test_normalize_tokens_drops_pure_numeric_tokens()
    test_normalization_is_applied_to_both_sides_so_the_match_happens()
    test_table_embed_text_omits_sql_type_keywords_but_keeps_column_words()
    print("  PASS  #222 subword split: CamelCase/snake/dotted -> words, plurals "
          "singularized without mangling `address`/`status`, types out of the embed text; "
          "#221 pure-numeric tokens (partition/version discriminators) dropped both sides")
    test_dense_embedder_irrelevant_question_declines()
    test_dense_embedder_semantic_hit_retrieves_with_no_token_overlap()
    test_flat_but_relevant_schema_does_not_decline()
    test_decline_is_decided_on_raw_cosine_not_the_boosted_score()
    print("  PASS  #222 relative decline floor: raw cosine (not the boosted score), "
          "declines a COMPRESSED dense range, keeps semantic hits with no token overlap, "
          "and does not decline a flat-but-relevant schema")
    test_sqlite_fk_edges_are_introspected()
    test_engine_port_default_fk_edges_is_empty()
    test_sqlite_fk_edges_empty_when_no_foreign_keys_declared()
    test_cloud_providers_declare_fk_queries()
    print("  PASS  fk_edges: sqlite PRAGMA introspection / ABC concrete [] default / "
          "no-FK sqlite table -> [] / cloud providers declare FK catalog queries")
    test_refresh_schema_is_available_and_safe_everywhere()
    test_sqlite_refresh_schema_no_op_safe()
    test_sqlite_with_cache_subclass_refresh_clears()
    test_every_caching_engine_refresh_actually_clears_its_cache()
    print("  PASS  refresh_schema: ABC concrete no-op default / sqlite (no cache) safe / "
          "cache-clear mechanism re-introspects / ALL 7 cloud engines really drop "
          "_schema_cache")
    print("\nSCHEMA-INDEX SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
