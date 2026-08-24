"""Schema index - compose-time table embeddings + join graph (card #221).

The router already retrieves at STORE level (profiles.py: embed once, cosine, relative
floor). This module applies the same shape one level down, at TABLE level, so a
warehouse with thousands of tables sends the generator single-digit tables instead of
the whole catalog. Metadata only ever enters the index (names, types) - never sample
values (LAW 1).

Join graph edges come from two sources because warehouses (Synapse, Redshift, BigQuery)
routinely declare NO foreign keys: declared FK edges where the engine has them, plus
INFERRED edges (same column name + compatible type in two tables). The connectivity
prior is the PageRank analog: core entity tables are heavily referenced; junk look-
alikes (sales_2023_backup, tmp_orders) have no edges and stay at baseline.
"""
from __future__ import annotations

import math
import re

from dbsearch.router.profiles import cosine


def table_text(t: dict) -> str:
    """One embeddable text per table: qualified name + column names/types + any comment.
    Metadata only - no sample values (LAW 1)."""
    cols = ", ".join(f"{c['name']} {c.get('type', '')}".strip()
                     for c in t.get("columns", []))
    comment = t.get("comment", "")
    return f"{t['table']}: {cols}" + (f" - {comment}" if comment else "")


# --- subword normalization (#222 Fix 3) ----------------------------------------------
# Database identifiers are not words. `SalesOrderHeader`.lower() is the single token
# `salesorderheader`, which shares NOTHING with the question word `sales` under ANY
# exact-token embedder (HashingEmbedding is a non-stemming bag of words), and a plural
# question word (`customers`) never matches a singular table (`Customer`). The result is
# a ranking that is pure hash noise in both directions: measured on AdventureWorksLT,
# "who are our top 5 customers by total due" ranked SalesOrderDetail top at dim=128 (a
# collision) and retrieved NOTHING at dim=4096 - i.e. it declined on its own customer
# database. Splitting identifiers into real words is what makes the cosine mean anything.
# This helps EVERY embedder: a dense model also reads "sales order header" better than
# "salesorderheader". The normalization is applied to BOTH sides (table text AND the
# question) - applying it to one side only would remove the match rather than create it.

# CamelCase / PascalCase with sane acronym runs:
#   SalesOrderHeader -> sales order header ; ProductID -> product id
#   HTTPServer       -> http server        ; Sales2023   -> sales   (see _is_numeric)
_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")

# Words that must survive naive singularization intact. Two families: identifiers whose
# singular already ends in `s` (status, address -> "addres" would be nonsense, and would
# STOP `addresses` from matching `Address`), and short English function words.
_KEEP_AS_IS = frozenset((
    "series", "species", "news", "sales",       # no useful singular / lexicalized plural
    "this", "thus", "its", "his", "hers", "yours", "ours", "does", "goes",
    "gas", "bus", "plus", "less", "versus", "various", "previous",
))


def _singular(word: str) -> str:
    """Naive, deterministic singularization so a plural question word matches a singular
    table name. Correctness bar is MATCHING, not linguistics: the same rule runs on both
    sides, so `sales -> sale` on the question and `sales -> sale` on the table still meet.
    What it must NOT do is mangle a word whose singular already ends in `s` (`address`,
    `status`) - that would break the match it exists to create."""
    if len(word) <= 3 or not word.endswith("s") or word in _KEEP_AS_IS:
        return word
    if word.endswith("ss") or word.endswith("us") or word.endswith("is"):
        return word                              # address, status, analysis - leave alone
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"                   # categories -> category
    for suffix in ("sses", "shes", "ches", "xes", "zes"):
        if word.endswith(suffix):
            return word[:-2]                     # addresses -> address, batches -> batch
    return word[:-1]                             # customers -> customer


# English function words carry no retrieval signal but DO carry hash-collision noise: in
# "who are our top 5 customers by total due" they are more than half the tokens, and under
# a 128-bucket hashing embedder they collide into buckets owned by real schema words. That
# is measurably what let an IRRELEVANT question ("airspeed velocity of an unladen swallow")
# score 0.18 against SalesOrderDetail and fail to decline. Dropped from BOTH sides.
# Deliberately conservative: only function/interrogative words. Verbs and quantifiers
# (list, show, top, name, order, status, type, key, date) are NOT here - they are real
# column words, and dropping them would delete signal, not noise.
_STOPWORDS = frozenset((
    "a", "an", "the", "of", "for", "and", "or", "to", "in", "on", "at", "by", "with",
    "from", "as", "is", "are", "was", "were", "be", "been", "do", "does", "did",
    "have", "has", "had", "who", "what", "which", "when", "where", "how", "why",
    "that", "this", "these", "those", "it", "its", "we", "our", "us", "you", "your",
    "me", "my", "i", "there", "than", "then", "into", "over", "per", "please",
))


