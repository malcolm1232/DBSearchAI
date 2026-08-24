"""`kind: mysql` — Azure Database for MySQL (and any MySQL) as a federated pushdown store
(#158, template-cloned from postgres.py / azure_sql.py, ADR 0007). MySqlEngine is a
SqlEnginePort sibling: the SQL executes INSIDE MySQL and only the result set comes back.
The read-only guard, audit trail, NL2SQL seam and row-policy fallback all stay in
FederatedSqlStore; this module only introspects information_schema and executes.

LAW 7: PyMySQL is imported lazily inside the connection factory — only needed when a
manifest actually composes a mysql store. Secrets arrive via ${ENV} in stores.yml,
resolved SERVER-side (LAW 1). TLS is required (Azure MySQL sets require_secure_transport=ON);
a permissive SSL context (encrypt, don't verify) keeps the demo CA-free — tighten with a
real CA for production.

Delegated auth (ADR 0006, query-as-user): Azure Database for MySQL accepts an Entra access
token AS THE PASSWORD for the AAD principal. The delegated path opens its OWN connection
with that token so MySQL enforces THAT user's grants source-side. One connection per
credential; no aad_user configured -> the delegated ask FAILS CLOSED (LAW 2), never the
service identity.

MySQL note: information_schema.table_schema is the DATABASE name (not Postgres' 'public'),
so introspection scopes to DATABASE() — the connected database.
"""
from __future__ import annotations

from typing import Callable, Optional

from dbsearch.router.provider import StoreProviderPort
from dbsearch.router.providers.rds_auth import mint_token
from dbsearch.router.store import StoreProfile
from dbsearch.router.structured import (
    Authorizer, FederatedSqlStore, SqlEnginePort, SqlGenerator,
    call_user_connect, entra_principal_from_token,
)

_SCHEMA_SQL = (
    "SELECT c.table_name, c.column_name, c.data_type "
    "FROM information_schema.columns c "
    "JOIN information_schema.tables t "
    "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
    "WHERE t.table_type = 'BASE TABLE' AND c.table_schema = DATABASE() "
    "ORDER BY c.table_name, c.ordinal_position"
)

# MySQL has no schemas-within-database (table_schema IS the database), so schema()
# spells table names BARE - match that exactly here, scoped to the connected database
# via TABLE_SCHEMA = DATABASE() the same way _SCHEMA_SQL does.
_FK_SQL = (
    "SELECT TABLE_NAME, REFERENCED_TABLE_NAME "
    "FROM information_schema.KEY_COLUMN_USAGE "
    "WHERE REFERENCED_TABLE_NAME IS NOT NULL AND TABLE_SCHEMA = DATABASE()"
)


