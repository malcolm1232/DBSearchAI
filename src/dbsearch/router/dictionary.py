"""In-tenant value dictionary - server-side literal resolution (#479, ADR 0015).

By LAW 1 no customer value may enter a prompt (`structured.py:_schema_fingerprint`,
`schema_index.table_text`), so the SQL generator is structurally blind to how a value is
cased, separated or abbreviated. It writes the literal the way the USER said it: "debit
cards" against a column storing `debit_card`, "Dominican Republic" against `D.R.`. The
query matches nothing, and before #476 the empty aggregate was reported as a zero.

The repair belongs on THIS side of the boundary. The model still writes the user's
wording; the server maps that wording onto the stored encoding afterwards, inside the
tenant, and discloses the substitution. Nothing here is a prompt input.

Pure except for `column_values`, which reads through the caller's own AccessContext - so
the dictionary is a per-identity view by construction (LAW 2): a value the caller cannot
read is not resolvable for them, with no separate trimming logic to get wrong.
"""
from __future__ import annotations

import re

from dbsearch.router.profiles import cosine

#: Distinct values above this are an identifier-like column - no meaningful dictionary,
#: and profiling it would materialise PII into a second place (a LAW 1 protection as much
#: as a correctness one).
MAX_DICTIONARY_VALUES = 200

#: Bound on the raw DISTINCT read behind a token dictionary. A multi-value column's
#: combos can outnumber its tokens many times over (movielens genres: hundreds of combos,
#: ~20 genres), so the scan reads past MAX_DICTIONARY_VALUES - but never unboundedly:
#: a column with thousands of distinct raw values is an identifier whatever its shape.
_SCAN_LIMIT = 2000

#: The embedding rung accepts only a high absolute similarity AND a clear margin over the
#: runner-up. Resolving to the wrong REAL value would be a confident falsehood - the exact
#: failure class this work removes - so ambiguity declines.
#: Measured against nomic-embed-text on the #473 real pack, and the margin is why this is
#: safe: "cancelled" -> canceled scores 0.929 with a +0.415 margin and "science fiction" ->
#: Sci-Fi scores 0.837 with +0.227, while "dominican republic" ranks CURACAO first at 0.622
#: with a +0.017 margin over the runner-up. Embeddings do not resolve abbreviations - they
#: rank a plausible WRONG country top - so the guard has to reject on both counts. Without
#: the margin this feature would confidently answer "Curacao".
MIN_SIMILARITY = 0.80
MIN_MARGIN = 0.05

_IDENT_RE = r"[A-Za-z_]\w*"

_PREDICATE = re.compile(
    r"""^\s*(?:LOWER|UPPER)?\s*\(?\s*((?:[A-Za-z_]\w*\s*\.\s*)?[A-Za-z_]\w*)\s*\)?\s*=\s*
        (?:LOWER|UPPER)?\s*\(?\s*'((?:[^']|'')*)'\s*\)?\s*$""",
    re.I | re.X)

#: The contains-LIKE the generator writes for a value it is unsure of (`LOWER(genres)
#: LIKE '%science fiction%'` is the actual E-002 SQL). Only the both-sides-wrapped shape
#: is read: a prefix/suffix anchor is part of what the pattern MEANS, and substituting a
#: stored value into it would silently change the question.
_LIKE_PREDICATE = re.compile(
    r"""^\s*(?:LOWER|UPPER)?\s*\(?\s*((?:[A-Za-z_]\w*\s*\.\s*)?[A-Za-z_]\w*)\s*\)?\s+LIKE\s+
        (?:LOWER|UPPER)?\s*\(?\s*'%((?:[^']|'')*)%'\s*\)?\s*$""",
    re.I | re.X)

_ALIAS = re.compile(
    rf"\b(?:from|join)\s+({_IDENT_RE})(?:\s+as)?(?:\s+({_IDENT_RE}))?", re.I)


def normalize_value(value: str) -> str:
    """Fold the differences that are pure notation: case, punctuation, separators, and a
    trailing plural. `debit_card`, "Debit Cards" and "debit-card" all land on "debit card".

    Deliberately does NOT reach abbreviations or synonyms - `D.R.` normalizes to "d r",
    not "dominican republic". That is the embedding rung's job, and pretending otherwise
    here would mean guessing."""
    text = re.sub(r"[^0-9a-z]+", " ", str(value).lower()).strip()
    return " ".join(word[:-1] if len(word) > 3 and word.endswith("s") else word
                    for word in text.split() if word not in _ELIDED)