def _is_numeric(word: str) -> bool:
    """A token that is nothing but digits is a PARTITION / VERSION DISCRIMINATOR, not
    vocabulary (#221 wide-schema proof). `load_0417`, `events_2024_01`, `sales_2023_backup`
    say nothing about what the table HOLDS - the number says only WHICH SLICE of an
    otherwise identical family it is.

    Keeping them is actively harmful, and at warehouse scale it breaks the honesty
    guarantee. A numbered family of 1,193 staging tables contributes 1,193 UNIQUE tokens,
    which is pure noise: to a hashing embedder those tokens are just collision fodder, and
    with that many of them competing for the bucket space an ordinary irrelevant English
    question lands on one by pure chance, earns a spurious nonzero cosine, clears the
    separation signal in `_declines`, and the store ANSWERS a question it should have
    declined. Measured on the 1,200-table fixture at production defaults:

        before this rule:   5 / 10 irrelevant questions declined
                            ("best hiking trails near Seattle" -> stg.load_0945)
        after  this rule:  10 / 10 declined

    A dense embedder is hurt too, just more quietly: the numbers dilute the real signal in
    the table's vector. This is why the rule belongs in the shared normalizer rather than
    being special-cased for HashingEmbedding.

    ACCEPTED TRADE-OFF, do not "fix" this back: a user asking "orders in 2023" no longer
    matches `orders_2023` ON THE YEAR. That is fine and intended. If two tables differ only
    by number they are near-identical to RETRIEVAL anyway - `orders_2023` and `orders_2024`
    both surface together as the same family, and the generator disambiguates from the real
    table names it is shown in the subset. Retrieval's job is to find the right FAMILY; the
    model picks the slice.

    Only a token that is ENTIRELY digits is dropped. Alphanumerics are never mangled by
    THIS rule: `_WORD_RE` has already split `s3_bucket` into `s` + `3` + `bucket`, so all
    this does is drop the bare `3` and keep the alphabetic stems. Both sides get the same
    treatment, so `s3 bucket` still matches `s3_bucket` on {s, bucket}.

    Consequence, by design: `ipv4_addr` and `ipv6_addr` become token-identical, as do
    `_v1`/`_v2` suffixes. That is the documented family behaviour - near-identical tables
    surface together and the generator disambiguates from the real names it is shown.
    """
    return word.isdigit()


def normalize_tokens(text: str) -> list:
    """`dw.SalesOrderHeaders` -> ['dw', 'sales', 'order', 'header']. Splits on any
    non-alphanumeric (snake_case, dotted qualified names), then CamelCase, then drops
    pure-numeric tokens (see `_is_numeric`), then singularizes, then drops function words.

    Applied to BOTH sides - table text AND question text - the same both-sides rule #222
    Fix 3 established. A normalization applied to one side only removes matches rather than
    creating them."""
    words: list = []
    for chunk in re.split(r"[^A-Za-z0-9]+", text or ""):
        if not chunk:
            continue
        for m in _WORD_RE.finditer(chunk):
            raw = m.group(0).lower()
            if _is_numeric(raw):
                continue                 # partition/version discriminator, not vocabulary
            word = _singular(raw)
            if word not in _STOPWORDS:
                words.append(word)
    return words


def normalize_text(text: str) -> str:
    """The normalized form handed to the embedder - identifiers become real words."""
    return " ".join(normalize_tokens(text))


def table_embed_text(t: dict) -> str:
    """What actually gets EMBEDDED for a table: the qualified name + column NAMES + any
    comment, subword-normalized. `table_text` stays raw - it is the human-readable form.

    SQL type keywords are deliberately left OUT of the embedded text. `int`/`nvarchar`/
    `decimal` never appear in a user's question, so they contribute nothing to the match,
    but they DO inflate the table vector's norm - and unevenly: a table with four nvarchar
    columns is pushed away from every question relative to a table with two, purely
    because of its DDL. Measured on AdventureWorksLT, that dilution is what sank the
    `Customer` table below `CustomerAddress` for "who are our top 5 customers by total
    due". Type information is not lost; it is used where it actually means something -
    `infer_edges` matches join keys on name + TYPE FAMILY.
    """
    parts = [t["table"]] + [c["name"] for c in t.get("columns", [])]
    comment = t.get("comment", "")
    if comment:
        parts.append(comment)
    return normalize_text(" ".join(parts))


