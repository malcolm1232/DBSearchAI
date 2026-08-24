"""`kind: azure_sql` — Azure SQL Database as a federated pushdown store (#107, ADR 0007).

AzureSqlEngine is a SqlEnginePort sibling of SqliteEngine: the SQL executes INSIDE
Azure SQL and only the result set comes back. Everything that matters — the read-only
visible-schema guard, the audit trail, the NL2SQL seam, row-policy fallback — stays in
FederatedSqlStore; this module only introspects INFORMATION_SCHEMA and executes.

LAW 7: pymssql (TDS) is imported lazily inside the connection factory — the optional
dep (`pip install '.[azure-sql]'`) is only needed when a manifest actually composes an
azure_sql store. Secrets arrive via ${ENV} in stores.yml, resolved SERVER-side (LAW 1).

Delegated auth (Phase H #131, ADR 0006): a delegated credential is an AAD access token
(audience https://database.windows.net/) from the E5 broker. pymssql cannot present
one, so the delegated path opens its OWN connection via pyodbc/msodbcsql, passing the
token as SQL_COPT_SS_ACCESS_TOKEN — Azure SQL then enforces THAT user's permissions
source-side. One connection per credential (mirrors DatabricksEngine); no pyodbc or no
token -> the delegated ask FAILS CLOSED (LAW 2), never the service identity.

Serverless free-tier auto-pauses: an idle connection is severed and the first statement
after resume fails. The engine reconnects ONCE and retries — safe because the guard
upstream only ever lets reads through.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from dbsearch.router.provider import StoreProviderPort
from dbsearch.router.store import StoreProfile
from dbsearch.router.structured import (
    Authorizer, FederatedSqlStore, SqlEnginePort, SqlGenerator,
)

# SQL Server ODBC pre-connect attribute: an AAD access token in place of user/password
_SQL_COPT_SS_ACCESS_TOKEN = 1256


def _aad_token_struct(token: str) -> bytes:
    """Pack an AAD access token the way the ODBC driver wants it:
    <uint32 little-endian byte length><token as UTF-16-LE>."""
    body = token.encode("utf-16-le")
    return len(body).to_bytes(4, "little") + body


# TABLE_SCHEMA is part of a table's identity, not decoration: a real database keeps its tables
# in named schemas (AdventureWorksLT -> SalesLT), and the generators drop schema[i]["table"]
# straight into `FROM ...`. Discovering a bare `Product` therefore produces SQL that CANNOT
# execute — `Invalid object name 'Product'` — for everything outside the default schema (#203).
_SCHEMA_SQL = (
    "SELECT c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE "
    "FROM INFORMATION_SCHEMA.COLUMNS c "
    "JOIN INFORMATION_SCHEMA.TABLES t "
    "ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
    "WHERE t.TABLE_TYPE = 'BASE TABLE' "
    "ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION"
)

# sys.foreign_keys is the T-SQL catalog view for declared FKs; join to sys.tables/
# sys.schemas on both the referencing (parent) and referenced side to get schema-
# qualified names matching schema()'s own `TABLE_SCHEMA.TABLE_NAME` spelling exactly.
_FK_SQL = (
    "SELECT s1.name + '.' + t1.name, s2.name + '.' + t2.name "
    "FROM sys.foreign_keys fk "
    "JOIN sys.tables t1 ON t1.object_id = fk.parent_object_id "
    "JOIN sys.schemas s1 ON s1.schema_id = t1.schema_id "
    "JOIN sys.tables t2 ON t2.object_id = fk.referenced_object_id "
    "JOIN sys.schemas s2 ON s2.schema_id = t2.schema_id"
)


class AzureDatabaseUnavailable(RuntimeError):
    """#811: the 40613 wake-retry ran out of budget without ever connecting.

    Its own type, not a bare RuntimeError, so the health and executor layers can render the
    message as user-facing instructions the way #727's SchemaUnavailable is rendered, rather
    than surfacing the driver's raw `(40613, b'...')` tuple - which is what a user saw after
    waiting two minutes for a database whose name was simply wrong.
    """


class AzureSqlEngine(SqlEnginePort):
    # #714: T-SQL is the one dialect in the fleet with no LIMIT clause. The generator
    # emitted generic `... LIMIT 5` and Synapse answered 42000 "Incorrect syntax near
    # 'LIMIT'", surfaced to the user as "the source did not respond successfully". The
    # instruction rides in the dialect string itself so every adapter inherits it.
    dialect = "T-SQL (Microsoft SQL Server / Azure Synapse): use SELECT TOP n, never LIMIT"

    def __init__(self, connect: Callable[[], object], tables: Optional[list] = None,
                 resume_wait: float = 5.0, resume_timeout: float = 120.0,
                 user_connect: Optional[Callable[[str], object]] = None) -> None:
        self._connect = connect
        self._user_connect = user_connect
        self._user_conns: dict = {}          # credential -> live connection (ADR 0006)
        self._allow = {t.lower() for t in (tables or [])}
        self._resume_wait = resume_wait
        self._resume_timeout = resume_timeout
        self._conn = None
        self._schema_cache = None

    def _open(self, factory: Optional[Callable[[], object]] = None,
              deadline: Optional[float] = None):
        """A PAUSED serverless DB refuses connections fast (error 40613) — and the
        refused attempt is what triggers the auto-resume. Keep retrying the connect
        until the DB wakes (bounded); any other connect error raises immediately.

        #811: THE GATEWAY CANNOT TELL YOU WHICH IT IS. Azure answers 40613 - "Database 'x'
        on server 'y' is not currently available" - for a paused database that is resuming
        AND for a database that does not exist, with the same code and the same sentence.
        There is no signal to discriminate on: a resuming database eventually connects and a
        nonexistent one never does, so the ONLY way to tell them apart is to wait out the
        budget. That is defensible; spending it and then re-raising the raw
        `(40613, b'...')` bytes was not, because it told a user who fat-fingered a database
        name to wait for a resume that is never coming.

        So exhaustion raises a message naming BOTH readings, in the order they are worth
        checking. `deadline` is passed in by `_run` so a reconnect shares this call's budget
        rather than starting a second full one.
        """
        factory = factory or self._connect
        if deadline is None:
            deadline = time.monotonic() + self._resume_timeout
        while True:
            try:
                return factory()
            except Exception as exc:
                msg = str(exc)
                paused = "40613" in msg or "not currently available" in msg
                if not paused:
                    raise
                if time.monotonic() >= deadline:
                    raise AzureDatabaseUnavailable(
                        f"still unavailable after {self._resume_timeout:.0f}s. Either it is "
                        f"resuming from auto-pause and needs another minute, or the database "
                        f"or server name is wrong and there is nothing to resume - Azure "
                        f"answers the same 40613 for both. Check the name first, then retry."
                    ) from exc
                time.sleep(self._resume_wait)

    @classmethod
    def from_config(cls, config: dict) -> "AzureSqlEngine":
        missing = [k for k in ("server", "database", "user", "password")
                   if not config.get(k)]
        if missing:
            raise ValueError(f"azure_sql config missing {missing} "
                             "(use ${ENV} refs in stores.yml — resolved server-side)")

        driver = config.get("driver", "ODBC Driver 18 for SQL Server")

        def _odbc_login_connect():
            import pyodbc   # lazy (LAW 7)

            # Synapse dedicated pool rejects `USE <db>` (which pymssql issues to select the
            # database) — pyodbc selects it via the connstring `Database=` property instead,
            # no USE. Same TDS/T-SQL otherwise, so the rest of the engine is unchanged.
            return pyodbc.connect(
                f"Driver={{{driver}}};Server=tcp:{config['server']},1433;"
                f"Database={config['database']};Encrypt=yes;TrustServerCertificate=no;"
                f"Connection Timeout=75;Uid={config['user']};Pwd={config['password']}",
                timeout=75, autocommit=True)

        def _pymssql_connect():
            import pymssql   # lazy: optional dep, only at compose/query time (LAW 7)

            # timeout 75s: a paused serverless DB takes up to ~60s to auto-resume on
            # first contact — waiting is correct, failing fast is not.
            # #226: autocommit. pymssql defaults autocommit=OFF, so every SELECT opens a
            # transaction that is never committed and the connection sits idle-in-
            # transaction on the CUSTOMER's database, holding locks (blocks DDL, blocks
            # VACUUM/cleanup, pins the log). Every query here is read-only by construction
            # (validate_sql permits only SELECT/WITH), so there is nothing to commit.
            # The pyodbc login path above already got this right; this one did not - and
            # SynapseProvider reuses THIS engine verbatim.
            return pymssql.connect(server=config["server"], user=config["user"],
                                   password=config["password"],
                                   database=config["database"],
                                   tds_version="7.4", timeout=75, login_timeout=75,
                                   autocommit=True)

        # `use_odbc` (SynapseProvider sets it): SQL-login over pyodbc, no USE statement.
        connect = _odbc_login_connect if config.get("use_odbc") else _pymssql_connect

        def user_connect(token: str):
            try:
                import pyodbc   # lazy: optional dep, delegated asks only (LAW 7)
            except ImportError as exc:
                raise RuntimeError(
                    "azure_sql delegated (query-as-user) path needs pyodbc + "
                    "msodbcsql — not installed; failing closed (LAW 2)") from exc
            # #721 (LAW 2): NEVER pool delegated connections. The connection string
            # below is IDENTICAL for every user - the per-user AAD token rides only in
            # attrs_before, which the process-wide ODBC pool ignores when matching, and
            # applies only to genuinely NEW connections. With pooling on (the pyodbc
            # default), one user's closed connection can be handed to the NEXT user
            # still authenticated as its previous owner - probed live: bob counted
            # alice's 6 RLS rows same-process, his own 2 in a fresh process. Pooling is
            # a process-wide flag read at connect time; the engine already caches one
            # live connection per credential (ADR 0006), so this path loses nothing.
            pyodbc.pooling = False
            # #226: autocommit - same reason as the login path. This is the DELEGATED
            # (query-as-user) connection, so an uncommitted transaction would idle on the
            # customer DB under the USER's own identity. Read-only by construction.
            return pyodbc.connect(
                f"Driver={{{driver}}};Server=tcp:{config['server']},1433;"
                f"Database={config['database']};Encrypt=yes;"
                "TrustServerCertificate=no;Connection Timeout=75",
                attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: _aad_token_struct(token)},
                timeout=75, autocommit=True)

        # #811: the wake budget is now reachable from configuration. It was constructor-only,
        # so nothing in the product could set it and every deployment got 120s at 5s steps -
        # 24 attempts - including on a store whose database name was simply wrong. An
        # operator running a database that never auto-pauses (provisioned rather than
        # serverless) has no reason to wait at all, and could not say so. Absent keys keep
        # the existing defaults exactly, so no deployment changes behaviour by upgrading.
        kw = {}
        for key, cast in (("resume_wait", float), ("resume_timeout", float)):
            if config.get(key) is not None:
                try:
                    kw[key] = cast(config[key])
                except (TypeError, ValueError):
                    raise ValueError(f"azure_sql `{key}` must be a number, got "
                                     f"{config[key]!r}")
        return cls(connect, tables=config.get("tables"), user_connect=user_connect, **kw)

    def _run(self, sql: str):
        # #811: ONE budget per call. Both _open() sites used to compute their own deadline,
        # so a statement that opened a connection and then lost it could spend the full
        # resume_timeout twice - 240s of a caller's time for one query, with the ask-path
        # budget long since expired and a worker thread still burning. The deadline is taken
        # once here and shared, so "how long can this call wait" has a single answer.
        deadline = time.monotonic() + self._resume_timeout
        if self._conn is None:
            self._conn = self._open(deadline=deadline)
        try:
            cur = self._conn.cursor()
            cur.execute(sql)
        except Exception:
            # auto-pause severed the idle connection — reconnect ONCE and retry.
            # Upstream validate_sql guarantees reads only, so re-execute is harmless.
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = self._open(deadline=deadline)
            cur = self._conn.cursor()
            cur.execute(sql)
        return cur

    def _allowed(self, qualified: str, bare: str) -> bool:
        """An allowlist entry may be written either way. `SalesLT.Product` is the honest form
        for a real database; a bare `sales` keeps every existing stores.yml working. A bare
        entry deliberately does NOT match a same-named table in another schema — `audit.sales`
        is a different table from `dbo.sales`, and quietly widening the store to both would be
        a LAW-2 leak, not a convenience."""
        if not self._allow:
            return True
        if qualified.lower() in self._allow:
            return True
        return bare.lower() in self._allow and f"dbo.{bare}".lower() == qualified.lower()

    def schema(self) -> list:
        # cached per engine: every ask introspects (SQL generation + guard) and a
        # cloud round trip per query is waste — a recompose builds a fresh engine
        if self._schema_cache is not None:
            return self._schema_cache
        cur = self._run(_SCHEMA_SQL)
        out: list = []
        for tschema, table, column, dtype in cur.fetchall():
            qualified = f"{tschema}.{table}"
            if not self._allowed(qualified, table):
                continue
            if not out or out[-1]["table"] != qualified:
                out.append({"table": qualified, "columns": []})
            out[-1]["columns"].append({"name": column, "type": dtype})
        # #807: the #727 contract - never cache an EMPTY schema, or a fixed GRANT needs a
        # recompose to be seen. See RedshiftEngine.schema() for the full reasoning. This one
        # also covers SYNAPSE, which subclasses AzureSqlProvider and reuses this engine
        # verbatim - there is no synapse engine of its own to fix.
        if out:
            self._schema_cache = out
        return out

    def execute(self, sql: str, credential: "str | None" = None,
                principal: "str | None" = None) -> tuple:
        if credential is not None:
            if self._user_connect is None:
                # querying as the service account would break the delegation
                # promise, so with no token-auth path wired we fail CLOSED (LAW 2)
                raise RuntimeError(
                    "azure_sql store has no delegated (query-as-user) connect path "
                    "wired — failing closed (LAW 2); wire pyodbc/msodbcsql or "
                    "register a row_policy fallback instead")
            conn = self._user_conns.get(credential)
            if conn is None:
                conn = self._user_conns[credential] = self._open(
                    lambda: self._user_connect(credential))
            cur = conn.cursor()
            cur.execute(sql)
        else:
            cur = self._run(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        return cols, [tuple(r) for r in cur.fetchall()]

    def fk_edges(self) -> list:
        # Optimization signal only (#221 join graph) - any introspection failure
        # (missing VIEW DEFINITION grant, a severed auto-paused connection) must
        # degrade to [] rather than fail the store.
        try:
            cur = self._run(_FK_SQL)
            return [(a, b) for a, b in cur.fetchall()]
        except Exception:
            return []

    def refresh_schema(self) -> None:
        self._schema_cache = None


class AzureSqlProvider(StoreProviderPort):
    """config: server/database/user/password (${ENV} refs) + optional `tables` allowlist
    — the allowlist bounds BOTH introspection and the guard's visible schema."""

    kind = "azure_sql"
    modes = ("pushdown",)       # ADR 0008: queried in place, never copied

    def __init__(self, *, sql_generator: Optional[SqlGenerator] = None,
                 authorizer: Optional[Authorizer] = None,
                 engine_factory: Optional[Callable[[dict], SqlEnginePort]] = None,
                 broker=None, embedder=None, value_llm=None) -> None:
        self._gen = sql_generator
        self._authorizer = authorizer
        self._broker = broker
        # #222 Fix 2: the edition's embedder reaches the #221 schema index (same wiring
        # LocalIndexProvider has had since #143). None -> lazy HashingEmbedding default.
        self._embedder = embedder
        # #462: the edition's chat model for the literal-resolution rung; the
        # in_tenant gate lives in dictionary._llm_pick, absent-means-no (LAW 1).
        self._value_llm = value_llm
        self._engine_factory = engine_factory or AzureSqlEngine.from_config

    def _make(self, config: dict) -> FederatedSqlStore:
        authorizer = self._authorizer
        if authorizer is None and self._broker is not None:
            # bind this store's authorize() to the broker (ADR 0006 precedence:
            # delegation -> row policy -> principals), keyed by the store id
            sid = config["id"]
            authorizer = lambda u, _s=sid: self._broker.access_for(u, _s)  # noqa: E731
        return FederatedSqlStore(
            store_id=config["id"], business_unit=config.get("business_unit", ""),
            title=config.get("title", config["id"]),
            description=config.get("description", ""),
            engine=self._engine_factory(config),
            sql_generator=self._gen, authorizer=authorizer,
            topics=config.get("topics") or [], embedder=self._embedder,
            value_llm=self._value_llm)

    def probe(self, config: dict) -> StoreProfile:
        return self._make(config).profile()

    def build(self, config: dict) -> FederatedSqlStore:
        return self._make(config)
