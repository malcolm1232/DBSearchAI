"""Federated structured store — pushdown SQL behind StorePort (Phase E E4, card #101).

ADR 0007: the query executes INSIDE an engine, and only the result set becomes Evidence.
The embedded engine here is stdlib sqlite3 — the in-tenant path for loose `.csv`/inline
tables (DuckDB, BigQuery, Synapse, Redshift are siblings behind the same SqlEnginePort;
cloud engines execute remotely under the E5 delegated credential). Every generated SQL
passes the read-only guard BEFORE execution and is written to an audit sink (§8):
single SELECT/WITH, no comments, no DDL/DML, and no table outside the visible schema —
a generated query can never touch what the caller can't see.

NL2SQL is a SEAM: `sql_generator(question, schema) -> sql`. The default
`keyword_sql_generator` is deterministic and honest about being naive (SUM/COUNT/AVG +
'by X' GROUP BY); `llm_sql_generator` (#135) wraps a schema-grounded chat model behind
the SAME seam, validating the model's SQL against the visible schema and falling back to
the keyword generator on any failure. Row-level security: preferred = the engine runs as
the delegated user (gate #2, ADR 0006); fallback = the broker's row_policy predicate is
applied over the result set here.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import re
from collections import OrderedDict
from abc import ABC, abstractmethod
from typing import Callable

from dbsearch.router.dictionary import (
    column_values, predicate_literal, resolve_literal, table_aliases,
)
from dbsearch.router.evidence import Evidence, ROW
from dbsearch.router.provider import StoreProviderPort
from dbsearch.router.store import (
    ANALYTICAL, AccessContext, EXACT, FEDERATED_SQL, StorePort, StoreProfile,
)

SqlGenerator = Callable[[str, list], str]      # (question, schema) -> sql
Authorizer = Callable[[str], AccessContext]    # user_oid -> AccessContext (broker seam)
AuditSink = Callable[[dict], None]


def entra_principal_from_token(token: str) -> str:
    """Azure Postgres/MySQL bind a delegated connection to `user=<Entra principal>` with the
    Entra access token AS the password, so `user` must match the token's own subject. Derive it
    from the token (its `upn`/`preferred_username` claim) rather than pinning ONE identity in
    config — so a single store serves ANY signed-in user (true query-as-user, #188/#189).
    Best-effort JWT payload decode; no signature check here (the DB validates the token)."""
    import base64
    import json

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return ""
    return (claims.get("upn") or claims.get("preferred_username")
            or claims.get("unique_name") or "")


def call_user_connect(user_connect, token: str, principal: "str | None"):
    """Call a provider's delegated-connect factory, passing the session `principal` (#193) only
    when the factory accepts it. Factories come in two shapes: the Entra ones derive the principal
    FROM the token (1-arg `(token)`), the Cloud SQL / Google ones need it threaded from the session
    (2-arg `(token, principal)`) because a Google OAuth token is opaque. Binding by arity keeps
    both working (same pattern as identity_broker._for_idp)."""
    import inspect

    try:
        arity = len(inspect.signature(user_connect).parameters)
    except (TypeError, ValueError):      # builtins / C callables
        arity = 1
    return user_connect(token, principal) if arity >= 2 else user_connect(token)

# The trailing `(?!\s*\()` is what separates a write STATEMENT from a scalar FUNCTION that
# happens to share its name (#477). `REPLACE INTO` is an upsert; `REPLACE(salary, ',', '')`
# is a string function, and MySQL's TRUNCATE(n, d) and INSERT(str, pos, len, new) are the
# same story. No write statement in any dialect puts a parenthesis directly after its
# leading keyword, so this is a structural distinction rather than a list of exceptions -
# the guard is narrowed, never weakened.
#
# It mattered because a model asked a PARAPHRASED question hedges defensively: llama3.1:8b
# answered "the fattest single-year pay packet a ballplayer took home" with
# `SELECT MAX(CAST(REPLACE(salary, ',', '') AS REAL)) FROM salaries` - correct, read-only,
# and rejected. `llm_sql_generator` then swallowed the rejection and degraded to the
# keyword stub, so the store answered about SALARIES with `SELECT * FROM batting LIMIT 5`
# and nothing surfaced the reason. Measured on the #473 real pack as capability F.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|"
    r"truncate|grant|revoke|vacuum|reindex)\b(?!\s*\()", re.I)
# A table reference can be schema-qualified and/or bracket/quote-delimited: `SalesLT.Product`,
# `[dbo].[sales]`. Capturing only the leading identifier (the old `([A-Za-z_]\w*)`) stopped at
# the dot and read `FROM SalesLT.Product` as the table "SalesLT" — rejecting valid SQL as an
# invisible table (#203). Matching the WHOLE dotted name is also the safe direction: the full
# name must be visible, so `evil.sales` can never ride in on `sales` being allowed.
_IDENT = r"[\[\"`]?[A-Za-z_]\w*[\]\"`]?"
_TABLE_REF = re.compile(rf"\b(?:from|join)\s+({_IDENT}(?:\s*\.\s*{_IDENT})*)", re.I)
_CTE_NAME = re.compile(r"\b(?:with|,)\s*([A-Za-z_]\w*)\s+as\s*\(", re.I)


def _norm_table(name: str) -> str:
    """`[dbo] . [Sales]` and `dbo.sales` are the same table; compare them as such. Only the
    delimiters and whitespace are stripped — the schema qualifier is preserved, because it is
    part of the table's identity."""
    return re.sub(r'[\[\]"`\s]', "", name).lower()


def validate_sql(sql: str, visible_tables: list) -> None:
    """Read-only, single-statement, visible-schema-only guard. Raises ValueError."""
    s = sql.strip()
    if "--" in s or "/*" in s:
        raise ValueError("SQL comments are not allowed")
    if ";" in s.rstrip(";"):
        raise ValueError("multiple SQL statements are not allowed")
    first = s.split(None, 1)[0].lower() if s else ""
    if first not in ("select", "with"):
        raise ValueError("only SELECT/WITH queries are allowed")
    if _FORBIDDEN.search(s):
        raise ValueError("write/DDL/introspection keywords are not allowed")
    allowed = {_norm_table(t) for t in visible_tables}
    allowed |= {m.group(1).lower() for m in _CTE_NAME.finditer(s)}
    for m in _TABLE_REF.finditer(s):
        if _norm_table(m.group(1)) not in allowed:
            raise ValueError(f"table {m.group(1)!r} is not in the visible schema")


# --- #219 federated semi-join: bind half A's join-key values into half B's query ----------
#
# "tickets vs revenue by product" decomposes to two halves that each answer correctly but over
# NON-OVERLAPPING key sets, so the synthesizer honestly refuses to correlate them. The fix is a
# semi-join: run half A, take the join-key VALUES it actually showed, and constrain half B to
# them (WHERE key IN (...)). The values are CUSTOMER DATA from store A spliced into SQL for store
# B, so injection is the design constraint - handled MECHANICALLY here (a strict allowlist +
# quote-safe literalization), never by an LLM prompt, so #230's schema-only property is preserved.

_BIND_VALUE_OK = re.compile(r"^[A-Za-z0-9 _.\-]{1,64}$")
_BIND_NUMERIC = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
_GROUP_BY = re.compile(r"\bgroup\s+by\s+([A-Za-z_][\w.]*)", re.I)

#: The disclosed key cap ADR 0014-B settled on shape but not size. #219's original 32 was
#: sized for top-N breakdown alignment; the #474 filter-carry needs the cap to hold a
#: REAL filter's key set (D-001: 296 RJ customers), and an IN list of a few hundred
#: allowlisted values is comfortably inside every engine's limits. A filter matching MORE
#: than this still fails closed with a disclosed decline - never a partial total.
KEY_CARRY_CAP = 500
_MAX_BIND_VALUES = KEY_CARRY_CAP


def sanitize_bind_values(values: list) -> tuple:
    """Allowlist each join-key value before it can touch SQL. Kept iff it matches
    `^[A-Za-z0-9 _.\\-]{1,64}$`; anything else (a quote, semicolon, comment marker, paren,
    percent, backslash, over-length) is DROPPED - never escaped, never passed. De-dupes and
    caps at `_MAX_BIND_VALUES` (by construction <= the rows half A showed). Returns
    (kept, dropped_count) so the drop can be DISCLOSED, never silent.

    Because a quote character cannot survive the allowlist, a single-quoted literal built from a
    kept value cannot be broken out of: escape bugs are structurally impossible, not merely
    handled."""
    kept: list = []
    dropped = 0
    seen: set = set()
    for v in values:
        s = "" if v is None else str(v).strip()
        if len(kept) >= _MAX_BIND_VALUES:
            dropped += 1
            continue
        if _BIND_VALUE_OK.match(s) and s not in seen:
            seen.add(s)
            kept.append(s)
        else:
            dropped += 1
    return kept, dropped


def bind_literal(value: str) -> str:
    """A KEPT (already-sanitized) value as a SQL literal. All-numeric -> unquoted so numeric keys
    compare as numbers; everything else single-quoted. The allowlist has already guaranteed the
    value carries no quote character, so the quoting cannot be escaped."""
    return value if _BIND_NUMERIC.match(value) else "'" + value + "'"


def groupby_column(sql: str) -> "str | None":
    """The output column a GROUP BY groups on, alias-prefix stripped (`p.ProductNumber` ->
    `ProductNumber`), or None when the query does not group. The stripped name is BOTH the
    semantic join key and the column that the wrapping `SELECT * FROM (...) AS _semi` exposes,
    so it is what the outer `WHERE ... IN (...)` must reference.

    A composite `GROUP BY a, b` yields only the FIRST key (`a`); the semi-join then binds on `a`
    alone, which is over-broad but still NARROWING (LAW 2 holds) and honest. Single-key grouping
    is the canonical federated-question shape; a full composite semi-join is out of scope here."""
    m = _GROUP_BY.search(sql or "")
    if not m:
        return None
    return m.group(1).rsplit(".", 1)[-1]


#: A bare single-column projection: `SELECT [DISTINCT] [t.]col FROM ...` - exactly one
#: plain column reference, no function call, before FROM.
_PROJECTION = re.compile(
    r"^\s*SELECT\s+(?:DISTINCT\s+)?((?:[A-Za-z_]\w*\s*\.\s*)?[A-Za-z_]\w*)\s+FROM\b",
    re.I)


def projection_column(sql: str) -> "str | None":
    """The single column a plain projection selects (#474 gate 2), alias-prefix stripped,
    or None for anything else - an aggregate, a multi-column list, a breakdown, a UNION.
    'Which customers are in RJ' generates `SELECT customer_id FROM customers WHERE ...`,
    and that column is the join key the measure half needs."""
    s = sql or ""
    if re.search(r"\bGROUP\s+BY\b|\bUNION\b", s, re.I):
        return None
    m = _PROJECTION.match(s)
    if not m:
        return None
    return re.sub(r"\s+", "", m.group(1)).rsplit(".", 1)[-1]


#: Clause keywords that end a WHERE body - an injected predicate must land before them.
_TAIL_CLAUSE = re.compile(r"\b(GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT)\b", re.I)


def inject_predicate(sql: str, predicate: str) -> "str | None":
    """`sql` with `predicate` ANDed into its WHERE clause (#474 gate 3), or None when the
    statement is a shape this cannot do safely (a CTE, a UNION - their WHERE ownership is
    ambiguous, and a predicate landing on the wrong arm silently changes the answer).

    The existing WHERE body is parenthesized before the AND so an `a OR b` filter keeps
    its meaning. The predicate itself is built by the caller from schema names and
    `bind_literal`-quoted allowlisted values - nothing user-written enters here."""
    s = (sql or "").strip().rstrip(";")
    if re.match(r"^\s*WITH\b", s, re.I) or re.search(r"\bUNION\b", s, re.I):
        return None
    m = _TAIL_CLAUSE.search(s)
    head, tail = (s[:m.start()], s[m.start():]) if m else (s, "")
    where = re.search(r"\bWHERE\b", head, re.I)
    if where:
        body = head[where.end():].strip()
        head = f"{head[:where.start()]}WHERE ({body}) AND {predicate} "
    else:
        head = f"{head.rstrip()} WHERE {predicate} "
    return (head + tail).strip()


def _split_final_select(sql: str) -> tuple:
    """Split `sql` into (with_prefix, final_select) at its top-level SELECT: the leading WITH-CTE
    definitions (empty for a plain SELECT) and the outermost SELECT that follows.

    Why the semi-join needs this: `SELECT * FROM (<sql>) AS _semi WHERE ...` is invalid when
    `<sql>` is a CTE - T-SQL (and others) forbid `FROM (WITH ... SELECT ...)`. But a CTE is in
    scope for the WHOLE statement, INCLUDING a derived table in its final SELECT's FROM, so the
    correct wrap keeps the WITH at the top and wraps only the final SELECT:
        `WITH c AS (...) SELECT * FROM (<final select over c>) AS _semi WHERE key IN (...)`.
    The first SELECT at paren-depth 0 (outside string literals) IS that final select - every CTE's
    own SELECT sits inside its `( )` at depth >= 1. For a plain SELECT the split point is index 0,
    so the whole query wraps exactly as before."""
    depth = 0
    in_str = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if in_str:
            if ch == "'":
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and ch in "sS" and re.match(r"select\b", sql[i:i + 7], re.I) \
                and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            return sql[:i], sql[i:]
        i += 1
    return "", sql


_ORDER_BY = re.compile(r"order\s+by\b", re.I)


def _strip_trailing_order_by(sql: str) -> str:
    """Drop a trailing top-level ORDER BY. Wrapping `... ORDER BY x` as a derived table
    (`SELECT * FROM (... ORDER BY x) AS t`) is INVALID in T-SQL and in SQL views/subqueries
    generally, unless a TOP/OFFSET is also present - caught live on Azure SQL. The inner ordering
    is cosmetic anyway once the result is filtered to the small, already-ranked key set the
    semi-join binds to. Only a depth-0 ORDER BY outside string literals is stripped, so an ORDER
    BY inside a window function (`... OVER (ORDER BY ...)`) or a CTE body is left untouched."""
    depth = 0
    in_str = False
    i, n, cut = 0, len(sql), -1
    while i < n:
        ch = sql[i]
        if in_str:
            if ch == "'":
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and ch in "oO" and _ORDER_BY.match(sql[i:]) \
                and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            cut = i
        i += 1
    return sql[:cut].rstrip() if cut != -1 else sql


def semijoin_wrap(sql: str, col: str, values: list) -> str:
    """Constrain `sql` to `col IN (<sanitized values>)` - the #219 semi-join wrap. Keeps any
    leading CTE prefix at the top (see `_split_final_select`), strips the final SELECT's trailing
    ORDER BY (illegal inside a derived table), and wraps that SELECT as `... AS _semi WHERE ...`.
    `values` MUST already be sanitized (`sanitize_bind_values`); each is literalized quote-safely."""
    prefix, body = _split_final_select(sql)
    body = _strip_trailing_order_by(body)
    in_list = ", ".join(bind_literal(v) for v in values)
    return f"{prefix}SELECT * FROM ({body}) AS _semi WHERE {col} IN ({in_list})"


_TRAILING_CLAUSE = re.compile(
    r"\b(?:group\s+by|having|order\s+by|limit|window|fetch\s+first|offset)\b", re.I)


def _repair_empty_carry() -> bool:
    """#504's kill switch. `DBSEARCH_REPAIR_EMPTY_CARRY=0` restores the pre-#504 behaviour
    exactly (an empty carry source stays empty), so the Gate's reversibility check is a
    config change rather than a revert. Anything unparseable stays ON - the default is the
    measured behaviour, and a typo must not silently disable a shipped fix."""
    raw = (os.environ.get("DBSEARCH_REPAIR_EMPTY_CARRY") or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def empty_aggregate(cols: list, rows: list) -> bool:
    """Is this result a single-cell aggregate that came back 0 or NULL (#476)?

    `SELECT COUNT(*) ... WHERE <filter>` over zero matching rows returns ONE row holding 0,
    which is indistinguishable downstream from a genuine zero - and the product asserted it:
    "0 players in the register were born in the Dominican Republic", where the stored
    encoding is 'D.R.'. That is a falsehood a user cannot detect.

    Narrow on purpose. Zero rows is already EMPTY and needs no help; a multi-column
    breakdown containing a zero row is a different shape and is left alone."""
    if len(cols) != 1 or len(rows) != 1 or len(rows[0]) != 1:
        return False
    value = rows[0][0]
    return value is None or (isinstance(value, (int, float))
                             and not isinstance(value, bool) and value == 0)


def predicate_probes(sql: str, dialect: str = "") -> list:
    """Split a WHERE clause into its top-level AND terms and return one
    `(predicate, probe_sql)` pair per term, each probe asking only "does this condition
    match ANY row on its own?".

    This is the evidence that separates a real zero from a filter that matched nothing, and
    it is cheap and stays in the tenant: the probe reuses the query's own FROM/JOIN clause
    and selects a literal 1. Nothing about the stored values leaves the engine - the answer
    is one bit per predicate.

    Deliberately conservative. OR is not split (a disjunction can be legitimately empty in
    one arm), subqueries and anything past GROUP BY/HAVING/ORDER BY/LIMIT are excluded, and
    an unparseable WHERE returns no probes at all - which leaves the existing behaviour
    exactly as it was. A no-op is always the safe direction here: the fix must never turn a
    correct answer into a decline."""
    prefix, body = _split_final_select(sql)
    if prefix:                                        # a CTE: the FROM clause is not portable
        return []
    match = re.search(r"\bfrom\b", body, re.I)
    where = re.search(r"\bwhere\b", body, re.I)
    if not match or not where:
        return []
    from_clause = body[match.start():where.start()].strip()
    tail = body[where.end():]
    stop = _TRAILING_CLAUSE.search(tail)
    conditions = tail[:stop.start()] if stop else tail
    if re.search(r"\bselect\b", conditions, re.I):
        return []                                     # a subquery: do not reason about it
    # Skip only the TERMS that cannot be reasoned about, not the whole clause. A generator
    # that hedges one condition into a parenthesised OR - which #477 shows it does
    # routinely - used to make every OTHER condition unprobeable too, so the query that
    # matched nothing because of a plain `status = 'canceled'` sailed through unexamined.
    # A disjunction itself stays excluded: one arm of an OR can be legitimately empty.
    # The exclusion is OR, not parentheses: `LOWER(col) = 'x'` is a function call and must
    # stay probeable, while `(a IS NULL OR b > c)` is a disjunction whose arms may each be
    # legitimately empty.
    # #714: on a T-SQL engine the LIMIT-form probe is a parse error, which the caller
    # treats as "no opinion" — so the whole #476/#479 literal-repair rail was silently
    # dead on Azure SQL and Synapse. TOP is the same one-bit probe in that dialect.
    def _probe(term: str) -> str:
        if _is_tsql(dialect):
            return f"SELECT TOP 1 1 {from_clause} WHERE {term}"
        return f"SELECT 1 {from_clause} WHERE {term} LIMIT 1"

    return [(term, _probe(term))
            for term in _split_top_level_and(conditions)
            if not re.search(r"\bor\b", term, re.I)]


def _split_top_level_and(conditions: str) -> list:
    """Split on AND at paren-depth 0 and outside string literals.

    A regex split would cut `category = 'bed bath and table'` in half, and `LOWER(x) = 'y'`
    is a function call, not a subquery - both have to survive. Same scanning idiom as
    `_split_final_select`, for the same reason: SQL text is not regular."""
    terms, start, depth, in_str, i = [], 0, 0, False, 0
    while i < len(conditions):
        ch = conditions[i]
        if in_str:
            in_str = ch != "'"
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and re.match(r"\band\b", conditions[i:i + 4], re.I) \
                and (i == 0 or not conditions[i - 1].isalnum()):
            terms.append(conditions[start:i].strip())
            start = i = i + 3
            continue
        i += 1
    terms.append(conditions[start:].strip())
    return [t for t in terms if t]


def describe_schema(schema: list, descriptions: dict) -> list:
    """Attach AUTHORED table/column descriptions to a schema, as a new list (#486).

    `descriptions` is `{table: {"": table_text, column: column_text}}` - the empty key is
    the table's own description. Unknown tables and columns are ignored rather than
    invented, a missing description leaves that entry byte-identical, and the caller's
    schema is never mutated.

    **LAW 1 lives on this line.** An AUTHORED description ("order lifecycle state") is
    metadata: a human wrote it about the schema, and it may travel to the model. A
    description DERIVED from rows ("contains delivered, shipped, canceled") is customer
    data wearing a label, and this function must never be handed one - it would put values
    into a prompt bound for Azure OpenAI / Anthropic / Groq, which is exactly what #462's
    value linking and #479's server-side resolution exist to avoid. The only source wired
    to it is store CONFIG, alongside title and description; nothing on this path reads a
    row.

    Why it exists: the generator was given `orders(order_id TEXT, order_status TEXT, ...)`
    and nothing else, so it had to guess what every column MEANT. Measured on the real
    pack (#473), routing reached the right store in 31 of 32 questions and every one of the
    five confidently-wrong answers was a meaning failure downstream of that - "how many
    parcels made it into the buyer's hands" answered by counting rows with a freight value,
    because nothing said `order_status='delivered'` is what that means."""
    described = []
    for table in schema:
        entry = dict(table)
        table_desc = (descriptions or {}).get(table.get("table"), {})
        if table_desc.get(""):
            entry["comment"] = table_desc[""]
        columns = []
        for column in table.get("columns", []):
            column = dict(column)
            text = table_desc.get(column.get("name"))
            if text:
                column["description"] = text
            columns.append(column)
        entry["columns"] = columns
        described.append(entry)
    return described


def _sniffed_types(spec: dict) -> list:
    """One declared SQL type per column, inferred from the rows (#481).

    An embedded store used to be created as `CREATE TABLE t ("a", "b")` - no types at all -
    so `schema()` reported EVERY column as TEXT and the model was told a 1-5 review score
    was text. Its defensive `REPLACE(SUBSTR(review_score, 3), ',', '')` was the rational
    response to that: strip separators before trusting a number that is "text". SUBSTR from
    position 3 of "4" is empty, and the answer was an average review score of 0.0.

    Declaring the type fixes a second failure for free. Without an affinity SQLite compares
    a REAL column against '2015' as text and matches nothing, which is how B-003 answered
    "the highest number of home runs any club hit in the 2015 season is None" (#475). With
    REAL declared, affinity converts the literal and the comparison works.

    Numeric-vs-text is decided by the same rule the independent gold engine uses
    (`eval/golden/stage2._load_csv`): numeric only when EVERY value parses as a number, so
    a numeric-looking value in an otherwise-text column stays text and the two engines
    cannot disagree about which columns are numbers.

    Whole numbers are declared INTEGER rather than REAL so a count of 100 keeps reading as
    "100" in the answer instead of becoming "100.0". Both affinities convert a quoted
    literal; only one of them changes what the reader sees."""
    rows = spec.get("rows") or []
    types = []
    for i in range(len(spec["columns"])):
        values = [r[i] for r in rows if i < len(r)]
        if not values or not all(_looks_numeric(v) for v in values):
            types.append("TEXT")
        else:
            types.append("INTEGER" if all(float(v).is_integer() for v in values) else "REAL")
    return types


def _or_null(value):
    """A blank cell is a MISSING value, so it loads as NULL (#490).

    It used to load as `''`, and every SQL idiom for absence stopped working. Asked how
    many orders were called off before delivery, the model wrote the reasonable
    `order_status = 'canceled' AND delivered_on IS NULL`; the empty strings meant IS NULL
    matched nothing, the query returned zero, and the answer was "there were no orders
    called off before delivery" against a gold of 9. Same shape as #481 - the model's
    instinct was right and the engine was misrepresenting the data.

    The type rule above is deliberately NOT relaxed to suit this: a blank still makes its
    column TEXT, the same call the independent gold engine makes, so the two engines cannot
    disagree about which columns are numbers."""
    return None if isinstance(value, str) and not value.strip() else value


def _looks_numeric(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    try:
        float(str(value))
        return True
    except (TypeError, ValueError):
        return False


#: String functions that do real work on TEXT and nothing but damage on a number.
_STRING_OPS = ("substr", "substring", "replace", "trim", "ltrim", "rtrim", "upper", "lower")
_STRING_OP_CALL = re.compile(rf"\b({'|'.join(_STRING_OPS)})\s*\(", re.I)
_NUMERIC_TYPE = re.compile(r"int|real|float|double|decimal|numeric|money|bigint", re.I)
_COLUMN_REF = re.compile(r"^(?:[A-Za-z_]\w*\s*\.\s*)?([A-Za-z_]\w*)$")


def _numeric_columns(schema: list) -> set:
    """Lowercased names of columns that are numeric in EVERY table declaring them.

    A name that is numeric in one table and text in another is ambiguous without full
    alias resolution, so it is excluded - the rewrite below must never fire on a column
    whose type it is not certain of."""
    numeric, textual = set(), set()
    for table in schema or []:
        for column in table.get("columns", []):
            name = str(column.get("name", "")).lower()
            (numeric if _NUMERIC_TYPE.search(str(column.get("type", ""))) else textual).add(name)
    return numeric - textual


#: #729(a) — how a value should be READ, in the smallest vocabulary that is enough to render
#: it honestly. Ordered, first match wins: `datetime` must beat `date`, and `numeric` must be
#: settled before `int` (`bigint` contains `int`, and so does `point`-free `integer`).
#:
#: These are the DECLARED types out of information_schema, which every engine's `schema()`
#: already returns as a string - not DB-API cursor type codes, which are integers in some
#: drivers and absent in others. That is why the schema is the source here and the cursor is
#: not, and it is the same source #481 settled the string-op rewrite from.
_VALUE_CLASS = (
    ("ts",   re.compile(r"timestamp|datetime|smalldatetime|\btime\b", re.I)),
    ("date", re.compile(r"\bdate\b", re.I)),
    # FRACTIONAL types only. An integer column is deliberately NOT grouped: nothing in a type
    # separates a count from an identifier, and `customer_id=29485` rendered as `29,485` is the
    # #729 falsification all over again - a value the reader cannot paste back into a query.
    ("num",  re.compile(r"decimal|numeric|money|real|float|double|\bnumber\b", re.I)),
)


def value_classes(schema: list) -> dict:
    """Lowercased column name -> how to READ its values ("num" / "date" / "ts").

    Same certainty rule as `_numeric_columns` above, and for the same reason: a name that is
    numeric in one table and text in another cannot be resolved without full alias resolution,
    so it is dropped rather than guessed. A column missing from this map renders exactly as the
    database returned it, which is the behaviour every column has today - so being wrong about
    a type costs a formatting opportunity, never a value.

    This exists because the client CANNOT do it. `period=2024.06` and `price=1200.50` are the
    same string shape; only the declared type separates them, and the shape-based formatter
    that tried was falsified on `app_version=2024.1.0` -> `2,024.1.0`. The type is knowable
    here and nowhere downstream, so it is settled here (#481's rule, applied to rendering)."""
    seen: dict = {}
    for table in schema or []:
        for column in table.get("columns", []):
            name = str(column.get("name", "")).lower()
            if not name:
                continue
            declared = str(column.get("type", ""))
            cls = next((c for c, rx in _VALUE_CLASS if rx.search(declared)), None)
            if name in seen and seen[name] != cls:
                seen[name] = None       # ambiguous across tables - never guess
            else:
                seen.setdefault(name, cls)
    return {n: c for n, c in seen.items() if c}


def _close_paren(sql: str, open_at: int) -> int:
    """Index of the paren matching the one at `open_at`, or -1. String-literal aware."""
    depth, in_str, i = 0, False, open_at
    while i < len(sql):
        ch = sql[i]
        if in_str:
            in_str = ch != "'"
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _first_argument(args: str) -> str:
    """The first top-level comma-separated argument, trimmed."""
    depth, in_str = 0, False
    for i, ch in enumerate(args):
        if in_str:
            in_str = ch != "'"
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            return args[:i].strip()
    return args.strip()


def strip_string_ops_on_numeric(sql: str, schema: list) -> str:
    """Remove string functions wrapped around a NUMERIC column (#481).

    Asked a paraphrased question the model hedges defensively against the declared type,
    and against a `review_score REAL` holding 1-5 it wrote
    `AVG(CAST(REPLACE(SUBSTR(review_score, 3), ',', '') AS REAL))`. SUBSTR from position 3
    of "4" is empty, CAST('' AS REAL) is 0.0, and the answer was "shoppers were typically
    very dissatisfied, with an average review score of 0.0". Gold was 3.79, and nothing
    about that reads as a failure to the person receiving it.

    The server knows the type, so this does not have to be argued with the model (#254's
    lesson: stability is ours to impose). A number has no commas to strip and no substring
    to take, so the wrapper is provably a no-op at best and destructive at worst - but ONLY
    when the argument is EXACTLY a numeric column reference. An expression, a literal, a
    text column, an unknown column or a name that is numeric in one table and text in
    another all leave the query untouched, because there the function may be doing real
    work. Applied innermost-outward until it reaches a fixpoint, so nesting unwinds."""
    numeric = _numeric_columns(schema)
    if not numeric:
        return sql
    for _ in range(8):                       # fixpoint; the bound is a runaway guard
        rewritten = _strip_one(sql, numeric)
        if rewritten == sql:
            return sql
        sql = rewritten
    return sql


def _strip_one(sql: str, numeric: set) -> str:
    for match in _STRING_OP_CALL.finditer(sql):
        close = _close_paren(sql, match.end() - 1)
        if close < 0:
            continue
        argument = _first_argument(sql[match.end():close])
        column = _COLUMN_REF.match(argument)
        if column and column.group(1).lower() in numeric:
            return sql[:match.start()] + argument + sql[close + 1:]
    return sql


_AGG_MEASURE = re.compile(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\([^()]*\)(?:\s+AS\s+(\w+))?", re.I)


def rank_by_measure(sql: str) -> str:
    """Re-order a GROUP BY breakdown by its aggregated MEASURE descending - #232, the twin of #207.

    A breakdown shows only `top_k` of its groups, so those groups must be the ones that MATTER.
    An LLM asked "how many tickets for each product" often writes `... GROUP BY product_number
    ORDER BY product_number` - ordered by the KEY, so the shown rows are an ALPHABETICAL accident
    (BK-M68B-38, BK-M68B-42, ...) rather than the most-ticketed. Fed into a #219 semi-join that is
    fine for CORRECTNESS (the halves still align on those keys) but poor for the ANSWER: it
    compares an arbitrary five products instead of the top five.

    So rewrite the trailing ORDER BY to the aggregate the query already computes (its alias if it
    has one, else the aggregate expression), DESC. No GROUP BY, or no aggregate to rank by, or a
    CTE (whose aggregate/alias may not be visible at the outer level): return unchanged - a
    fail-safe no-op, never a broken query.

    Applied ONLY to a semi-join CARRY-SOURCE half (`retrieve_ranked`), never globally: a global
    ORDER-BY-measure changes the row order of EVERY breakdown, which destabilized the answer
    model's citation numbering on multi-store answers (a dangling `[n]`) - the reason the broad
    prompt rule was reverted from #219."""
    if re.match(r"\s*with\b", sql, re.I):        # CTE: outer ORDER BY may not see the inner alias
        return sql
    if groupby_column(sql) is None:              # not a breakdown - nothing to rank
        return sql
    m = _AGG_MEASURE.search(sql)
    if not m:                                    # no aggregate to order by
        return sql
    measure = m.group(1) or m.group(0)           # the alias if present, else the whole COUNT(...)
    return f"{_strip_trailing_order_by(sql)} ORDER BY {measure} DESC"


def _depth0_tail(sql: str) -> str:
    """The part of `sql` outside every parenthesised group — for a CTE, its OUTER query.

    Used to decide whether a measure ALIAS declared inside a CTE body is actually referenceable
    from the outer ORDER BY. `WITH c AS (... COUNT(*) n ...) SELECT a FROM c` declares `n` but
    never selects it, so `ORDER BY n` would be invalid SQL."""
    out, depth, in_str = [], 0, False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
        if not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
                continue
        if depth == 0 and not (ch == "("):
            out.append(ch)
    return "".join(out)


def _trailing_order_by(sql: str) -> "str | None":
    """The depth-0 trailing ORDER BY clause body, or None. Mirrors `_strip_trailing_order_by`'s
    scanning so the two never disagree about which ORDER BY is 'the' one."""
    stripped = _strip_trailing_order_by(sql)
    if stripped == sql:
        return None
    return sql[len(stripped):].strip()[len("ORDER BY"):].strip()


def _outer_select_list(sql: str) -> str:
    """The outer query's SELECT list — what it actually PROJECTS, between its top-level SELECT
    and FROM. ORDER BY may only reference something the query selects (or groups by), so this,
    not the whole outer query, is what decides whether an alias is orderable."""
    tail = _depth0_tail(sql)
    m = re.search(r"\bselect\b(.*?)\bfrom\b", tail, re.I | re.S)
    return m.group(1) if m else ""


def _cte_measure_alias(sql: str) -> "str | None":
    """For a CTE, the aggregate's alias IF the OUTER query actually selects it.

    #267: this used to accept the alias appearing anywhere at depth 0, which is not the same
    thing. "how many product skus have total revenue over 10000" produces
    `... SELECT COUNT(*) FROM product_revenue WHERE total_revenue > 10000` — the alias is
    there, in the WHERE clause, but the outer query is a SCALAR AGGREGATE that projects only
    COUNT(*). Appending ORDER BY total_revenue made SQL Server reject the whole statement
    ("not contained in either an aggregate function or the GROUP BY clause"), so a question
    that used to work returned nothing at all. Being mentioned is not being selected."""
    m = _AGG_MEASURE.search(sql)
    alias = m.group(1) if m else None
    if not alias:
        return None                              # no alias: unreferenceable outside the body
    projected = _outer_select_list(sql)
    if not projected or re.search(r"\*", projected):
        return None                              # SELECT * / unparsed: do not guess
    if re.search(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(", projected, re.I):
        return None                              # scalar aggregate outer query — nothing to order
    return alias if re.search(rf"\b{re.escape(alias)}\b", projected) else None


def rank_grouped_default(sql: str) -> str:
    """#207: order a truncated breakdown by its MEASURE — the conservative, always-on twin of
    #232's `rank_by_measure`.

    Why this exists separately. #232 rewrites unconditionally, which is right for a semi-join
    carry source (ranking IS the point there). As a DEFAULT it would be too blunt: it strips any
    trailing ORDER BY, so a question that asked for a particular order would be silently
    re-sorted. So this variant only touches the ALPHABETICAL ACCIDENT — a breakdown with no
    ordering at all, or one ordered by its own GROUP BY key. Any other ORDER BY (already the
    measure, ASC for "smallest first", some third column) expresses intent and is left alone.

    It also handles the CTE shape #232 skips, because that is the shape the live failure took:
    `WITH r AS (SELECT k, SUM(x) AS total ... GROUP BY k) SELECT k, total FROM r ORDER BY k`.
    Safe only when the alias is visible in the outer query (`_cte_measure_alias`), otherwise the
    rewrite would emit invalid SQL — so that case stays a no-op.

    Fail-safe throughout: anything that is not a plain grouped aggregate returns unchanged."""
    key = groupby_column(sql)
    if key is None:                              # not a breakdown — nothing to rank
        return sql

    current = _trailing_order_by(sql)
    if current is not None:
        # only the key-ordered accident is ours to fix; anything else is intent
        first = re.split(r"\s|,", current.strip(), 1)[0]
        if first.split(".")[-1].lower() != key.lower():
            return sql

    if re.match(r"\s*with\b", sql, re.I):
        alias = _cte_measure_alias(sql)
        if not alias:
            return sql                           # alias not referenceable outside the CTE body
        return f"{_strip_trailing_order_by(sql)} ORDER BY {alias} DESC"

    m = _AGG_MEASURE.search(sql)
    if not m:
        return sql
    measure = m.group(1) or m.group(0)
    return f"{_strip_trailing_order_by(sql)} ORDER BY {measure} DESC"


def _value_for_column(content: str, col: str) -> "str | None":
    """Pull `col`'s value out of a `c1=v1, c2=v2` evidence-content string, anchored on the column
    name so a value that itself contains ', ' cannot silently shift the parse onto the next field
    (an over-captured value is then dropped by the allowlist, not mis-bound)."""
    m = re.search(r"(?:^|, )" + re.escape(col) + r"=(.*?)(?:, [A-Za-z_][\w.]*=|$)", content or "")
    return m.group(1) if m else None


def bind_values_from_evidence(evidence: list) -> tuple:
    """From half A's SHOWN evidence rows, recover (join_column, values) to carry into half B.

    Two shapes carry (#219, #474 gate 2):

    - a GROUP BY breakdown - the column is A's GROUP BY; carrying only the shown top-N is
      the #219 contract (line the halves up row-for-row), deliberately partial.
    - a plain single-column projection ("SELECT customer_id FROM customers WHERE ...") -
      the filter half of every ordinary filter-here-measure-there question. Here partial
      is NOT alignment, it is a wrong total waiting to happen: half B would aggregate a
      SUBSET of the filter's matches and assert it as the answer. So a projection whose
      total_rows exceeds what was shown refuses to carry - the ADR 0014 cliff, fail
      closed - and a multi-column projection refuses too (no unambiguous key).

    Returns (None, []) when nothing carries; the next half then runs unbound."""
    if not evidence:
        return None, []
    prov = evidence[0].provenance or {}
    sql = prov.get("sql") or prov.get("query") or ""
    col = groupby_column(sql)
    if not col:
        col = projection_column(sql)
        if not col:
            return None, []
        total = prov.get("total_rows")
        if isinstance(total, int) and total > len(evidence):
            return None, []                    # partial projection carry: the cliff
    values = [v for v in (_value_for_column(ev.content, col) for ev in evidence) if v is not None]
    return col, values


class SqlEnginePort(ABC):
    """Where the SQL runs. Embedded (sqlite/DuckDB, in-tenant) or remote pushdown
    (BQ/Synapse/Redshift as the delegated user)."""

    @abstractmethod
    def schema(self) -> list:
        """[{table, columns: [{name, type}]}] — auto-introspected (§11.3)."""

    def row_counts(self) -> "dict | None":
        """{table: n} for the admin snapshot (#562), or None when this engine cannot count.

        NOT abstract, and None rather than {} on purpose. A federated engine may be unable
        to count cheaply, or at all under the caller's grants — and an operator reading
        "0 rows" on a warehouse that is full makes a wrong decision confidently. None means
        "not reported by this engine" and the surface says so; {} would mean "no tables" and
        0 would mean "empty". Unknown is not the same as empty (#392).

        Deliberately not built on the NL2SQL path: the "how many records" probe the health
        canary uses answers for ONE table and lets the generator pick which, so it cannot
        produce a per-table snapshot without guessing.
        """
        return None

    @abstractmethod
    def execute(self, sql: str, credential: "str | None" = None,
                principal: "str | None" = None) -> tuple:
        """-> (column_names, rows). `credential` is the E5 delegated credential (ADR
        0006 query-as-the-user); engines that can't present it MUST fail closed —
        silently running as the service identity would break the delegation promise
        (LAW 2). Embedded engines (sqlite) ignore it: they hold in-tenant derived
        data already trimmed upstream.

        `principal` (#193) is the session identity (the signed-in user's email), threaded
        for engines whose delegated credential is an OPAQUE token that does not itself carry
        the principal - Cloud SQL / Google IAM database auth authenticates as `principal` with
        the Google access token as the password. Engines whose token IS the principal (Azure
        Entra JWTs) or that don't delegate ignore it."""

    def fk_edges(self) -> list:
        """[(referencing_table, referenced_table)] with names as schema() spells them.
        Default: none. Edges feed the #221 schema index join graph - an optimization
        signal, so introspection failures must return [] rather than raise. Warehouses
        (Synapse/Redshift/BigQuery) declare no FKs; the index compensates with
        inferred name/type edges. CONCRETE, not abstract: seven providers subclass this
        port and none of them introspect FKs today - making this abstract would break
        every one of them."""
        return []

    def refresh_schema(self) -> None:
        """Drop any cached introspection so the next schema() call hits the engine.
        The #221 widen retry calls this - the second retrieval chance doubles as the
        cheap answer to schema drift between composes. Concrete no-op default for
        engines that don't cache; cache-holding engines override to clear."""
        pass


class SqliteEngine(SqlEnginePort):
    # #714: what the NL2SQL generator is told about the target syntax. Class-level,
    # config-free: the dialect is a property of the ENGINE, not of any one store.
    dialect = "SQLite"

    def __init__(self, conn) -> None:
        self._conn = conn

    @classmethod
    def from_tables(cls, tables: dict) -> "SqliteEngine":
        import sqlite3

        # check_same_thread=False: the E3 executor retrieves from worker threads; each
        # dispatch touches the connection from exactly one thread at a time.
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        for name, spec in tables.items():
            cols = ", ".join(f'"{c}" {t}' for c, t in
                             zip(spec["columns"], _sniffed_types(spec)))
            conn.execute(f'CREATE TABLE "{name}" ({cols})')
            ph = ", ".join("?" for _ in spec["columns"])
            conn.executemany(f'INSERT INTO "{name}" VALUES ({ph})',
                             [[_or_null(v) for v in row] for row in spec.get("rows", [])])
        conn.commit()
        return cls(conn)

    @classmethod
    def from_csv_files(cls, paths: list) -> "SqliteEngine":
        import csv
        from pathlib import Path as _P

        tables: dict = {}
        for p in paths:
            with open(p, newline="") as fh:
                rows = list(csv.reader(fh))
            if not rows:
                continue
            name = re.sub(r"\W+", "_", _P(p).stem) or "data"
            tables[name] = {"columns": rows[0], "rows": rows[1:]}
        return cls.from_tables(tables)

    def schema(self) -> list:
        out = []
        names = [r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in names:
            cols = [{"name": r[1], "type": r[2] or "TEXT"}
                    for r in self._conn.execute(f'PRAGMA table_info("{name}")')]
            out.append({"table": name, "columns": cols})
        return out

    def row_counts(self) -> "dict | None":
        """Exact, per table (#562). Embedded and in-tenant, so COUNT(*) is cheap and there is
        no delegation to honour — this engine holds derived data already trimmed upstream."""
        out = {}
        for row in self.schema():
            name = row["table"]
            # The table name comes from sqlite_master, not from a caller, and is quoted
            # anyway — this is not a place to accept an outside identifier.
            out[name] = self._conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        return out

    def execute(self, sql: str, credential: "str | None" = None,
                principal: "str | None" = None) -> tuple:
        cur = self._conn.execute(sql)     # embedded in-tenant engine: no delegation
        cols = [d[0] for d in cur.description] if cur.description else []
        return cols, cur.fetchall()

    def fk_edges(self) -> list:
        out = []
        names = [r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for name in names:
            for row in self._conn.execute(f'PRAGMA foreign_key_list("{name}")'):
                out.append((name, row[2]))       # row[2] = referenced table
        return out


def keyword_sql_generator(question: str, schema: list, top_k: int = 5,
                          dialect: str = "") -> str:
    """Deterministic naive NL2SQL over the FIRST table — the demo default, loudly not a
    real generator (that is a schema-grounded LlmPort job behind the same seam).

    #714: dialect-aware in its ONE dialect-sensitive production, the broad-SELECT row
    cap. This generator is also the FALLBACK when a model generation fails validation,
    and a fallback that emits `LIMIT` against a T-SQL engine turns a recoverable bad
    generation into a hard parse error — the exact 42000 the matrix caught live."""
    q = question.lower()
    table = schema[0]["table"]
    columns = [c["name"] for c in schema[0]["columns"]]

    def mentioned(candidates):
        for c in candidates:
            if re.search(r"\b" + re.escape(c.lower()) + r"\b", q):
                return c
        return None

    if re.search(r"\b(how many|count)\b", q):
        return f"SELECT COUNT(*) AS count FROM {table}"
    agg = "SUM" if re.search(r"\b(total|sum)\b", q) else \
          "AVG" if re.search(r"\b(average|avg|mean)\b", q) else None
    if agg:
        by = re.search(r"\bby\s+(\w+)", q)
        group = by.group(1) if by and by.group(1) in [c.lower() for c in columns] else None
        col = mentioned([c for c in columns if c.lower() != group]) or \
            next((c for c in columns if c.lower() != group), columns[-1])
        label = ("total_" if agg == "SUM" else "avg_") + col
        if group:
            return f"SELECT {group}, {agg}({col}) AS {label} FROM {table} GROUP BY {group}"
        return f"SELECT {agg}({col}) AS {label} FROM {table}"
    if _is_tsql(dialect):
        return f"SELECT TOP {top_k} * FROM {table}"
    return f"SELECT * FROM {table} LIMIT {top_k}"


MIN_SCHEMA_HASHING_DIM = 4096


def schema_index_embedder(embedder):
    """The embedder the SCHEMA INDEX may safely use (#225).

    A hashed bag-of-words embedder scores a NONZERO cosine between a question and a table that
    share ZERO tokens - such a score is, by definition, a hash collision. With few buckets those
    collisions are manufactured wholesale, and the collision floor RISES with table count, so at
    warehouse scale the ranking degrades into noise in BOTH directions: irrelevant questions stop
    declining, and the RIGHT table stops being retrieved. Measured on live AdventureWorks (Azure
    SQL, 12 tables), "who are our top 5 customers by total due":
        dim= 128: [ProductDescription, ProductModelProductDescription, Product, ProductModel,
                   CustomerAddress, SalesOrderHeader]      <- SalesLT.Customer MISSING
        dim=4096: [CustomerAddress, SalesOrderHeader, Customer]                   <- correct
    and at 160-table warehouse scale: dim=128 declined 0/4 irrelevant questions, dim=4096 declined
    4/4, at zero recall cost.

    So the schema index REQUIRES bucket capacity, and it cannot merely DEFAULT to it: the server
    THREADS its edition embedder into every SQL store (#222 Fix 2), and the self-host/dev edition's
    embedder is `HashingEmbedding()` - the 128-dim default. A default is bypassed by an argument;
    a requirement is not. Without this, Fix 2 silently cancelled the dim=4096 fix and shipped the
    noisy index to production - which is exactly what a live canvas run caught.

    A DENSE embedder (Azure OpenAI et al.) is left ALONE at any dimension: its dimensions are
    learned features, not hash buckets, so a small dim still carries real signal and none of this
    reasoning applies. The document rail's own 128-dim default is likewise untouched - a schema's
    vocabulary (every table x every column name) is far larger than a chunk of prose.
    """
    try:
        from dbsearch.adapters.local import HashingEmbedding
    except Exception:                    # adapters unavailable - take what we were given
        return embedder
    if embedder is None:
        return HashingEmbedding(dim=MIN_SCHEMA_HASHING_DIM)
    if isinstance(embedder, HashingEmbedding) and embedder.dim < MIN_SCHEMA_HASHING_DIM:
        return HashingEmbedding(dim=MIN_SCHEMA_HASHING_DIM)
    return embedder


CANNOT_ANSWER = "CANNOT_ANSWER"


class _UnrepairableFilterMiss(RuntimeError):
    """#495 - internal to FederatedSqlStore: an empty aggregate whose filter provably
    matched nothing (#476) and whose literal could not be repaired (#479). Measured cause
    on the real pack: the DESCRIBED prompt led llama3.1:8b to fabricate an extra
    predicate (E-001: `customer_id = 'customer'`), which no literal repair can save.
    retrieve() answers it by re-generating ONCE against the bare schema; every other
    entry point converts it back to the plain decline it always was. Never leaves the
    store."""


class CannotAnswerFromSchema(RuntimeError):
    """#211: this store does NOT hold the kind of data the question asks about.

    Not an error, and not "no rows matched" — the store is healthy and gave the honest answer.
    Asked which products have the most SUPPORT TICKETS, a sales database used to reply

        SELECT p.ProductNumber, COUNT(DISTINCT sod.SalesOrderDetailID) AS support_tickets ...

    counting order lines and CALLING them support tickets. The number was real; the label was
    invented, and it shipped with citations and a confident conclusion. `validate_sql` cannot
    catch that — the SQL is valid and touches only visible tables. The guard checks SAFETY, not
    SEMANTIC HONESTY. So the model is now told to decline, and a decline must reach the user as
    a decline.
    """


class SchemaUnavailable(RuntimeError):
    """#727: introspection returned ZERO tables - a fault of the SOURCE side, never of the data.

    An empty schema and an honest retrieval miss used to be indistinguishable: both fell
    through `_subset_for` to the same `CannotAnswerFromSchema`, so a store whose delegated
    credential lacked privileges - or whose `tables:` allowlist matched nothing, or whose
    introspection statement came back `HasResultSet: false` - told the owner it "holds no
    data of this kind". On prod that taught a user their freight store holds no freight.
    A decline is a claim about the DATA; this is a claim about the SOURCE, and the executor
    maps it to an ERROR whose remedy the user can actually act on.
    """


#: #808: the SQL engines all record their `tables:` allowlist here. Duck-typed on purpose -
#: the engines share no base class - and pinned by a guard that walks every provider class
#: and asserts the attribute exists, so a rename fails a test instead of silently downgrading
#: every allowlist warning to the generic one.
_ALLOWLIST_ATTR = "_allow"


def empty_schema_warnings(engine, schema) -> list:
    """#808: why is this store's schema empty, said at COMPOSE time.

    #727 made the ASK honest - a store with no visible tables raises SchemaUnavailable and
    the answer names the remedy. But the compose that created it still turned the node green
    and said nothing, so the owner learned about it only by asking a question and reading a
    failure. Everything needed to say it earlier is already here: the engine knows whether an
    allowlist is filtering, and the introspection has already run.

    The allowlist case gets its own sentence because it is the likelier cause and the one
    with a precise fix: a BARE entry matches only the default schema, deliberately (a bare
    name must not drag in a same-named table from another schema - that is a different table,
    LAW 2). That rule is right and stays; the silence around it is the defect.
    """
    if schema:
        return []
    if getattr(engine, _ALLOWLIST_ATTR, None):
        return ["This source connected, but the `tables:` allowlist matched none of its "
                "tables, so it can answer nothing. Entries must be schema-qualified "
                "(`schema.table`) - a bare name matches only the default schema."]
    return ["This source connected, but introspection returned 0 tables, so it can answer "
            "nothing. Check the credential's privileges on the source."]


def _strip_sql_fence(text: str) -> str:
    """Model output → bare SQL: drop ```sql fences and a leading 'sql' language tag."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    return s.strip().rstrip(";").strip()


# #222 Fix 1: is the generator currently looking at a NARROWED subset of the schema?
# Only the STORE knows (it is the thing that narrowed), but the knowledge is needed
# inside the generator, which is handed to the store already built and behind a fixed
# `(question, schema) -> sql` seam. A context var carries it across that seam without
# changing the seam: the store opens `retrieval_mode(True)` around the call, and any
# generator that degrades on failure consults it. Per-thread/per-task by construction,
# so the E3 executor's worker threads cannot see each other's mode.
_RETRIEVAL_MODE: contextvars.ContextVar = contextvars.ContextVar(
    "dbsearch_sql_retrieval_mode", default=False)


@contextlib.contextmanager
def retrieval_mode(active: bool):
    """Store-owned declaration: the schema handed to the generator is a RETRIEVED SUBSET,
    so a generation failure may be a RETRIEVAL MISS and must not be papered over."""
    token = _RETRIEVAL_MODE.set(active)
    try:
        yield
    finally:
        _RETRIEVAL_MODE.reset(token)


def in_retrieval_mode() -> bool:
    return _RETRIEVAL_MODE.get()


# The same seam, in the other direction (#482). When generation fails outside retrieval
# mode the store still gets a query back - the keyword generator's naive one - and used to
# get no way of knowing that is what it was holding. The degraded query was then executed,
# cited and answered over as though the model had written it.
#
# That silence had a measured cost. #477 was a one-token bug (`REPLACE(` read as the
# `REPLACE INTO` statement) and it survived because every symptom looked like a model that
# could not handle paraphrase: a question about salaries answered with `SELECT * FROM
# batting LIMIT 5`, and no surface anywhere saying why. The reason existed for a moment
# inside an `except` clause and was thrown away.
#
# Set by the generator, read by the store, cleared per generation so it cannot leak into
# the next question. Metadata only - an exception message about SQL, never a row (LAW 8).
_GENERATION_DEGRADED: contextvars.ContextVar = contextvars.ContextVar(
    "dbsearch_generation_degraded", default=None)


def generation_degraded() -> "str | None":
    """Why the last generation in this context degraded to the keyword generator, if it
    did. None when the model's own SQL was used."""
    return _GENERATION_DEGRADED.get()


#: #481: the query the model wrote, when the server had to normalise it. Same seam and
#: same discipline as _GENERATION_DEGRADED - a rewrite that is never recorded is a rewrite
#: nobody can audit.
_QUERY_NORMALIZED: contextvars.ContextVar = contextvars.ContextVar(
    "dbsearch_query_normalized", default=None)


def query_normalized() -> "str | None":
    """The model's original SQL, if this context's query was normalised. None otherwise."""
    return _QUERY_NORMALIZED.get()


def _schema_fingerprint(schema: list) -> tuple:
    """What the generator was shown: table+column names plus the AUTHORED #486
    descriptions. No values ever reach the generator (LAW 1), so nothing derived from
    customer data enters this key - descriptions are config someone wrote.

    Descriptions are part of the key because they are part of the PROMPT: the #495
    reprompt regenerates against the bare schema, and a names-only fingerprint made
    described and bare collide, so the retry was handed the cached described SQL - the
    exact generation it exists to escape."""
    return tuple(sorted(
        (str(t.get("table", "")), str(t.get("comment", "")),
         tuple(sorted((str(c.get("name", "")), str(c.get("description", "")))
                      for c in t.get("columns", []))))
        for t in (schema or [])))


def memoized_sql_generator(gen: SqlGenerator, max_entries: int = 256) -> SqlGenerator:
    """#254: make one question over one schema always produce the SAME SQL.

    The model runs at temperature 0.0 and STILL alternated — 6 identical asks of "total revenue
    for each product sku" produced LEFT JOIN (295 rows) three times and INNER JOIN (142) three
    times. LLMs are not bit-deterministic, so no sampling setting fixes this; the stability has
    to be imposed above the model.

    Both variants are defensible (a never-sold product has no revenue, or has zero), and since
    #207 both surface the same top rows. What flapped was the DENOMINATOR the user is told —
    "5 of 295" vs "5 of 142" for one identical question. Being told two different things by two
    identical asks is a trust failure whichever number you prefer, so this fixes the flapping
    rather than legislating a join.

    Deliberately NOT normalising the SQL itself: rewriting a model's join semantics would change
    what the query MEANS, and "include entities with no matching rows" is a real question a user
    can ask. Stability is ours to impose; semantics are the question's.

    Cached on (question, schema names) — the generated SQL is a function of exactly those, and by
    LAW 1 the generator never saw a value, so nothing customer-derived is stored or keyed on.
    Failures are never cached (that would freeze a bad generation for the process lifetime) and
    the cache is a bounded LRU (an unbounded one on a long-lived server is a leak)."""
    cache: "OrderedDict[tuple, str]" = OrderedDict()

    def _gen(question: str, schema: list, dialect: str = "") -> str:
        # #714: dialect is part of the key — one cached generation must never serve two
        # dialects (the same question is valid SQL on Postgres and a parse error on T-SQL).
        key = (" ".join(str(question or "").split()).lower(), _schema_fingerprint(schema),
               dialect)
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        if dialect and _accepts_dialect(gen):
            sql = gen(question, schema, dialect=dialect)
        else:
            sql = gen(question, schema)
        # #717: a DEGRADED generation (the model failed and the keyword fallback answered)
        # must not be memoized — that freezes one bad roll for the process lifetime, which
        # is exactly what this docstring promises never happens. #482 flags it per
        # generation; consult the flag before remembering.
        if sql and _GENERATION_DEGRADED.get() is None:   # only a real generation is worth remembering
            cache[key] = sql
            cache.move_to_end(key)
            while len(cache) > max_entries:
                cache.popitem(last=False)         # evict least-recently-used
        return sql

    return _gen


def _is_tsql(dialect: str) -> bool:
    """#714: the one dialect family in the fleet whose row-cap syntax is TOP, not LIMIT."""
    return "t-sql" in (dialect or "").lower()


def _accepts_dialect(fn) -> bool:
    """#714: does this callable declare a `dialect` keyword? Signature-read once per
    call, mirroring how the connector rail binds credentials by parameter name (#673):
    passing a kwarg a function never declared is a crash, and silently dropping the
    dialect for everyone instead would leave the T-SQL bug in place."""
    import inspect
    try:
        return "dialect" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def llm_sql_generator(llm, fallback: SqlGenerator = keyword_sql_generator) -> SqlGenerator:
    """#135: a schema-grounded LLM NL2SQL generator, drop-in behind the SqlGenerator
    seam (mirrors C3's `llm_entry_parser`). The model only PROPOSES SQL
    (`llm.generate_sql(question, schema)`); this wrapper is deterministic about safety —
    it strips fences and validates the output against the visible schema with the SAME
    read-only guard the store enforces. ANY failure — refusal, empty, bad JSON/SQL,
    a write, an invisible table, an API error - falls back to `keyword_sql_generator`
    ONLY when the store handed over the FULL schema, so a demo store never errors on a
    bad generation; it degrades to the naive query.
    (FederatedSqlStore.retrieve re-validates too: defense in depth, not a substitute.)

    #222 Fix 1 - in RETRIEVAL MODE (the store narrowed the schema to a retrieved subset)
    that fallback is FORBIDDEN, and this is the whole point:

      the LLM naming a table that is not in the allowlist is the HIGHEST-QUALITY SIGNAL
      AVAILABLE that RETRIEVAL MISSED IT.

    Before #221 the model always saw the full schema, so validate_sql inside this wrapper
    effectively never fired and the fallback never ran in production. Narrowing the schema
    turned that dead branch live and pointed it at the worst possible target: a competent
    model asked "who are our top 5 customers by total due" writes a Customer x
    SalesOrderHeader join, the narrowed allowlist rejects it because retrieval missed both
    tables, the exception is swallowed, and `keyword_sql_generator` emits
    `SELECT SUM(SalesOrderDetailID) AS total_SalesOrderDetailID FROM SalesOrderDetail`
    over the top-ranked NOISE table - summing a primary key, calling it a total - which
    the narrowed allowlist then BLESSES, because the noise table IS in it. Confident
    citation, fabricated answer: exactly the #211 class this codebase was fixed for.
    So in retrieval mode the failure is re-raised as CannotAnswerFromSchema and the store
    gets to widen once, then decline honestly."""
    def generate(question: str, schema: list, dialect: str = "") -> str:
        tables = [t["table"] for t in schema]
        _GENERATION_DEGRADED.set(None)      # per generation: never leak into the next one
        try:
            if dialect and _accepts_dialect(llm.generate_sql):
                raw = llm.generate_sql(question, schema, dialect=dialect)
            else:
                raw = llm.generate_sql(question, schema)
            sql = _strip_sql_fence(raw)
            if not sql:
                raise ValueError("empty generation")
            # #211: the model saying "this schema has nothing to do with the question" is a
            # RESULT, not a failure — and it must NOT fall through to the keyword generator,
            # because a broad SELECT over "the most relevant table" is the same fabrication
            # wearing different clothes. Raise past the fallback so the store DECLINES.
            if sql.strip().upper().startswith(CANNOT_ANSWER):
                raise CannotAnswerFromSchema(
                    "this source holds no data of the kind the question asks about")
            validate_sql(sql, tables)      # read-only, single-stmt, visible-schema only
            return sql
        except CannotAnswerFromSchema:
            raise
        except Exception as exc:
            if in_retrieval_mode():
                raise CannotAnswerFromSchema(
                    "generation failed against the RETRIEVED subset of the schema "
                    f"({exc}); a guessed table is not an answer") from exc
            # #482: the fallback still runs - that is a separate, deliberate decision - but
            # the reason stops being thrown away here.
            _GENERATION_DEGRADED.set(f"{type(exc).__name__}: {exc}"[:300])
            # #714: the fallback needs the dialect too - a degraded generation that
            # emits LIMIT against T-SQL turns "recoverable" into a hard parse error.
            if dialect and _accepts_dialect(fallback):
                return fallback(question, schema, dialect=dialect)
            return fallback(question, schema)

    return generate


class FederatedSqlStore(StorePort):
    def __init__(self, store_id: str, business_unit: str, title: str, description: str,
                 engine: SqlEnginePort, *, sql_generator: SqlGenerator | None = None,
                 authorizer: Authorizer | None = None, audit: AuditSink | None = None,
                 topics: list | None = None,
                 retrieval_k: int = 8, widen_k: int = 30,
                 embedder=None, schema_descriptions: "dict | None" = None,
                 value_llm=None) -> None:
        self._store_id = store_id
        self._bu = business_unit
        self._title = title
        self._description = description
        self._engine = engine
        self._gen = sql_generator or keyword_sql_generator
        self._authorizer = authorizer
        self._audit = audit
        self._topics = topics or []
        self.audit_trail: list = []            # always kept, even without a sink (§8)
        # #221: retrieval - the generator sees a RANKED SUBSET of the schema, not the
        # whole warehouse, and the SAME subset becomes validate_sql's table allowlist.
        self._retrieval_k = retrieval_k
        self._widen_k = widen_k
        self._embedder = embedder              # None -> lazy default (HashingEmbedding)
        # #486: AUTHORED text about what the tables and columns MEAN, from store config.
        # Config, never introspection: a description read off the rows would be customer
        # data entering a prompt (LAW 1). See `describe_schema`.
        self._schema_descriptions = schema_descriptions or {}
        # #462: the edition's chat model, used ONLY by the literal-resolution LLM rung -
        # and only when its `in_tenant` flag says the prompt never leaves the tenant.
        # The gate lives in `dictionary._llm_pick`, not here, so no caller can forget it.
        self._value_llm = value_llm
        self._schema_index = None
        self._index_schema = None              # identity key: rebuilt when schema changes

    def _narrows(self, schema: list) -> bool:
        """RETRIEVAL MODE: does this store hand the generator a SUBSET? At small N it does
        not - the full schema passes through and behavior is byte-identical to pre-#221
        (the five demo stores). Only in the narrowed case is a generation failure evidence
        of a retrieval miss, so only there is the keyword fallback forbidden (#222 Fix 1)."""
        return len(schema) > self._retrieval_k

    def _subset_for(self, question: str, schema: list, k: int, *,
                    floor_frac: float = 0.6, margin_frac: float = 0.15,
                    max_tables: int = 16) -> list:
        """Ranked retrieved subset, or the schema itself at small N (identity - the
        five demo stores behave exactly as before #221).

        #222 Fix 5: the retrieval knobs are PLUMBED, not just `k`. `k` was never the
        binding constraint - SchemaIndex's relative `floor_frac` cut first, so raising
        k from 8 to 30 provably returned the identical set and the "widen" was a no-op."""
        if not self._narrows(schema):
            return schema
        if self._schema_index is None or self._index_schema is not schema:
            from dbsearch.router.schema_index import SchemaIndex
            # A REQUIREMENT, not a default (#225) - the server passes its 128-dim edition
            # embedder in, which silently bypassed the old `if self._embedder is None` default.
            self._embedder = schema_index_embedder(self._embedder)
            try:
                fk = self._engine.fk_edges()
            except Exception:
                fk = []
            self._schema_index = SchemaIndex(schema, self._embedder, fk_edges=fk)
            self._index_schema = schema
        return self._schema_index.retrieve(
            question, k=k, floor_frac=floor_frac, margin_frac=margin_frac,
            max_tables=max_tables)

    def _generate(self, question: str, subset: list, narrowed: bool) -> str:
        """The ONE place SQL is generated. `narrowed` is the store's own declaration that
        the generator is seeing a retrieved subset - a generation failure there must reach
        the widen/decline machinery, never a keyword guess over the top-ranked noise
        table (#222 Fix 1).

        #222 Fix 6 - the same principle, taken to its end. router_api explicitly supports a
        deployment with NO chat model ("absent the capability, SQL stores keep the
        deterministic default"), and in THAT config `keyword_sql_generator` is the whole
        NL2SQL layer: there is no model to say CANNOT_ANSWER, so the retrieval floor is the
        only guard there is. It is not enough. Measured on a keyword-only store:

            "how many HR employees are on parental leave"
                -> SELECT COUNT(*) AS count FROM ProductModel
            "which products have the most support tickets"
                -> SELECT * FROM Product LIMIT 5

        A confident, cited number with nothing to do with the question - #211, live. The
        keyword generator is honest about being naive, but "SELECT ... FROM
        schema[0]['table']" over a RETRIEVED subset is a guess dressed as an answer, and
        the narrowed allowlist then blesses it. So: in retrieval mode it is FORBIDDEN
        OUTRIGHT. A store that narrowed and has no LLM must DECLINE.

        On the identity path it stays fully available - the five demo stores rely on it and
        it is exactly what they advertise (a naive query over a schema the caller can see
        in full, never a retrieved guess)."""
        if narrowed and self._gen is keyword_sql_generator:
            raise CannotAnswerFromSchema(
                "this store narrowed its schema to a retrieved subset and has no NL2SQL "
                "model; the deterministic keyword generator would be guessing a table")
        with retrieval_mode(narrowed):
            # #714: the generator must know the TARGET dialect — generic SQL dies on
            # T-SQL (`LIMIT`). Bound by PARAMETER NAME (the #673 rail pattern): a
            # generator that never declared `dialect` keeps its exact old call shape.
            dialect = getattr(self._engine, "dialect", "")
            if dialect and _accepts_dialect(self._gen):
                return self._gen(question, subset, dialect=dialect)
            return self._gen(question, subset)

    def profile(self) -> StoreProfile:
        schema = self._engine.schema()
        return StoreProfile(store_id=self._store_id, title=self._title,
                            description=self._description, kind=FEDERATED_SQL,
                            capabilities={ANALYTICAL, EXACT}, business_unit=self._bu,
                            topics=list(self._topics), schema=schema,
                            freshness="live", proof_kind="sql",
                            warnings=empty_schema_warnings(self._engine, schema))

    def authorize(self, user_oid: str) -> AccessContext:
        if self._authorizer is not None:
            return self._authorizer(user_oid)   # the E5 broker seam (gate #2)
        return AccessContext(user_oid=user_oid, principals=[])

    def described_schema(self) -> list:
        """This store's schema with its authored descriptions attached (#486)."""
        return describe_schema(self._engine.schema(), self._schema_descriptions)

    def _read_schema(self, described: bool) -> list:
        """The schema, with #727's emptiness contract: zero tables is a SOURCE fault.

        One `refresh_schema()` retry first - never a loop - so a transient empty (an
        expired STS session since repaired, a GRANT fixed after compose) recovers without
        a recompose; the engines stopped caching empty reads for exactly this reason.
        Still empty after the retry -> SchemaUnavailable, which the executor renders as an
        ERROR with these instructions, never as "holds no data of this kind"."""
        schema = self.described_schema() if described else self._engine.schema()
        if not schema:
            self._engine.refresh_schema()
            self._schema_index = None
            schema = self.described_schema() if described else self._engine.schema()
        if not schema:
            raise SchemaUnavailable(
                "this source's schema could not be read - introspection returned 0 tables. "
                "Check the delegated credential's privileges on the source, and that any "
                "`tables:` allowlist entries are schema-qualified (a bare name only matches "
                "the default schema).")
        return schema

    def _resolve_sql(self, question: str, described: bool = True) -> tuple:
        """Generate this store's SQL for `question`, with the #221/#222 widen-once-then-decline
        machinery. Returns (sql, subset, schema); raises CannotAnswerFromSchema on an honest
        decline. Factored out of retrieve() so retrieve_bound (#219) shares the EXACT same
        generation, guard and decline path - the semi-join only constrains the RESULT, it must
        never weaken any of that.

        `described=False` is the #495 reprompt: the bare names-and-types schema the
        generator saw before #486, used exactly once when the described generation
        produced an unrepairable filter miss."""
        # #486: the generator sees the AUTHORED descriptions alongside names and types.
        # Retrieval and validation are unaffected - they read table/column names, which are
        # unchanged - but the model is no longer guessing what a column MEANS.
        schema = self._read_schema(described)
        narrowed = self._narrows(schema)
        subset = self._subset_for(question, schema, self._retrieval_k)
        sql = None
        if subset:
            try:
                sql = self._generate(question, subset, narrowed)
            except CannotAnswerFromSchema:
                sql = None
        if sql is None:
            # widen ONCE: re-introspect (cheap schema-drift answer), then a GENUINELY wider
            # retrieval, then done - never a loop. A second miss (empty retrieval or a
            # second decline) is honest.
            #
            # #222 Fix 5: widening means relaxing the FLOORS, not only k. floor_frac 0.6 ->
            # 0.0 admits the whole ranked top-`widen_k` including zero-signal tables, which
            # is the point: the miss being recovered from is a table whose lexical cosine
            # was zero. margin_frac 0.15 -> 0.0 relaxes the decline gate to "the best table
            # must merely beat the schema's own baseline", while `min_cosine` still holds
            # the line against a schema with no signal at all. max_tables lifts 16 -> 24 so
            # the wider ranking is not immediately re-cut by the cap - still bounded, and
            # still the exact allowlist validate_sql enforces.
            self._engine.refresh_schema()
            # the widen re-read was always the DESCRIBED schema, even on the #495 bare
            # reprompt - preserved; only the emptiness handling (#727) is new.
            schema = self._read_schema(True)
            narrowed = self._narrows(schema)
            self._schema_index = None
            subset = self._subset_for(question, schema, self._widen_k, floor_frac=0.0,
                                      margin_frac=0.0, max_tables=24)
            if not subset:
                raise CannotAnswerFromSchema(
                    "no table in this source matches the question")
            # a second CannotAnswer propagates - including a second retrieval-mode failure
            sql = self._generate(question, subset, narrowed)
        # #481: the model hedges against the declared type and wraps numeric columns in
        # string functions. The server KNOWS the type, so this is settled here rather than
        # argued with the model - and only where the argument is provably a numeric column.
        normalized = strip_string_ops_on_numeric(sql, subset)
        _QUERY_NORMALIZED.set(sql if normalized != sql else None)
        return normalized, subset, schema

    def _execute_and_build(self, access: AccessContext, sql: str, subset: list, schema: list,
                           top_k: int, *, bind: "dict | None" = None,
                           resolved: "dict | None" = None,
                           reprompted: "str | None" = None,
                           carry_source: bool = False) -> list:
        """Validate -> row-policy wrap -> audit -> execute -> Evidence. Shared by retrieve() and
        retrieve_bound() so the semi-join cannot bypass the read-only guard, the row policy, or
        the audit sink. `bind` (#219) rides along in provenance for observability and the
        alignment disclosure; it is metadata (column name + counts), never a value (LAW 8)."""
        validate_sql(sql, [t["table"] for t in subset])
        if access.row_policy:
            # E5 fallback path: proven predicate over the result set. (Delegated-credential
            # engines enforce source-side instead — preferred, ADR 0006.)
            sql = f"SELECT * FROM ({sql}) WHERE {access.row_policy}"
        degraded = generation_degraded()
        record = {"user": access.user_oid, "store": self._store_id, "sql": sql,
                  "row_policy": access.row_policy or "",
                  # LAW 8: record THAT delegation happened, never the credential
                  "delegated": access.delegated_credential is not None}
        if bind is not None:
            record["bind"] = bind
        if reprompted:
            record["reprompted"] = reprompted        # #495: second generation, bare schema
        if degraded:
            record["degraded"] = degraded            # #482: why this is not the model's SQL
        normalized = query_normalized()
        if normalized:
            record["normalized"] = normalized        # #481: the SQL as the model wrote it
        self.audit_trail.append(record)
        if self._audit is not None:
            self._audit(record)
        cols, rows = self._engine.execute(sql, credential=access.delegated_credential,
                                          principal=access.user_oid)
        if empty_aggregate(cols, rows):
            missed = self._unmatched_predicate(access, sql)
            if missed is not None:
                # #476: the aggregate is empty because the FILTER matched nothing, not
                # because the quantity is zero. Passing the 0 through is a falsehood the
                # reader has no way to detect, so the store returns no evidence and the
                # executor records it EMPTY - "I looked and found no matching rows", which
                # is exactly what happened.
                #
                # #479: but first, try to make the question ANSWERABLE. The generator never
                # saw a value (LAW 1), so it wrote the literal the way the user said it;
                # mapping that wording onto the stored encoding is this side's job. One
                # repair, disclosed. Nothing below this line can fire on a query that
                # worked, because a working query never reaches it.
                if resolved is None:
                    repaired = self._repair_literal(access, sql, missed, subset, schema,
                                                    top_k, reprompted=reprompted)
                    if repaired is not None:
                        return repaired
                    # #495: nothing on this side could save the query - the miss is not a
                    # literal encoding but (typically) a predicate the model invented.
                    # Signal the caller so retrieve() can re-generate ONCE against the
                    # bare schema; every other entry point converts this back to the
                    # decline it always was.
                    raise _UnrepairableFilterMiss(missed)
                return []
        elif not rows and resolved is None and carry_source and _repair_empty_carry():
            # #504: the SAME miss, one shape further out - but ONLY for a CARRY SOURCE.
            #
            # #476 reasoned that "zero rows is already EMPTY and needs no help", and for
            # an answer the user reads that is right: an empty list is self-evidently
            # empty and they judge it directly. It is false for a semi-join / rescue
            # carry-source half, whose rows are not an answer at all but the KEYS the next
            # half binds to - there zero rows is a dead mechanism, disclosed live as "the
            # filter half carried no key values" (F-005/D-003, findings s20). F-005 is
            # D-001's twin in the wrong vocabulary, which the plain path WOULD have
            # repaired had the question been phrased as an aggregate.
            #
            # The first cut of this fix triggered on ANY zero-row result, and review caught
            # what that costs: asking for a product that does not exist returned the
            # NEAREST STORED NEIGHBOUR's rows ('Widget Pro' -> 'Widget Plus'), with the
            # substituted column absent from the SELECT list, so neither the prose nor the
            # screen carried any trace - `provenance.resolved` is not rendered anywhere.
            # That trades an honest decline for a possibly-wrong answer, which is the exact
            # failure class the verify-everything architecture exists to remove. The
            # aggregate precedent does not carry: there the alternative was a printed 0,
            # a falsehood the reader could not detect. Here the alternative was correct.
            #
            # So the trigger is the miss AND the caller's role. On a carry source the rows
            # are mechanics the bind consumes (they are excluded from the synthesizer's
            # prompt by #474's mechanics rule), and a wrong key set cannot masquerade as an
            # answer - the aligned-trust gate rejects a rescue whose halves do not
            # mechanically bind. Deliberately NOT raising _UnrepairableFilterMiss: #495's
            # bare-schema re-generation stays scoped to the empty-aggregate case it was
            # measured on.
            missed = self._unmatched_predicate(access, sql)
            if missed is not None:
                repaired = self._repair_literal(access, sql, missed, subset, schema,
                                                top_k, reprompted=reprompted)
                if repaired is not None:
                    return repaired
        table = (_TABLE_REF.search(sql) or [None]) and (
            _TABLE_REF.search(sql).group(1) if _TABLE_REF.search(sql) else
            (schema[0]["table"] if schema else ""))
        # The query's TRUE row count travels with the evidence (#206). Without it the answer
        # is built from `top_k` rows and nobody downstream can tell 5-of-5 from 5-of-295 —
        # so the synthesizer writes "here is the total revenue for EACH product SKU" over a
        # 2% sample and the reader has no way to know. A row cap gets the same treatment the
        # cost cap already gets: disclosed, never silent.
        total = len(rows)
        # #729(a): how to READ the values in `content`, for the columns this query actually
        # returned. Scoped to `cols` so an alias the schema knows nothing about ("SUM(x) AS
        # total") simply has no entry and renders raw, and built from `subset` - the tables in
        # THIS query - rather than the whole store, because a narrower scope has fewer of the
        # cross-table name collisions `value_classes` refuses to guess at.
        #
        # It rides in provenance, not in `content`: `content` is what the MODEL is shown and
        # what the Sources rail must stay checkable against, so the raw value the database
        # returned is never rewritten on its way anywhere. Only the render decides.
        classes = value_classes(subset)
        col_classes = {str(c): classes[str(c).lower()]
                       for c in cols if str(c).lower() in classes}
        out = []
        for i, row in enumerate(rows[:top_k]):
            content = ", ".join(f"{c}={v}" for c, v in zip(cols, row))
            prov = {"sql": sql, "table": table, "row_ids": [i], "total_rows": total}
            if col_classes:
                prov["column_types"] = col_classes
            if bind is not None:
                prov["bind"] = bind
            if resolved is not None:
                prov["resolved"] = resolved      # #479: the substitution, disclosed (LAW 8)
            if reprompted:
                prov["reprompted"] = reprompted  # #495: the bare-schema retry, disclosed
            if degraded:
                prov["degraded"] = degraded      # #482: this is not the model's SQL
            out.append(Evidence(store_id=self._store_id, business_unit=self._bu,
                                kind=ROW, content=content, provenance=prov, score=None))
        return out

    def _unmatched_predicate(self, access: AccessContext, sql: str) -> "str | None":
        """The first WHERE term of `sql` that matches no rows on its own, or None (#476).

        Returns the predicate rather than a boolean because #479 needs to know WHICH one
        missed in order to repair it. Every probe that fails to execute is treated as "no
        opinion", never as proof: a dialect this parser guessed wrong about must leave the
        answer exactly as it was."""
        for predicate, probe in predicate_probes(
                sql, dialect=getattr(self._engine, "dialect", "")):
            try:
                _, hit = self._engine.execute(probe, credential=access.delegated_credential,
                                              principal=access.user_oid)
            except Exception:      # noqa: BLE001 - an unprobeable dialect proves nothing
                continue
            if not hit:
                return predicate
        return None

    def _repair_literal(self, access: AccessContext, sql: str, predicate: str,
                        subset: list, schema: list, top_k: int,
                        reprompted: "str | None" = None) -> "list | None":
        """Re-run `sql` with the missed predicate's literal mapped onto a stored value.

        The generator wrote the literal in the USER's words because by LAW 1 it never saw a
        value. This reads the column's own values through the CALLER's credential - so an
        unreadable value is unresolvable, LAW 2 by construction, not by a filter someone
        has to remember to apply - and rewrites that one predicate.

        None whenever anything is uncertain: an unparseable predicate, an unknown table, a
        column with no useful dictionary, no confident match, or a repaired query that is
        STILL empty. Uncertainty here must stay a decline, never a guess."""
        parsed = predicate_literal(predicate)
        if parsed is None:
            return None
        reference, written, shape = parsed
        qualifier, _, column = reference.rpartition(".")
        aliases = table_aliases(sql)
        table = aliases.get(qualifier) if qualifier else next(iter(aliases.values()), None)
        if table is None or (qualifier and len(aliases) > 1 and qualifier not in aliases):
            return None
        values = column_values(self._engine, _norm_table(table), column, access)
        if not values:
            return None
        match = resolve_literal(written, values, self._embedder, llm=self._value_llm)
        if match is None or match == written:
            return None
        # A "like" repair keeps the contains semantics: the resolved TOKEN goes back
        # inside the wildcards, so `LOWER(genres) LIKE '%science fiction%'` becomes
        # `genres LIKE '%Sci-Fi%'` - still "contains", now against the stored spelling.
        replacement = (f"{reference} LIKE {bind_literal(f'%{match}%')}" if shape == "like"
                       else f"{reference} = {bind_literal(match)}")
        repaired = sql.replace(predicate, replacement, 1)
        resolved = {"column": column, "written": written, "resolved_to": match}
        try:
            return self._execute_and_build(access, repaired, subset, schema, top_k,
                                           resolved=resolved,
                                           reprompted=reprompted) or None
        except Exception:          # noqa: BLE001 - a failed repair is not an answer
            return None

    def retrieve(self, access: AccessContext, question: str, top_k: int = 5) -> list:
        """#207: a breakdown is truncated to top_k before the user ever sees it, so the rows
        SHOWN must be the ones that matter. `rank_grouped_default` re-orders a key-ordered
        breakdown by its measure and leaves everything else alone — including any ordering the
        question itself asked for. Applied here, at the seam, so every store gets it rather than
        only the semi-join carry source (#232)."""
        sql, subset, schema = self._resolve_sql(question)
        try:
            return self._execute_and_build(access, rank_grouped_default(sql), subset, schema,
                                           top_k)
        except _UnrepairableFilterMiss:
            # #495: the described generation produced a filter that provably matched
            # nothing and could not be repaired - on the real pack, a predicate the model
            # fabricated under the longer prompt. Re-generate ONCE against the bare
            # schema (the exact prompt that wrote correct SQL for E-001 pre-#486) and run
            # it through the same guard + repair ladder, disclosed as `reprompted`. A
            # CannotAnswerFromSchema decline never lands here - the descriptions are WHY
            # the hr/home-runs trap declines, and that decline must stand.
            if not self._schema_descriptions:
                return []
            try:
                sql, subset, schema = self._resolve_sql(question, described=False)
                rows = self._execute_and_build(access, rank_grouped_default(sql), subset,
                                               schema, top_k, reprompted="bare-schema")
            except (CannotAnswerFromSchema, _UnrepairableFilterMiss):
                return []
            # Measured live (run 462a): stripped of its descriptions, the bare prompt can
            # also resurrect exactly the fabrication the descriptions prevent - the
            # hr/home-runs trap answered AVG(HR) again whenever the described generation
            # happened to write an empty-matching filter instead of CANNOT_ANSWER. So the
            # bare result is trusted ONLY when its own literal was repaired: proof it is
            # the value-encoding case this path exists for, not a second opinion on a
            # question the described model refused to answer.
            if rows and all((r.provenance or {}).get("resolved") for r in rows):
                return rows
            return []

    def retrieve_ranked(self, access: AccessContext, question: str, top_k: int = 5) -> list:
        """#232: like retrieve(), but a GROUP BY breakdown is re-ordered by its MEASURE before the
        top_k cut, so the rows SHOWN are the true top rows - not an alphabetical slice. Used only
        for a semi-join CARRY-SOURCE half, so half A's carried keys are its top-N by the measure
        (the most-ticketed products), which is what makes the cross-store comparison meaningful.
        `rank_by_measure` is a fail-safe no-op on anything that isn't a plain grouped aggregate, so
        this can never produce a query retrieve() would not have."""
        sql, subset, schema = self._resolve_sql(question)
        try:
            # #504: carry_source=True - an empty key set here is a dead mechanism, not an
            # answer, so the literal repair is allowed to fire on zero rows (and ONLY here).
            return self._execute_and_build(access, rank_by_measure(sql), subset, schema,
                                           top_k, carry_source=True)
        except _UnrepairableFilterMiss:
            return []                  # #495: the carry-source half declines as before

    def retrieve_bound(self, access: AccessContext, question: str, values: list,
                       top_k: int = 5, column: "str | None" = None) -> list:
        """#219: run this store's query CONSTRAINED to the join-key values half A produced - a
        federated semi-join. This half generates its SQL as usual, then wraps its OWN group-by
        column: `SELECT * FROM (<sql>) AS _semi WHERE <col> IN (<values>)`. It never sees half
        A's column NAME - #215 guarantees both halves carry the same semantic grain, so half A's
        values are keys in this half's own key column too.

        #474 gate 3: a SCALAR aggregate has no group-by column to wrap, so when the caller
        names the carried COLUMN, the IN list is pushed into the query's own WHERE instead -
        directly when the SQL already touches a table holding that column, or one hop away
        through a shared key (`order_id IN (SELECT order_id FROM orders WHERE customer_id
        IN ...)`). Deterministic and schema-derived; anything it cannot prove falls open.

        Injection is structurally impossible: values pass `sanitize_bind_values` (strict allowlist)
        and `bind_literal` (quote-safe) before touching SQL, and nothing enters an LLM prompt.

        Fails OPEN, disclosed: if the wrapped query ERRORS (e.g. this half aliased its key so the
        outer column name misses), or there is no group-by to bind on, or no value survived the
        allowlist, it retries UNBOUND and marks the evidence `aligned: False`. A lined-up answer
        is best, an unaligned one is still honest, an error is neither."""
        sql, subset, schema = self._resolve_sql(question)
        kept, dropped = sanitize_bind_values(values)
        col = groupby_column(sql)
        if col and kept:
            wrapped = semijoin_wrap(sql, col, kept)
            try:
                return self._execute_and_build(
                    access, wrapped, subset, schema, top_k,
                    bind={"column": col, "values_n": len(kept), "dropped": dropped,
                          "aligned": True})
            except Exception:  # noqa: BLE001 - fall open to the unbound query (+ disclose)
                pass
        injected = (self._inject_column_bind(sql, column, kept)
                    if not col and column and kept else None)
        if injected is not None:
            try:
                return self._execute_and_build(
                    access, injected, subset, schema, top_k,
                    bind={"column": column, "values_n": len(kept), "dropped": dropped,
                          "aligned": True})
            except Exception:  # noqa: BLE001 - fall open to the unbound query (+ disclose)
                pass
        reason = ("no key value survived sanitization" if not kept
                  else "the key-bound query failed" if (col or injected)
                  else "no bind path to the carried column" if column
                  else "this half does not group on a key")
        try:
            return self._execute_and_build(
                access, sql, subset, schema, top_k,
                bind={"aligned": False, "reason": reason, "dropped": dropped})
        except _UnrepairableFilterMiss:
            return []                  # #495: the bound half declines as before

    def _inject_column_bind(self, sql: str, column: str, kept: list) -> "str | None":
        """`sql` constrained to `column IN (kept)` (#474 gate 3), or None when no safe
        bind path exists.

        Two deterministic, schema-derived paths - both mechanical, no LLM anywhere:

        - DIRECT: a table already in the query holds `column` - qualify with that
          table's alias and AND it into the WHERE.
        - ONE HOP: some other table T in this store holds `column` and shares a join
          column k with a table in the query - constrain through it:
          `k IN (SELECT k FROM T WHERE column IN (...))`. The shared column is chosen
          deterministically (first by name) so one question always binds one way (#254).

        Anything else returns None and the caller falls open to the disclosed unbound
        query. A bind this cannot PROVE from the schema is a wrong total waiting to
        happen - fail open and disclose, never guess."""
        if not re.fullmatch(r"[A-Za-z_]\w*", column or ""):
            return None
        # Any literal predicate the GENERATOR wrote on the carry column is a guess by
        # construction - it never saw a value (LAW 1) - and measured live it invents
        # placeholders (`LOWER(customer_id) IN ('customer_1', 'customer_2')`) that zero
        # the result even after the real keys are ANDed in. Neutralize them to 1=1
        # before injecting the authoritative list; predicates on every OTHER column are
        # real filters the question asked for, and are never touched.
        placeholder = re.compile(
            rf"(?:LOWER\s*\(\s*)?(?:[A-Za-z_]\w*\s*\.\s*)?{re.escape(column)}\s*\)?\s*"
            rf"(?:(?:NOT\s+)?IN\s*\([^)]*\)|=\s*'(?:[^']|'')*')", re.I)
        sql = placeholder.sub("1=1", sql)
        in_list = ", ".join(bind_literal(v) for v in kept)
        columns_of = {t["table"]: {c["name"].casefold(): c["name"]
                                   for c in t.get("columns", [])}
                      for t in self._engine.schema()}
        target = column.casefold()
        aliases = table_aliases(sql)
        # DIRECT: a table in the query holds the carried column.
        for ref, table in sorted(aliases.items()):
            actual = columns_of.get(table, {}).get(target)
            if actual:
                return inject_predicate(sql, f"{ref}.{actual} IN ({in_list})")
        # ONE HOP: another table in this store holds it and shares a key with the query.
        for hop_table, hop_cols in sorted(columns_of.items()):
            if hop_table in aliases.values() or target not in hop_cols:
                continue
            for ref, table in sorted(aliases.items()):
                shared = sorted(set(columns_of.get(table, {})) & set(hop_cols) - {target})
                if shared:
                    k = shared[0]
                    inner = (f"SELECT {hop_cols[k] if k in hop_cols else k} FROM {hop_table} "
                             f"WHERE {hop_cols[target]} IN ({in_list})")
                    return inject_predicate(
                        sql, f"{ref}.{columns_of[table][k]} IN ({inner})")
        return None

    def rerun_sql(self, access: AccessContext, sql: str, cap: int = 50):
        """#165 proof re-run: execute a SERVER-ISSUED statement verbatim under the
        CURRENT caller's guards. Same chain as retrieve() minus generation:
        validate → wrap caller's row policy → delegated-credential execute.
        Wrapping again over an already-wrapped statement intersects policies — safe."""
        schema = self._engine.schema()
        validate_sql(sql, [t["table"] for t in schema])
        if access.row_policy:
            sql = f"SELECT * FROM ({sql}) WHERE {access.row_policy}"
        record = {"user": access.user_oid, "store": self._store_id, "sql": sql,
                  "row_policy": access.row_policy or "",
                  "delegated": access.delegated_credential is not None, "rerun": True}
        self.audit_trail.append(record)
        if self._audit is not None:
            self._audit(record)
        cols, rows = self._engine.execute(sql, credential=access.delegated_credential,
                                          principal=access.user_oid)
        return cols, rows[:cap], len(rows)


class CsvSqlProvider(StoreProviderPort):
    """`kind: csv` — loose structured files as a federated SQL store (in-tenant embedded
    engine, ADR 0007). config: `tables:` inline {name: {columns, rows}} or `files:` csv paths."""

    kind = "csv"
    modes = ("pushdown",)       # ADR 0008: federated, queried in place, never copied

    def __init__(self, *, sql_generator: SqlGenerator | None = None,
                 authorizer: Authorizer | None = None, broker=None,
                 embedder=None, value_llm=None) -> None:
        self._gen = sql_generator
        self._authorizer = authorizer
        self._broker = broker
        # #222 Fix 2: the EDITION's embedder (same wiring LocalIndexProvider already has,
        # #143). Without it every FederatedSqlStore silently fell back to the lazy 128-dim
        # HashingEmbedding, so the #221 schema index NEVER saw the real semantic embedder
        # in ANY deployment - table retrieval ran on a lexical toy while the document rail
        # ran on the real model. None still means "lazy HashingEmbedding" (tests rely on it).
        self._embedder = embedder
        # #462: the edition's chat model for the literal-resolution rung; the
        # in_tenant gate lives in dictionary._llm_pick, absent-means-no (LAW 1).
        self._value_llm = value_llm

    def _make(self, config: dict) -> FederatedSqlStore:
        if config.get("tables"):
            engine = SqliteEngine.from_tables(config["tables"])
        elif config.get("files"):
            engine = SqliteEngine.from_csv_files(config["files"])
        else:
            raise ValueError("csv store config needs `tables` or `files`")
        authorizer = self._authorizer
        if authorizer is None and self._broker is not None:
            sid = config["id"]
            authorizer = lambda u, _s=sid: self._broker.access_for(u, _s)  # noqa: E731
        return FederatedSqlStore(
            store_id=config["id"], business_unit=config.get("business_unit", ""),
            title=config.get("title", config["id"]), 
            description=config.get("description", ""), engine=engine,
            sql_generator=self._gen, authorizer=authorizer,
            topics=config.get("topics") or [], embedder=self._embedder,
            schema_descriptions=config.get("schema_descriptions"),
            value_llm=self._value_llm)

    def probe(self, config: dict) -> StoreProfile:
        return self._make(config).profile()

    def build(self, config: dict) -> FederatedSqlStore:
        return self._make(config)