_TYPE_FAMILY = {
    "int": "num", "bigint": "num", "smallint": "num", "tinyint": "num",
    "decimal": "num", "numeric": "num", "float": "num", "real": "num", "money": "num",
    "varchar": "str", "nvarchar": "str", "char": "str", "nchar": "str",
    "text": "str", "string": "str",
    "date": "time", "datetime": "time", "datetime2": "time", "timestamp": "time",
}


def _family(sql_type: str) -> str:
    base = re.split(r"[(\s]", (sql_type or "").strip().lower())[0]
    return _TYPE_FAMILY.get(base, base)


DEFAULT_MAX_FANOUT = 20


def infer_edges(schema: list, *, max_fanout: int = DEFAULT_MAX_FANOUT) -> list:
    """Candidate join edges from column name + type-family matching. Required, not
    optional: FK-only expansion silently no-ops on FK-less warehouses (spec hole 1).

    SELECTIVITY CAP (max_fanout) - the IDF intuition this design already uses at store
    level, applied to column names. A column name shared by MANY tables is a GENERIC /
    technical column (batch_id, id, created_at, status, payload); it carries no join
    information. A real join key (customer_id, product_number) appears in a handful of
    tables. So a name+type bucket spanning more than `max_fanout` tables emits NO edges
    at all - the whole bucket is skipped, never truncated to an arbitrary subset.

    Without the cap a warehouse's `batch_id` cliques its 1,190 staging tables together
    (~707k edges), handing every junk table the MAXIMUM degree while real entity tables
    sit near baseline - which INVERTS the connectivity prior it feeds, i.e. the exact
    reverse of this module's purpose. The cap is what stops that.
    """
    by_col: dict = {}
    for t in schema:
        for c in t.get("columns", []):
            key = (c["name"].lower(), _family(c.get("type", "")))
            by_col.setdefault(key, []).append(t["table"])
    edges: list = []
    seen: set = set()
    for tables in by_col.values():
        if len(tables) > max_fanout:
            continue                      # generic column, not a join key - skip entirely
        for i, a in enumerate(tables):
            for b in tables[i + 1:]:
                pair = tuple(sorted((a, b)))
                if a != b and pair not in seen:
                    seen.add(pair)
                    edges.append(pair)
    return edges


def build_graph(schema: list, fk_edges: list, *,
                max_fanout: int = DEFAULT_MAX_FANOUT) -> dict:
    """Undirected adjacency over ALL schema tables. FK edges pointing at tables not in
    the schema (dropped by an allowlist, or stale) are ignored rather than invented.
    `max_fanout` is passed through to infer_edges so callers can tune the generic-column
    selectivity cap; DECLARED FK edges are never capped (an engine-declared FK is a real
    join key by definition, however common)."""
    tables = {t["table"] for t in schema}
    graph: dict = {name: set() for name in tables}
    for a, b in list(fk_edges) + infer_edges(schema, max_fanout=max_fanout):
        if a in tables and b in tables and a != b:
            graph[a].add(b)
            graph[b].add(a)
    return graph


def connectivity_prior(graph: dict) -> dict:
    """Degree-based authority prior in [1.0, 2.0]. Zero-edge tables stay at 1.0 - the
    demotion of junk tables is RELATIVE: real entity tables get boosted past them."""
    if not graph:
        return {}
    max_deg = max((len(v) for v in graph.values()), default=0)
    if max_deg == 0:
        return {k: 1.0 for k in graph}
    return {k: 1.0 + math.log1p(len(v)) / math.log1p(max_deg)
            for k, v in graph.items()}


def _tokens(text: str) -> set:
    """Lexical-boost tokens. SAME normalization as the embedded text (#222 Fix 3): the
    old `[a-z0-9_]+` split kept `salesorderheader` and `customer_id` whole, so a question
    saying "customers" could not lexically hit a `Customer` table either."""
    return set(normalize_tokens(text))


