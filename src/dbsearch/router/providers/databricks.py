"""`kind: databricks` — a Databricks SQL warehouse as a federated pushdown store
(#107, ADR 0007). A SqlEnginePort sibling; guard/audit/NL2SQL stay in
FederatedSqlStore.

databricks-sql-connector is DB-API shaped and imports lazily inside the connection
factory (LAW 7). The token arrives via ${ENV} in stores.yml, resolved SERVER-side
(LAW 1). The connection pins a session catalog+schema, so the NL2SQL's unqualified
table names and the visible-schema guard work unchanged; introspection reads the
catalog's information_schema filtered to that schema. Free Edition serverless
warehouses cold-start on first contact — the connector blocks through it, so no
extra resume dance is needed (unlike Azure's 40613 refusal).
"""
from __future__ import annotations

from typing import Callable, Optional

from dbsearch.router.provider import StoreProviderPort
from dbsearch.router.store import StoreProfile
from dbsearch.router.structured import (
    Authorizer, FederatedSqlStore, SqlEnginePort, SqlGenerator,
)


class DatabricksEngine(SqlEnginePort):
    dialect = "Databricks Spark SQL"   # #714

    def __init__(self, connect: Callable[[], object], catalog: str, schema: str,
                 tables: Optional[list] = None,
                 user_connect: Optional[Callable[[str], object]] = None) -> None:
        self._connect = connect
        self._catalog = catalog
        self._db_schema = schema
        self._allow = {t.lower() for t in (tables or [])}
        self._conn = None
        self._schema_cache = None
        self._user_connect = user_connect
        self._user_conns: dict = {}

    @classmethod
    def from_config(cls, config: dict) -> "DatabricksEngine":
        missing = [k for k in ("host", "http_path", "token") if not config.get(k)]
        if missing:
            raise ValueError(f"databricks config missing {missing} "
                             "(use ${ENV} refs in stores.yml — resolved server-side)")
        catalog = config.get("catalog", "workspace")     # Free Edition defaults
        schema = config.get("schema", "default")

        def _open(token):
            from databricks import sql   # lazy optional dep (LAW 7)

            return sql.connect(
                server_hostname=str(config["host"]).replace("https://", ""),
                http_path=config["http_path"], access_token=token,
                catalog=catalog, schema=schema)          # session defaults: `FROM sales` works

        # ADR 0006: a delegated credential opens the SAME warehouse as that user's
        # token — Unity Catalog grants then enforce source-side.
        return cls(lambda: _open(config["token"]), catalog=catalog, schema=schema,
                   tables=config.get("tables"), user_connect=_open)

    def _run(self, sql: str):
        if self._conn is None:
            self._conn = self._connect()
        try:
            cur = self._conn.cursor()
            cur.execute(sql)
        except Exception:
            # stale session (warehouse idled out) — reconnect ONCE and retry;
            # upstream validate_sql guarantees reads only, so re-execute is harmless
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = self._connect()
            cur = self._conn.cursor()
            cur.execute(sql)
        return cur

    def schema(self) -> list:
        if self._schema_cache is not None:   # one introspection per engine lifetime
            return self._schema_cache
        cur = self._run(
            "SELECT table_name, column_name, data_type "
            f"FROM {self._catalog}.information_schema.columns "
            f"WHERE table_schema = '{self._db_schema}' "
            "ORDER BY table_name, ordinal_position")
        out: list = []
        for table, column, dtype in cur.fetchall():
            if self._allow and table.lower() not in self._allow:
                continue
            if not out or out[-1]["table"] != table:
                out.append({"table": table, "columns": []})
            out[-1]["columns"].append({"name": column, "type": dtype})
        self._schema_cache = out
        return out

    def execute(self, sql: str, credential: "str | None" = None,
                principal: "str | None" = None) -> tuple:
        if credential is not None:
            if self._user_connect is None:
                raise RuntimeError("databricks store has no delegated connect path "
                                   "wired — failing closed (LAW 2)")
            conn = self._user_conns.get(credential)
            if conn is None:
                conn = self._user_conns[credential] = self._user_connect(credential)
            cur = conn.cursor()
            cur.execute(sql)
        else:
            cur = self._run(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        return cols, [tuple(r) for r in cur.fetchall()]

    def refresh_schema(self) -> None:
        self._schema_cache = None


class DatabricksProvider(StoreProviderPort):
    """config: host/http_path/token (${ENV} refs) + optional catalog (default
    'workspace'), schema (default 'default') and `tables` allowlist bounding BOTH
    introspection and the guard's visible schema."""

    kind = "databricks"
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
        self._engine_factory = engine_factory or DatabricksEngine.from_config

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
