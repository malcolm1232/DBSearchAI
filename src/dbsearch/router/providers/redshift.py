"""`kind: redshift` — Redshift Serverless via the Data API as a federated pushdown
store (#107, ADR 0007). A SqlEnginePort sibling; guard/audit/NL2SQL stay in
FederatedSqlStore.

The Data API is the right transport here: HTTPS + SigV4 (ambient AWS credentials on
the SERVER, LAW 1), no cluster endpoint to expose, no psycopg/TDS driver. It is
async — execute_statement returns an id, the engine polls describe_statement to
completion and decodes get_statement_result's typed record fields. A paused
serverless workgroup auto-resumes during the poll window, so the pause never
surfaces to the caller. boto3 imports lazily (LAW 7). ADR 0006's delegated path
(STS AssumeRole per user) swaps the client factory at the same seam.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from dbsearch.router.provider import StoreProviderPort
from dbsearch.router.store import StoreProfile
from dbsearch.router.structured import (
    Authorizer, FederatedSqlStore, SqlEnginePort, SqlGenerator,
)

# Same trap as postgres (#214) / azure_sql (#203): hard-coding table_schema = 'public' makes
# every table in a real named schema INVISIBLE, and a bare table name cannot address one
# anyway (the generators put schema[i]["table"] straight into `FROM ...`).
_SCHEMA_SQL = (
    "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
    "WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'pg_internal') "
    "ORDER BY table_schema, table_name, ordinal_position"
)
_DEFAULT_SCHEMA = "public"
_TERMINAL = ("FINISHED", "FAILED", "ABORTED")


def _decode(field: dict):
    if field.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue"):
        if key in field:
            return field[key]
    return None


class RedshiftEngine(SqlEnginePort):
    dialect = "Amazon Redshift (PostgreSQL-like)"   # #714

    def __init__(self, client_factory: Callable[[], object], workgroup: str,
                 database: str, tables: Optional[list] = None,
                 poll_interval: float = 0.25, timeout: float = 180.0,
                 region: Optional[str] = None,
                 user_client_factory: Optional[Callable[[dict], object]] = None,
                 cold_hint_after: float = 5.0, cold_idle_s: float = 300.0) -> None:
        self._client_factory = client_factory
        self._workgroup = workgroup
        self._database = database
        self._allow = {t.lower() for t in (tables or [])}
        self._poll = poll_interval
        self._timeout = timeout      # generous: covers serverless auto-resume
        self._region = region
        self._client = None
        # #719: the cold-start ledger. MEASURED (260817, dbsearch-verify-wg): a paused
        # serverless workgroup does NOT refuse execute_statement - the Data API accepts and
        # QUEUES the statement while the warehouse wakes (observed 11.8s cold vs 2.2s warm),
        # so there is no Azure-40613-style fast failure to retry on, and the ask path's 8s
        # executor budget fires first. The engine records a hint when a statement sits
        # pending past `cold_hint_after` on the ASK path AND the engine was idle for
        # `cold_idle_s` (or has never finished a statement) - idleness is the discriminator
        # that keeps a merely-slow WARM query honestly labeled as a plain timeout.
        self._cold_hint_after = cold_hint_after
        self._cold_idle_s = cold_idle_s
        self._last_finished: "float | None" = None
        self._cold_hint = ""
        self._schema_cache = None
        self._user_client_factory = user_client_factory or self._default_user_client
        self._user_clients: dict = {}
        # ADR 0022 (via ADR 0024): whose credential reads information_schema. None = the
        # server's ambient AWS identity, which is right for a self-host box and impossible
        # for a hosted deployment reading a user's own Redshift.
        self._introspect_credential: "str | None" = None

    def introspect_as(self, credential: str) -> None:
        """Read the schema with this delegated STS triple instead of the ambient identity
        (ADR 0022). Set per caller, so the profile this engine yields is the one THAT user
        is entitled to - the cache is dropped when the credential changes, because two
        callers legitimately see two schemas for one store id."""
        if credential != self._introspect_credential:
            self._schema_cache = None
        self._introspect_credential = credential

    def _default_user_client(self, cred: dict):
        # ADR 0006: the STS triple becomes the caller identity — the role's Redshift /
        # Lake Formation grants then enforce source-side, and CloudTrail attributes
        # per user via the AwsStsExchange session name.
        import boto3

        return boto3.client("redshift-data", region_name=self._region,
                            aws_access_key_id=cred["access_key_id"],
                            aws_secret_access_key=cred["secret_access_key"],
                            aws_session_token=cred["session_token"])

    @classmethod
    def from_config(cls, config: dict) -> "RedshiftEngine":
        missing = [k for k in ("workgroup", "database") if not config.get(k)]
        if missing:
            raise ValueError(f"redshift config missing {missing}")

        def make_client():
            import boto3   # lazy optional dep (LAW 7); SigV4 creds are ambient/server-side

            return boto3.client("redshift-data",
                                region_name=config.get("region") or None)

        return cls(make_client, workgroup=config["workgroup"],
                   database=config["database"], tables=config.get("tables"),
                   region=config.get("region"))

    def cold_start_hint(self) -> str:
        """#719: the ask-path cold-start remedy, if the last pending statement earned one.

        Returns-and-clears: the executor reads this exactly once, at its own timeout, and a
        hint must never outlive the timeout it explains."""
        hint, self._cold_hint = self._cold_hint, ""
        return hint

    def _run(self, sql: str, credential: "str | None" = None,
             wake: bool = True) -> dict:
        if credential is not None:
            import json as _json

            key = credential
            client = self._user_clients.get(key)
            if client is None:
                client = self._user_clients[key] = \
                    self._user_client_factory(_json.loads(credential))
        else:
            if self._client is None:
                self._client = self._client_factory()
            client = self._client
        started = time.monotonic()
        # #719: was this engine cold when the statement went in? Decided BEFORE polling -
        # the first statement after an idle stretch is the cold-start shape.
        was_cold = (self._last_finished is None
                    or started - self._last_finished > self._cold_idle_s)
        sid = client.execute_statement(
            WorkgroupName=self._workgroup, Database=self._database, Sql=sql)["Id"]
        deadline = started + self._timeout
        while True:
            desc = client.describe_statement(Id=sid)
            if desc["Status"] in _TERMINAL:
                break
            # #719: the ask path's executor budget (8s) will fire long before this poll's
            # 180s deadline while a paused warehouse wakes. Record the honest explanation
            # for that timeout - only on the ask path (wake=False; introspection/health
            # ride the long poll and never label asks), and only when the engine was cold.
            if (not wake and was_cold and not self._cold_hint
                    and time.monotonic() - started >= self._cold_hint_after):
                self._cold_hint = ("this source's serverless warehouse was likely waking "
                                   "from idle - ask again in about a minute")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"redshift statement {sid} still "
                                   f"{desc['Status']} after {self._timeout:.0f}s")
            time.sleep(self._poll)
        if desc["Status"] != "FINISHED":
            raise RuntimeError(f"redshift statement {desc['Status']}: "
                               f"{desc.get('Error', 'no error detail')}")
        # #719: a statement that FINISHED explains no timeout - a hint set mid-poll must
        # not survive to label some later, unrelated failure - and a finish marks the
        # engine warm for the idleness discriminator.
        self._cold_hint = ""
        self._last_finished = time.monotonic()
        if not desc.get("HasResultSet"):
            return {"ColumnMetadata": [], "Records": []}
        return client.get_statement_result(Id=sid)

    def schema(self) -> list:
        if self._schema_cache is not None:   # one introspection per engine lifetime
            return self._schema_cache
        res = self._run(_SCHEMA_SQL, credential=self._introspect_credential)
        out: list = []
        for rec in res["Records"]:
            tschema, table, column, dtype = (_decode(f) for f in rec)
            qualified = f"{tschema}.{table}"
            if not self._allowed(qualified, table):
                continue
            if not out or out[-1]["table"] != qualified:
                out.append({"table": qualified, "columns": []})
            out[-1]["columns"].append({"name": column, "type": dtype})
        # #727: never cache an EMPTY schema. Zero tables is an error state (privileges, a
        # bare allowlist entry, HasResultSet=false), and caching it made the store decline
        # forever - a fixed GRANT then needed a recompose to be seen. A populated schema
        # still caches once per engine lifetime.
        if out:
            self._schema_cache = out
        return out

    def _allowed(self, qualified: str, bare: str) -> bool:
        """A bare allowlist entry means the DEFAULT schema only — it must not also match a
        same-named table in another schema, which is a different table (LAW 2)."""
        if not self._allow:
            return True
        if qualified.lower() in self._allow:
            return True
        return (bare.lower() in self._allow
                and qualified.lower() == f"{_DEFAULT_SCHEMA}.{bare}".lower())

    def execute(self, sql: str, credential: "str | None" = None,
                principal: "str | None" = None) -> tuple:
        res = self._run(sql, credential=credential, wake=False)
        cols = [c.get("name") or c.get("label", "") for c in res["ColumnMetadata"]]
        rows = [tuple(_decode(f) for f in rec) for rec in res["Records"]]
        return cols, rows

    def refresh_schema(self) -> None:
        self._schema_cache = None


class RedshiftProvider(StoreProviderPort):
    """config: workgroup + database (+ region, + optional `tables` allowlist bounding
    introspection AND the guard's visible schema). Auth = ambient AWS creds (SigV4).

    ADR 0024 + ADR 0022: a store that declares `delegation: {kind: aws_keys}` is
    introspected AND queried as the caller - `probe_as`/`build_as` take that caller's
    STS triple, minted from their own vaulted access keys. The ambient server identity
    is used only where no delegation is declared (the self-host topology)."""

    kind = "redshift"
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
        self._engine_factory = engine_factory or RedshiftEngine.from_config

    def _make(self, config: dict, credential: "str | None" = None) -> FederatedSqlStore:
        authorizer = self._authorizer
        if authorizer is None and self._broker is not None:
            # bind this store's authorize() to the broker (ADR 0006 precedence:
            # delegation -> row policy -> principals), keyed by the store id
            sid = config["id"]
            authorizer = lambda u, _s=sid: self._broker.access_for(u, _s)  # noqa: E731
        engine = self._engine_factory(config)
        # ADR 0022. `hasattr` rather than isinstance, same as BigQueryProvider: tests
        # inject fixture engines through engine_factory, and those keep introspecting
        # however they already do.
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

    def probe(self, config: dict) -> StoreProfile:
        return self._make(config).profile()

    def probe_as(self, config: dict, credential: "str | None" = None) -> StoreProfile:
        """ADR 0022: introspect as the caller when the store declares a delegation.
        Same optional-capability idiom as BigQueryProvider - credential=None is exactly
        probe()."""
        return self._make(config, credential=credential).profile()

    def build(self, config: dict) -> FederatedSqlStore:
        return self._make(config)

    def build_as(self, config: dict, credential: "str | None" = None) -> FederatedSqlStore:
        """ADR 0022's second half: probe and build produce SEPARATE engines, and retrieve
        re-reads the schema off the BUILT one - credentialing only the probe engine moves
        the wall rather than removing it (the #665 lesson: enumerate every call path)."""
        return self._make(config, credential=credential)
