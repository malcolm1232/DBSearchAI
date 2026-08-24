"""`kind: postgres` — Azure Database for PostgreSQL (and any PostgreSQL) as a federated
pushdown store (#155 sibling of azure_sql, ADR 0007). PostgresEngine is a SqlEnginePort
sibling of AzureSqlEngine: the SQL executes INSIDE Postgres and only the result set comes
back. The read-only guard, audit trail, NL2SQL seam and row-policy fallback all stay in
FederatedSqlStore; this module only introspects information_schema and executes.

LAW 7: psycopg (v3) is imported lazily inside the connection factory — only needed when a
manifest actually composes a postgres store. Secrets arrive via ${ENV} in stores.yml,
resolved SERVER-side (LAW 1). SSL is required (Azure Postgres rejects non-TLS).

Delegated auth (ADR 0006, query-as-user): Azure Database for PostgreSQL accepts an Entra
access token (audience https://ossrdbms-aad.database.windows.net) AS THE PASSWORD for the
AAD principal. The delegated path opens its OWN connection with that token so Postgres
enforces THAT user's grants/RLS source-side. One connection per credential (mirrors
AzureSqlEngine); no aad user configured -> the delegated ask FAILS CLOSED (LAW 2), never
the service identity.
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

# Real Postgres databases put tables in NAMED schemas. Hard-coding table_schema = 'public'
# made every one of them invisible — the store reported "no tables are visible to your grants"
# on a database full of data (#214, the sibling of #203). Discover every user schema and keep
# the qualifier: it is part of the table's identity, and the generators drop schema[i]["table"]
# straight into `FROM ...`, so a bare name cannot address a table outside the search_path.
_SCHEMA_SQL = (
    "SELECT c.table_schema, c.table_name, c.column_name, c.data_type "
    "FROM information_schema.columns c "
    "JOIN information_schema.tables t "
    "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
    "WHERE t.table_type = 'BASE TABLE' "
    "AND c.table_schema NOT IN ('pg_catalog', 'information_schema') "
    "AND c.table_schema NOT LIKE 'pg_toast%' "
    "ORDER BY c.table_schema, c.table_name, c.ordinal_position"
)
_DEFAULT_SCHEMA = "public"

# Postgres has no native "list foreign keys" view; the standard join is
# table_constraints (the FK constraint itself) against constraint_column_usage (the
# REFERENCED side). Qualified schema.table on both sides to match schema()'s own
# spelling exactly - an unqualified name here would silently match nothing in the graph.
_FK_SQL = (
    "SELECT tc.table_schema || '.' || tc.table_name, "
    "ccu.table_schema || '.' || ccu.table_name "
    "FROM information_schema.table_constraints tc "
    "JOIN information_schema.constraint_column_usage ccu "
    "ON ccu.constraint_name = tc.constraint_name "
    "AND ccu.constraint_schema = tc.constraint_schema "
    "WHERE tc.constraint_type = 'FOREIGN KEY'"
)




class PostgresEngine(SqlEnginePort):
    dialect = "PostgreSQL"   # #714: named to the generator so its syntax stays valid

    def __init__(self, connect: Callable[[], object], tables: Optional[list] = None,
                 user_connect: Optional[Callable[[str], object]] = None) -> None:
        self._connect = connect
        self._user_connect = user_connect
        self._user_conns: dict = {}          # credential -> live connection (ADR 0006)
        self._allow = {t.lower() for t in (tables or [])}
        self._conn = None
        self._schema_cache = None

    @classmethod
    def from_config(cls, config: dict) -> "PostgresEngine":
        missing = [k for k in ("host", "database", "user", "password")
                   if not config.get(k)]
        if missing:
            raise ValueError(f"postgres config missing {missing} "
                             "(use ${ENV} refs in stores.yml — resolved server-side)")
        port = int(config.get("port", 5432))
        sslmode = config.get("sslmode", "require")   # Azure Postgres rejects non-TLS

        def _open(user: str, password: str):
            import psycopg   # lazy: optional dep, only at compose/query time (LAW 7)

            # #226: autocommit. The driver defaults to autocommit=OFF, so every SELECT
            # opens a transaction that is NEVER committed - the connection then sits
            # `idle in transaction` on the CUSTOMER's database, holding locks. Caught
            # live: this connector's own fk_edges() introspection sat idle-in-transaction
            # for hours and BLOCKED DDL (a DROP SCHEMA queued behind it as `wait: Lock`).
            # On a live DB that also blocks VACUUM and pins the WAL. Every query on this
            # rail is read-only by construction (validate_sql permits only SELECT/WITH),
            # so there is nothing to commit and autocommit is simply correct.
            return psycopg.connect(host=config["host"], dbname=config["database"],
                                   user=user, password=password, port=port,
                                   sslmode=sslmode, connect_timeout=30,
                                   autocommit=True)

        def connect():
            return _open(config["user"], config["password"])

        aad_user = config.get("aad_user")

        def user_connect(token: str, session_principal: "str | None" = None):
            # The delegated connection authenticates as a DB principal with `token` as the
            # password. Azure Postgres (#188) puts the Entra principal INSIDE the token (a JWT),
            # so derive it. Cloud SQL / Google IAM database auth (#193) uses an OPAQUE OAuth
            # access token that carries no principal, so the user's email - the session identity -
            # is threaded in from the AccessContext. Precedence: an explicit `aad_user` pin, then
            # the token's own Entra principal, then the session principal. None -> fail closed so
            # a query-as-user store can never silently run as the wrong identity (LAW 2).
            principal = aad_user or entra_principal_from_token(token) or (session_principal or "")
            if not principal:
                raise RuntimeError(
                    "postgres delegated (query-as-user) path: no `aad_user` pin, no Entra "
                    "principal in the token, and no session principal to authenticate as; "
                    "failing closed (LAW 2)")
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

    def _allowed(self, qualified: str, bare: str) -> bool:
        """An allowlist entry may be written either way: `support.tickets` is the honest form
        for a real database, a bare `tickets` keeps existing configs working. A bare entry
        means the DEFAULT schema ONLY — it must not also drag in a same-named table from
        another schema, which is a different table and would silently widen the store (LAW 2)."""
        if not self._allow:
            return True
        if qualified.lower() in self._allow:
            return True
        return (bare.lower() in self._allow
                and qualified.lower() == f"{_DEFAULT_SCHEMA}.{bare}".lower())

    def schema(self) -> list:
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
        # #807 (the #727 contract, applied to the rest of the rail): never cache an EMPTY
        # schema. Zero tables is an error state - privileges, or an allowlist entry that
        # matched nothing (#808) - and caching it makes the store decline for the engine's
        # whole lifetime, so a fixed GRANT needs a recompose to be seen. `_read_schema`'s ONE
        # refresh retry can only rescue a repaired source if the retry actually re-reads.
        # A populated schema still caches once per engine lifetime.
        if out:
            self._schema_cache = out
        return out

    def execute(self, sql: str, credential: "str | None" = None,
                principal: "str | None" = None) -> tuple:
        if credential is not None:
            if self._user_connect is None:
                raise RuntimeError(
                    "postgres store has no delegated (query-as-user) connect path wired "
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
        # FK edges feed the #221 join graph as an optimization signal, never a
        # requirement (Redshift/Synapse/BigQuery routinely declare none at all) - so any
        # introspection failure (missing grants, a dead connection) must degrade to []
        # rather than take the store down.
        try:
            cur = self._run(_FK_SQL)
            return [(a, b) for a, b in cur.fetchall()]
        except Exception:
            return []

    def refresh_schema(self) -> None:
        self._schema_cache = None


class RdsPostgresEngine(PostgresEngine):
    """ADR 0026 (#814): the same Postgres wire protocol, an IAM auth token as the password.

    from_config requires host/database/user - NOT password. The delegated path redeems the
    caller's STS triple (ADR 0024) into `rds:generate-db-auth-token` for the CONFIGURED db
    user and connects with the token as the password; sslmode stays "require" (IAM auth
    demands TLS). A typed password keeps the self-host service path (ADR 0010 form 2). The
    Entra `aad_user` path is structurally unreachable: user_connect is replaced wholesale,
    so an Entra token can never be redeemed against an AWS database.

    `opener` / `rds_client_factory` are test seams, the redshift engine's
    `user_client_factory` idiom - production passes neither.
    """

    # ADR 0022 (via ADR 0026): whose credential reads information_schema. None = the
    # service path (typed password on a self-host box); a hosted RDS store introspects
    # as the caller, same as RedshiftEngine.
    _introspect_credential: "str | None" = None

    @classmethod
    def from_config(cls, config: dict, opener=None,
                    rds_client_factory=None) -> "RdsPostgresEngine":
        missing = [k for k in ("host", "database", "user") if not config.get(k)]
        if missing:
            raise ValueError(f"rds_postgres config missing {missing}")
        port = int(config.get("port", 5432))
        sslmode = config.get("sslmode", "require")   # IAM auth requires TLS

        def _open(user: str, password: str):
            if opener is not None:
                return opener(user, password)
            import psycopg   # lazy: optional dep, only at compose/query time (LAW 7)

            # autocommit for the same #226 reason the base engine documents.
            return psycopg.connect(host=config["host"], dbname=config["database"],
                                   user=user, password=password, port=port,
                                   sslmode=sslmode, connect_timeout=30,
                                   autocommit=True)

        def connect():
            pw = config.get("password")
            if not pw:
                # The honest skip reason a palette-added store composes to when the
                # caller has no AWS link - it names the remedy (#802 family), because
                # there is no password to silently fall back to.
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
        """Read the schema with this caller's delegated credential (ADR 0022). The cache
        drops when the credential changes - two callers legitimately see two schemas."""
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


class PostgresProvider(StoreProviderPort):
    """config: host/database/user/password (${ENV} refs) + optional `port`, `sslmode`,
    `tables` allowlist, `aad_user` (for OBO). The allowlist bounds BOTH introspection and
    the guard's visible schema."""

    kind = "postgres"
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
        self._engine_factory = engine_factory or PostgresEngine.from_config

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


class RdsPostgresProvider(PostgresProvider):
    """`kind: rds_postgres` - Amazon RDS / Aurora PostgreSQL. THE SAME ENGINE, a truthful name.

    #672. Nothing about the transport differs: RDS and Aurora speak Postgres over TLS, and
    psycopg does not care whose cloud the host is in. So this adds no capability - it adds
    DISCOVERABILITY and HONESTY, which were the actual defects:

      - discoverability. The canvas filed `postgres` under a provider group labelled Azure,
        and the AWS group offered only Redshift. A customer whose business runs on RDS
        Postgres - by far the most common thing anyone means by "our database on AWS" -
        opened the AWS group and was offered a data warehouse they do not have. The
        capability existed and nobody could find it.
      - honesty. origins.SYSTEM maps "postgres" -> "Azure Postgres", so an RDS-backed answer
        cited its source as Azure. A citation that names the wrong cloud is the #664 family:
        the product stating something more confident than true.

    A distinct kind (rather than the same kind listed under two groups) is the owner's
    ruling, and it earns its keep in the stored manifest: `kind: rds_postgres` says what the
    store IS, where a bare `postgres` cannot tell you which cloud the user thought they were
    connecting to.

    DELEGATION IS THE ENTRA RAIL'S OPPOSITE HERE (ADR 0026, #814). Azure Postgres delegates
    by presenting an Entra access token AS the password (audience ossrdbms-aad); wiring
    `aad_user` to an RDS store would redeem an Entra token against an AWS database -
    actively wrong, and structurally unreachable because RdsPostgresEngine replaces
    user_connect wholesale. RDS's honest equivalent IS wired now: the caller's vaulted
    aws_keys (ADR 0024) redeem into `rds:generate-db-auth-token`, a 15-minute IAM token
    used as the password for the configured db user - so this rail authenticates
    per-caller like redshift/s3, and the panel collects no password at all. A typed
    password in a hand-written manifest keeps the ADR 0010 self-host path.
    """

    kind = "rds_postgres"

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        if not kw.get("engine_factory"):
            self._engine_factory = RdsPostgresEngine.from_config

    def _make(self, config: dict, credential: "str | None" = None) -> FederatedSqlStore:
        # Mirrors RedshiftProvider._make (ADR 0022): the credential reaches the engine
        # via the optional-capability idiom, so fixture engines keep introspecting
        # however they already do.
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
        """ADR 0022's second half - probe and build produce SEPARATE engines and retrieve
        re-reads the schema off the BUILT one (the #665 lesson)."""
        return self._make(config, credential=credential)