#: Conjunctions a schema elides and prose does not: `bed_bath_table` is "bed, bath and
#: table". Dropped as notation, alongside the punctuation that separates the same words.
#: Kept deliberately tiny - a broad stopword list would start folding values that differ.
_ELIDED = frozenset({"and", "or"})


def value_matching_embedder(embedder):
    """The embedder value matching may use - a dense one, or nothing.

    A hashed bag-of-words embedder scores a nonzero cosine between texts sharing zero
    tokens; such a score is by definition a collision (#225 measured this). The schema
    index survives that by buying bucket capacity, because a schema's vocabulary is large.
    A column VALUE is one or two words, so capacity rescues nothing here, and a
    manufactured match would assert a wrong real value as fact. Refused outright."""
    try:
        from dbsearch.adapters.local import HashingEmbedding
    except Exception:                          # noqa: BLE001 - adapters unavailable
        return embedder
    return None if embedder is None or isinstance(embedder, HashingEmbedding) else embedder


def predicate_literal(predicate: str) -> "tuple | None":
    """(column_reference, written_literal, shape) from the shapes the generator writes:
    the equalities `col = 'x'` / `LOWER(col) = 'x'` / `LOWER(col) = LOWER('x')` (shape
    "eq") and the contains-LIKE `col LIKE '%x%'` with the same LOWER variants (shape
    "like", wrappers stripped so the ladder sees the user's words).

    The reference is returned AS WRITTEN, qualifier included, because the generator
    routinely aliases - `LOWER(T1.payment_type) = 'debit card'` is the actual SQL behind
    the E-001 failure - and the rewrite has to put back a reference that still resolves in
    the same query.

    Anything else - a range, a column-to-column comparison, a LIKE whose wildcards carry
    meaning (a prefix/suffix anchor, an inner `%` or `_`) - returns None: substituting a
    stored value into those would change what the predicate MEANS."""
    match = _PREDICATE.match(predicate or "")
    if match:
        return re.sub(r"\s+", "", match.group(1)), match.group(2).replace("''", "'"), "eq"
    match = _LIKE_PREDICATE.match(predicate or "")
    if match:
        written = match.group(2).replace("''", "'")
        if "%" in written or "_" in written:
            return None
        return re.sub(r"\s+", "", match.group(1)), written, "like"
    return None


def table_aliases(sql: str) -> dict:
    """{alias-or-table-name: table} for every FROM/JOIN in `sql`.

    A qualified predicate names an ALIAS, not a table, so the dictionary read needs this to
    know which table to ask. Both directions are recorded (`T1` -> payments AND `payments`
    -> payments) so an unqualified or self-named reference resolves through the same map."""
    out = {}
    for table, alias in _ALIAS.findall(sql or ""):
        if alias and alias.lower() in _NOT_AN_ALIAS:
            alias = ""
        out[table] = table
        if alias:
            out[alias] = table
    return out


#: Words that follow a table name without being an alias.
_NOT_AN_ALIAS = frozenset({
    "on", "where", "inner", "left", "right", "full", "outer", "join", "cross",
    "group", "order", "limit", "having", "union", "and", "or", "as", "using"})


def resolve_literal(written: str, candidates: list, embedder=None,
                    llm=None) -> "str | None":
    """Map the user's wording onto one stored value, or None to decline.

    The ladder, first hit wins: exact, case-fold, normalized, then - only with a dense
    embedder - nearest by cosine subject to MIN_SIMILARITY and MIN_MARGIN, and finally -
    only with an IN-TENANT model - one LLM disambiguation call over the shortlist. None
    at every step means "I could not tell", which the caller turns into an honest
    decline; it never means "no rows"."""
    if not candidates:
        return None
    for candidate in candidates:
        if candidate == written:
            return candidate
    folded = str(written).casefold()
    for candidate in candidates:
        if str(candidate).casefold() == folded:
            return candidate
    target = normalize_value(written)
    hits = [c for c in candidates if normalize_value(c) == target]
    if len(hits) == 1:
        return hits[0]
    if hits:
        return None                            # two stored values, one notation: ambiguous
    dense = value_matching_embedder(embedder)
    match = _nearest(written, candidates, dense)
    if match is not None:
        return match
    return _llm_pick(written, candidates, dense, llm)