class SchemaIndex:
    """Per-store retrieval index (card #221). Built once from the engine's (cached)
    schema; rebuilt by the store when the schema object changes (widen-retry
    re-introspection). Embeds every table ONCE at construction (the router's cheap-
    prefilter shape from profiles.py, applied one level down at table granularity), then
    `retrieve` ranks tables per question by cosine x connectivity prior x lexical boost,
    connects the top seeds through join-graph junction tables, and returns [] when
    nothing clears the floor.

    That [] is a cheap PREFILTER, not the honesty guarantee - do not lean on it as a
    safety property. Its lexical anchor is an OR that fires on a single content word,
    so "which products have the most support tickets" (the literal #211 question) will
    NOT decline here: it shares the word "product" with the schema. The semantic gate
    is the model's own CANNOT_ANSWER (#211), backed by the store's refusal to fall back
    to a guessed table in retrieval mode (#222). The floor's job is only to cut the
    obvious no-overlap case cheaply, before an LLM call.
    """

    def __init__(self, schema: list, embedder, fk_edges: list | None = None) -> None:
        self._schema = schema
        self._by_name = {t["table"]: t for t in schema}
        self._embedder = embedder
        self._vectors = dict(zip(
            [t["table"] for t in schema],
            embedder.embed([table_embed_text(t) for t in schema]) if schema else []))
        self._graph = build_graph(schema, fk_edges or [])
        self._prior = connectivity_prior(self._graph)
        # every content word this schema knows how to talk about (#222 Fix 4)
        self._vocab: set = set()
        for t in schema:
            self._vocab |= _tokens(table_embed_text(t))

    def _lex_boost(self, table: str, q_tokens: set) -> float:
        """Deterministic lexical bridge: a table or column NAMED in the question (the
        #215 decomposer keeps join keys in the sub-question text) outranks a merely
        cosine-similar one. Table-name hit > column hit > nothing."""
        t = self._by_name[table]
        name_tokens = _tokens(t["table"])
        if name_tokens & q_tokens:
            return 1.5
        col_tokens: set = set()
        for c in t.get("columns", []):
            col_tokens |= _tokens(c["name"])
        if col_tokens & q_tokens:
            return 1.25
        return 1.0

    def _shortest_path(self, a: str, b: str, max_edges: int = 3) -> list:
        """BFS shortest path a->b, at most `max_edges` edges (<= 2 intermediates).
        Returns INTERMEDIATE tables only (both endpoints excluded); [] when a == b,
        already adjacent, or unconnected within the cap.

        Standard parent-pointer BFS: track `parent[node]` as we discover nodes, then
        walk the pointers back from `b` to `a` once found and reverse. Simpler and less
        error-prone than reconstructing the path while still walking forward.
        """
        if a == b:
            return []
        parent = {a: None}
        frontier = [a]
        edges_used = 0
        while frontier:
            if edges_used >= max_edges:
                return []
            edges_used += 1
            nxt = []
            for node in frontier:
                for nb in self._graph.get(node, ()):
                    if nb in parent:
                        continue
                    parent[nb] = node
                    if nb == b:
                        path = []
                        cur = parent[b]
                        while cur is not None and cur != a:
                            path.append(cur)
                            cur = parent[cur]
                        path.reverse()
                        return path
                    nxt.append(nb)
            frontier = nxt
        return []

    def _declines(self, cosines: list, q_tokens: set, margin_frac: float,
                  min_cosine: float) -> bool:
        """The decline decision (#222 Fix 4), made on the RAW COSINE.

        The old gate compared an ABSOLUTE floor (0.05) against `cosine x connectivity
        prior (1.0-2.0) x lex_boost (1.0-1.5)` - a value that is up to 3x an inflated
        cosine, i.e. a category error: the thing being thresholded is not a similarity at
        all, and the same constant means completely different things for a table with
        edges and one without. It is also not portable across embedders - a dense model's
        cosines live in a compressed band (say 0.6-0.8), where an absolute 0.05 floor
        never fires and EVERY question "retrieves".

        So: decide on the raw cosine, and decide RELATIVELY, the way the rest of this
        codebase does (profiles.coarse_prune, the query-service relevance floor). Two
        INDEPENDENT signals say a question has something to do with this schema; the
        schema declines only when BOTH are silent.

        1. SEPARATION. Is the best table meaningfully more similar than this schema's own
           baseline similarity to this question? The baseline is the MEDIAN raw cosine -
           robust to the relevant tables in the tail, and it moves WITH the embedder (~0
           for a sparse lexical one, ~0.65 for a dense one):

               threshold = baseline + margin_frac * (1 - baseline)

           The margin is a fraction of the HEADROOM above the baseline, not a fixed
           number, which is what makes ONE constant work in both regimes: at baseline 0.0
           the best table must reach 0.15; at a dense baseline of 0.65 it need only reach
           0.70, which in that compressed range is the same amount of standing out.
           `min_cosine` is a secondary backstop for the degenerate all-zero column (and
           for the widen path, which relaxes margin_frac to 0).

        2. LEXICAL ANCHOR. Does the question name ANY content word this schema knows
           (`self._vocab`)? Separation alone is not sufficient, and the failure is not
           hypothetical: when a question is relevant to EVERY table (10 tables all keyed
           by `hub_id`, asked "anything about hub"), every cosine is equally high, so
           best == median and the separation rule reports "nothing stands out" - and
           declines a question the schema can obviously answer. High-and-flat and
           low-and-flat are opposite situations that separation cannot tell apart. The
           lexical anchor can, and it is embedder-independent.

        Conversely the anchor alone is not sufficient either: a dense embedder is entitled
        to match "revenue" to a `TotalDue` column with ZERO token overlap, and requiring
        an anchor would decline exactly the semantic hits it was bought for. Hence OR, and
        hence a decline means: the question shares no word with this schema AND no table
        stands out from the schema's own baseline.
        """
        if q_tokens & self._vocab:
            return False                        # lexical anchor - the question names us
        best = max(cosines)
        ordered = sorted(cosines)
        mid = len(ordered) // 2
        baseline = ordered[mid] if len(ordered) % 2 else \
            (ordered[mid - 1] + ordered[mid]) / 2.0
        baseline = max(0.0, baseline)
        threshold = max(min_cosine, baseline + margin_frac * (1.0 - baseline))
        return best < threshold

    def retrieve(self, question: str, k: int = 8, max_tables: int = 16,
                 floor_frac: float = 0.6, margin_frac: float = 0.15,
                 min_cosine: float = 0.02) -> list:
        """Rank the schema by relevance to `question`, keep the top seeds within a
        relative floor of the best, connect them through join-graph junction tables
        (spec hole 2: a question naming two tables that only join through a third,
        unnamed one), then cap and return. [] is the prefilter's no-overlap signal (see
        `_declines`), and the caller must never fall back to a guessed table on it - but
        it is NOT the semantic gate. A [] here means "nothing in this schema shares a word
        with the question"; it does not mean "this schema cannot answer it". See the class
        docstring: the model's CANNOT_ANSWER is what catches a question the schema merely
        LOOKS able to answer.

        The widen retry (#222 Fix 5) relaxes `floor_frac` and `margin_frac`, NOT just
        `k`: k was never the binding constraint (measured: k=8 and k=30 returned the
        identical set, because the relative floor cut first), so a k-only widen was a
        no-op wearing a widen's clothes.
        """
        if not self._schema:
            return []
        qv = self._embedder.embed([normalize_text(question)])[0]
        q_tokens = _tokens(question)
        scored = []
        cosines = []
        for name, tv in self._vectors.items():
            raw = cosine(qv, tv)
            cosines.append(raw)
            s = raw * self._prior.get(name, 1.0) * self._lex_boost(name, q_tokens)
            scored.append((s, name))
        if self._declines(cosines, q_tokens, margin_frac, min_cosine):
            return []                       # decline signal - caller must NOT generate
        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0][0]
        # floor_frac > 0 excludes zero-signal tables on its own (best > 0 here, because
        # _declines already required the best RAW cosine to clear min_cosine, and prior x
        # boost are both >= 1). floor_frac == 0 is the widen path deliberately letting
        # them in: the retrieval miss we are recovering from is precisely a table whose
        # lexical cosine was zero, so a widen that still requires signal cannot rescue it.
        seeds = [name for s, name in scored[:k] if s >= best * floor_frac]
        # connect seeds pairwise (spec: junction tables in, hub explosion out)
        path_tables: list = []
        for i, a in enumerate(seeds):
            for b in seeds[i + 1:]:
                for t in self._shortest_path(a, b):
                    if t not in seeds and t not in path_tables:
                        path_tables.append(t)
        ranked = seeds + path_tables
        return [self._by_name[n] for n in ranked[:max_tables]]