def _ssl_context(verify: bool):
    import ssl

    ctx = ssl.create_default_context()
    if not verify:                       # demo default: encrypt, don't verify the cert/CA
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class MySqlEngine(SqlEnginePort):
    dialect = "MySQL"        # #714: named to the generator so its syntax stays valid

    def __init__(self, connect: Callable[[], object], tables: Optional[list] = None,
                 user_connect: Optional[Callable[[str], object]] = None) -> None:
        self._connect = connect
        self._user_connect = user_connect
        self._user_conns: dict = {}          # credential -> live connection (ADR 0006)
        self._allow = {t.lower() for t in (tables or [])}
        self._conn = None
        self._schema_cache = None

    @classmethod
    def from_config(cls, config: dict) -> "MySqlEngine":
        missing = [k for k in ("host", "database", "user", "password")
                   if not config.get(k)]
        if missing:
            raise ValueError(f"mysql config missing {missing} "
                             "(use ${ENV} refs in stores.yml — resolved server-side)")
        port = int(config.get("port", 3306))
        verify = bool(config.get("ssl_verify", False))   # Azure MySQL requires TLS

        def _open(user: str, password: str):
            import pymysql   # lazy: optional dep, only at compose/query time (LAW 7)

            # #226: autocommit. The driver defaults to autocommit=OFF, so every SELECT
            # opens a transaction that is NEVER committed - the connection then sits
            # `idle in transaction` on the CUSTOMER's database, holding locks. Caught
            # live: this connector's own fk_edges() introspection sat idle-in-transaction
            # for hours and BLOCKED DDL (a DROP SCHEMA queued behind it as `wait: Lock`).
            # On a live DB that also blocks VACUUM and pins the WAL. Every query on this
            # rail is read-only by construction (validate_sql permits only SELECT/WITH),
            # so there is nothing to commit and autocommit is simply correct.
            return pymysql.connect(host=config["host"], user=user, password=password,
                                   database=config["database"], port=port,
                                   ssl=_ssl_context(verify), connect_timeout=30,
                                   autocommit=True)

        def connect():
            return _open(config["user"], config["password"])

        aad_user = config.get("aad_user")

        def user_connect(token: str, session_principal: "str | None" = None):
            # The delegated connection authenticates as a DB principal with `token` as the
            # password. Azure MySQL (#189) puts the Entra principal INSIDE the token (a JWT), so
            # derive it. Cloud SQL / Google IAM database auth (#193) uses an OPAQUE OAuth access
            # token carrying no principal, so the user's email - the session identity - is threaded
            # in from the AccessContext. Precedence: `aad_user` pin, then the token's Entra
            # principal, then the session principal. None -> fail closed (LAW 2).
            principal = aad_user or entra_principal_from_token(token) or (session_principal or "")
            if not principal:
                raise RuntimeError(
                    "mysql delegated (query-as-user) path: no `aad_user` pin, no Entra principal "
                    "in the token, and no session principal to authenticate as; failing closed "
                    "(LAW 2)")
            return _open(principal, token)

        return cls(connect, tables=config.get("tables"), user_connect=user_connect)

    def _run(self, sql: str):
        if self._conn is None:
            self._conn = self._connect()
        try:
            cur = self._conn.cursor()
            cur.execute(sql)
        except Exception:
            # a dropped/idle connection — reconnect ONCE and retry. Upstream validate_sql
            # guarantees reads only, so re-executing is harmless.
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = self._connect()
            cur = self._conn.cursor()
            cur.execute(sql)
        return cur

    def schema(self) -> list:
        if self._schema_cache is not None:
            return self._schema_cache
        cur = self._run(_SCHEMA_SQL)
        out: list = []
        for table, column, dtype in cur.fetchall():
            if self._allow and table.lower() not in self._allow:
                continue
            if not out or out[-1]["table"] != table:
                out.append({"table": table, "columns": []})
            out[-1]["columns"].append({"name": column, "type": dtype})
        # #807: the #727 contract - never cache an EMPTY schema, or a fixed GRANT needs a
        # recompose to be seen. See RedshiftEngine.schema() for the full reasoning.
        if out:
            self._schema_cache = out
        return out

    def execute(self, sql: str, credential: "str | None" = None,
                principal: "str | None" = None) -> tuple:
        if credential is not None:
            if self._user_connect is None:
                raise RuntimeError(
                    "mysql store has no delegated (query-as-user) connect path wired "
                    "— failing closed (LAW 2); set `aad_user` or register a row_policy")
            conn = self._user_conns.get(credential)
            if conn is None:
                conn = self._user_conns[credential] = call_user_connect(
                    self._user_connect, credential, principal)
            cur = conn.cursor()
            cur.execute(sql)
        else:
            cur = self._run(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        return cols, [tuple(r) for r in cur.fetchall()]

    def fk_edges(self) -> list:
        # Optimization signal only (#221 join graph) - any introspection failure
        # (missing grants, a dropped connection) must degrade to [] rather than fail
        # the store.
        try:
            cur = self._run(_FK_SQL)
            return [(a, b) for a, b in cur.fetchall()]
        except Exception:
            return []

    def refresh_schema(self) -> None:
        self._schema_cache = None


class RdsMySqlEngine(MySqlEngine):
    """ADR 0026 (#814): the same MySQL wire protocol, an IAM auth token as the password.
    The mirror of RdsPostgresEngine - see that docstring; the one difference is the
    default port (3306) and PyMySQL's always-on TLS context, which IAM auth requires."""

    _introspect_credential: "str | None" = None   # ADR 0022, per-caller schema reads

    @classmethod
    def from_config(cls, config: dict, opener=None,
                    rds_client_factory=None) -> "RdsMySqlEngine":
        missing = [k for k in ("host", "database", "user") if not config.get(k)]
        if missing:
            raise ValueError(f"rds_mysql config missing {missing}")
        port = int(config.get("port", 3306))
        verify = bool(config.get("ssl_verify", False))

        def _open(user: str, password: str):
            if opener is not None:
                return opener(user, password)
            import pymysql   # lazy: optional dep, only at compose/query time (LAW 7)

            # autocommit for the same #226 reason the base engine documents.
            return pymysql.connect(host=config["host"], user=user, password=password,
                                   database=config["database"], port=port,
                                   ssl=_ssl_context(verify), connect_timeout=30,
                                   autocommit=True)

        def connect():
            pw = config.get("password")
            if not pw:
                raise RuntimeError(
                    "rds store has no typed password and no delegated AWS credential "
                    "for this call - queries run with your own AWS keys: connect "
                    "Amazon in the account menu (a self-host manifest may set "
                    "`password` instead)")
            return _open(config["user"], pw)

        def user_connect(triple: str, session_principal: "str | None" = None):
            token = mint_token(config, triple, port, rds_client_factory)
            return _open(config["user"], token)

        return cls(connect, tables=config.get("tables"), user_connect=user_connect)

    def introspect_as(self, credential: str) -> None:
        """Read the schema with this caller's delegated credential (ADR 0022)."""
        if credential != self._introspect_credential:
            self._schema_cache = None
        self._introspect_credential = credential

    def _run(self, sql: str):
        cred = self._introspect_credential
        if cred:
            conn = self._user_conns.get(cred)
            if conn is None:
                conn = self._user_conns[cred] = call_user_connect(
                    self._user_connect, cred, None)
            cur = conn.cursor()
            cur.execute(sql)
            return cur
        return super()._run(sql)


class MySqlProvider(StoreProviderPort):
    """config: host/database/user/password (${ENV} refs) + optional `port`, `ssl_verify`,
    `tables` allowlist, `aad_user` (for OBO). The allowlist bounds BOTH introspection and
    the guard's visible schema."""

    kind = "mysql"
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
        self._engine_factory = engine_factory or MySqlEngine.from_config

    def _make(self, config: dict) -> FederatedSqlStore:
        authorizer = self._authorizer
        if authorizer is None and self._broker is not None:
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


class RdsMySqlProvider(MySqlProvider):
    """`kind: rds_mysql` - Amazon RDS / Aurora MySQL. The same engine under a truthful name.

    #672, and the sibling of RdsPostgresProvider - see that docstring for the full reasoning.
    In short: RDS MySQL is MySQL over TLS, so this adds no capability; it makes the store
    findable under AWS in the canvas and stops origins.SYSTEM citing "Azure MySQL" for a
    database that is not in Azure. ADR 0026 (#814): authenticates per-caller through the
    vaulted aws_keys -> IAM auth token path - see RdsPostgresProvider.
    """

    kind = "rds_mysql"

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        if not kw.get("engine_factory"):
            self._engine_factory = RdsMySqlEngine.from_config

    def _make(self, config: dict, credential: "str | None" = None) -> FederatedSqlStore:
        # Mirrors RdsPostgresProvider._make (ADR 0022) - optional-capability idiom.
        authorizer = self._authorizer
        if authorizer is None and self._broker is not None:
            sid = config["id"]
            authorizer = lambda u, _s=sid: self._broker.access_for(u, _s)  # noqa: E731
        engine = self._engine_factory(config)
        if credential and hasattr(engine, "introspect_as"):
            engine.introspect_as(credential)
        return FederatedSqlStore(
            store_id=config["id"], business_unit=config.get("business_unit", ""),
            title=config.get("title", config["id"]),
            description=config.get("description", ""),
            engine=engine,
            sql_generator=self._gen, authorizer=authorizer,
            topics=config.get("topics") or [], embedder=self._embedder,
            value_llm=self._value_llm)

    def probe_as(self, config: dict, credential: "str | None" = None) -> StoreProfile:
        """ADR 0022: introspect as the caller when the store declares a delegation."""
        return self._make(config, credential=credential).profile()

    def build_as(self, config: dict,
                 credential: "str | None" = None) -> FederatedSqlStore:
        """ADR 0022's second half (the #665 lesson) - see RdsPostgresProvider."""
        return self._make(config, credential=credential)