def _ranked(written: str, candidates: list, embedder) -> "list | None":
    """Every candidate scored against the user's wording, best first - or None when the
    embedder did not answer for ALL of them (#521).

    The length check is the substance. LAW 9 invites a bring-your-own provider, so a
    REMOTE embedder is a supported deployment, and a remote batch can come back empty or
    partial. Unpacking that positionally failed two ways: an empty reply raised out of the
    ladder, and `executor` reported the resulting error as the SOURCE failing - the
    misattributed decline #218 exists to remove - while a partial reply was worse, because
    `zip` silently dropped the unscored candidates and a lone survivor skips MIN_MARGIN
    entirely, resolving the user's wording to a WRONG stored value. A partial embedding is
    not a partial match; it is no match, which the caller turns into an honest decline."""
    if embedder is None:
        return None
    texts = [str(written)] + [str(c) for c in candidates]
    try:
        vectors = embedder.embed(texts)
        if len(vectors) != len(texts):         # an unsized reply raises here, and is
            return None                        # likewise no answer at all
        query, rest = vectors[0], vectors[1:]
        return sorted(((cosine(query, v), c) for v, c in zip(rest, candidates)),
                      key=lambda pair: pair[0], reverse=True)
    except Exception:                          # noqa: BLE001 - an embedder failure is not a match
        return None


def _nearest(written: str, candidates: list, embedder) -> "str | None":
    scored = _ranked(written, candidates, embedder)
    if not scored or scored[0][0] < MIN_SIMILARITY:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < MIN_MARGIN:
        return None
    return scored[0][1]


#: The LLM rung sends at most this many candidates, shortlisted by the embedder. Small on
#: purpose: the prompt is a values-in-a-prompt exception (see `_llm_pick`), so it carries
#: the minimum that lets the model name the right abbreviation.
_LLM_SHORTLIST = 10


def _llm_pick(written: str, candidates: list, embedder, llm) -> "str | None":
    """The last rung (#462, ADR 0015 amendment): ask an IN-TENANT model which stored
    value the user's wording means. Embeddings provably cannot resolve abbreviations -
    nomic-embed-text ranks CURACAO above `D.R.` for "dominican republic" - while a
    language model knows them cold.

    This is the ONE sanctioned exception to "no values in a prompt", and it is legal
    only because the prompt never leaves the tenant: the gate is the adapter's own
    `in_tenant` capability flag, absent-means-no (LAW 1, fail closed). The reply must be
    a VERBATIM member of the shortlist - anything else, including NONE, an error or a
    paraphrase, is a decline. A fabricated WHERE literal is the failure class this whole
    ladder exists to remove."""
    if llm is None or not getattr(llm, "in_tenant", False):
        return None
    pick = getattr(llm, "pick_value", None)
    if pick is None:
        return None
    shortlist = list(candidates)
    if len(shortlist) > _LLM_SHORTLIST:
        ranked = _ranked(written, shortlist, embedder)
        if not ranked:                         # no embedder, or no trustworthy ranking
            return None                        # from it: no way to shortlist honestly
        shortlist = [c for _score, c in ranked[:_LLM_SHORTLIST]]
    try:
        reply = pick(str(written), shortlist)
    except Exception:                          # noqa: BLE001 - a model failure is not a match
        return None
    reply = (reply or "").strip()
    return reply if reply in shortlist else None


def column_values(engine, table: str, column: str, access=None) -> "list | None":
    """The distinct values of one column, read through the caller's own credential, or
    None when the column has no useful dictionary (too many values, or unreadable).

    A pipe-delimited multi-value column (movielens `genres` stores
    "Action|Adventure|Sci-Fi") is dictionaried by TOKEN, not by combo: the retrievable
    unit is what a contains-LIKE can match, and a combo dictionary would never hold
    "Sci-Fi". The MAX_DICTIONARY_VALUES cap therefore applies to the tokens - it is the
    size of the dictionary actually offered for matching - while _SCAN_LIMIT bounds the
    raw read so an identifier-like column is still never materialised (LAW 1)."""
    if not re.fullmatch(r"[A-Za-z_]\w*", table or "") \
            or not re.fullmatch(r"[A-Za-z_]\w*", column or ""):
        return None
    sql = (f"SELECT DISTINCT {column} FROM {table} "
           f"WHERE {column} IS NOT NULL LIMIT {_SCAN_LIMIT + 1}")
    try:
        _cols, rows = engine.execute(
            sql, credential=getattr(access, "delegated_credential", None),
            principal=getattr(access, "user_oid", None))
    except Exception:                          # noqa: BLE001 - unreadable is not resolvable
        return None
    if len(rows) > _SCAN_LIMIT:
        return None
    raw = [r[0] for r in rows if isinstance(r[0], str)]
    if len(raw) <= MAX_DICTIONARY_VALUES and not any("|" in v for v in raw):
        return raw
    seen, tokens = set(), []
    for value in raw:
        for token in value.split("|"):
            token = token.strip()
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)
    if not tokens or len(tokens) > MAX_DICTIONARY_VALUES:
        return None
    return tokens
