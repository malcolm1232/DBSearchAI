"""`kind: bigquery` — Google BigQuery as a federated pushdown store (#107, ADR 0007).

A SqlEnginePort sibling of AzureSqlEngine: the SQL executes INSIDE BigQuery and only
the result set comes back. Guard/audit/NL2SQL/row-policy stay in FederatedSqlStore.

LAW 7: google-cloud-bigquery imports lazily inside the client factory — the optional
dep is only needed when a manifest actually composes a bigquery store. Two identities:
the SERVER's ADC (LAW 1) introspects schema (metadata only), and - when the store declares
`delegation: {kind: google_refresh}` - every user QUERY runs through a client built from
that user's delegated Google token (#193), so BigQuery's IAM and row-access policies decide
what comes back. A delegated store never falls back to ADC for a user query: an unlinked
Google account raises NotSignedIn and the ask is dropped and disclosed (LAW 2).

Introspection and generated SQL are scoped to ONE configured dataset: schema() reads
`<dataset>.INFORMATION_SCHEMA.COLUMNS`, and every query runs with that dataset as the
job's default, so the NL2SQL's unqualified table names resolve inside it and the
visible-schema guard keeps working unchanged.
"""
from __future__ import annotations

from typing import Callable, Optional

from dbsearch.router.provider import StoreProviderPort
from dbsearch.router.store import StoreProfile
from dbsearch.router.structured import (
    Authorizer, FederatedSqlStore, SqlEnginePort, SqlGenerator,
)


class BigQueryEngine(SqlEnginePort):
    dialect = "BigQuery Standard SQL"   # #714

    def __init__(self, client_factory: Callable[[], object], project: str, dataset: str,
                 tables: Optional[list] = None,
                 user_client_factory: Optional[Callable[[str], object]] = None) -> None:
        self._client_factory = client_factory
        self._project = project
        self._dataset = dataset
        self._allow = {t.lower() for t in (tables or [])}
        self._client = None
        self._schema_cache = None
        self._user_client_factory = user_client_factory or self._default_user_client
        self._user_clients: dict = {}
        # ADR 0022: whose credential reads INFORMATION_SCHEMA. None = the server's ADC, which
        # is right for an operator-owned warehouse and impossible for a user's own project.
        self._introspect_credential: "str | None" = None

    def introspect_as(self, credential: str) -> None:
        """Read the schema with this delegated credential instead of ADC (ADR 0022).

        Set per caller, so the profile this engine yields is the one THAT user is entitled
        to - which is why the cache below is dropped when the credential changes. Two
        callers legitimately see two schemas for one store id."""
        if credential != self._introspect_credential:
            self._schema_cache = None
        self._introspect_credential = credential

    def _default_user_client(self, credential: str):
        # ADR 0006 (#193): the user's delegated token becomes the client identity - BigQuery
        # IAM + row-access policies then enforce source-side. The quota project MUST be pinned:
        # a user token (especially a personal Google account) carries no billing project of its
        # own, and the job otherwise 403s with a userProject error.
        from google.api_core.client_options import ClientOptions
        from google.cloud import bigquery
        from google.oauth2.credentials import Credentials

        return bigquery.Client(
            project=self._project,
            credentials=Credentials(token=credential),
            client_options=ClientOptions(quota_project_id=self._project))

    @classmethod
    def from_config(cls, config: dict) -> "BigQueryEngine":
        missing = [k for k in ("project", "dataset") if not config.get(k)]
        if missing:
            raise ValueError(f"bigquery config missing {missing}")

        def make_client():
            from google.cloud import bigquery   # lazy optional dep (LAW 7)

            try:
                return bigquery.Client(project=config["project"])
            except Exception as exc:                       # google.auth DefaultCredentialsError
                # #660: name the control the user can actually reach. Google's own message says
                # "Your default credentials were not found" and links to the ADC setup docs -
                # advice to configure a machine identity on a server they do not own, for a
                # problem whose real fix is one field on the panel in front of them. Worse than
                # no message: it sends them somewhere they cannot go.
                #
                # Deliberately not a silent fallback to the delegated path. The store said
                # require_signin: no, which means "run as the server", and quietly running as
                # the USER instead would be a different identity than the one configured -
                # exactly the substitution LAW 2 forbids. Refuse, and say which switch to flip.
                if "default credentials" not in str(exc).lower():
                    raise
                raise RuntimeError(
                    "this deployment has no server-side Google credentials, so a BigQuery "
                    "store cannot run as the server. Set require_signin: yes to query as "
                    "your own linked Google account instead") from exc

        return cls(make_client, project=config["project"], dataset=config["dataset"],
                   tables=config.get("tables"))

    def _query(self, sql: str, scoped: bool = False, credential: "str | None" = None):
        if credential is not None:
            client = self._user_clients.get(credential)
            if client is None:
                client = self._user_clients[credential] = \
                    self._user_client_factory(credential)
        else:
            if self._client is None:
                self._client = self._client_factory()
            client = self._client
        job_config = self._job_config() if scoped else None
        return client.query(sql, job_config=job_config).result()

    def _job_config(self):
        try:
            from google.cloud import bigquery
        except ImportError:                     # fake-client tests without the SDK
            return None
        return bigquery.QueryJobConfig(
            default_dataset=f"{self._project}.{self._dataset}")

    def schema(self) -> list:
        if self._schema_cache is not None:   # one introspection per engine lifetime
            return self._schema_cache
        rows = self._query(
            "SELECT table_name, column_name, data_type "
            f"FROM `{self._project}`.{self._dataset}.INFORMATION_SCHEMA.COLUMNS "
            "ORDER BY table_name, ordinal_position",
            credential=self._introspect_credential)
        out: list = []
        for r in rows:
            table, column, dtype = r[0], r[1], r[2]
            if not self._allowed(table):
                continue
            if not out or out[-1]["table"] != table:
                out.append({"table": table, "columns": []})
            out[-1]["columns"].append({"name": column, "type": dtype})
        # #727: never cache an EMPTY schema - same contract as RedshiftEngine.schema().
        if out:
            self._schema_cache = out
        return out

    def _allowed(self, table: str) -> bool:
        """#808, the INVERSE of redshift's trap. `INFORMATION_SCHEMA.table_name` is bare, and
        this engine is already scoped to ONE dataset, so a bare entry is unambiguous here and
        stays exact. What was broken is the honest form: an operator who wrote
        `analytics.orders` - the shape redshift REQUIRES, and the shape #727's own remedy
        tells people to use - matched nothing and silently emptied the store.

        So accept the qualified forms too, but only when they name THIS engine's dataset and
        project. `other_dataset.orders` still matches nothing, because it genuinely refers to
        a table this engine cannot see - the same LAW 2 reasoning that keeps a bare redshift
        entry out of a non-default schema.
        """
        if not self._allow:
            return True
        t = table.lower()
        return (t in self._allow
                or f"{self._dataset}.{t}".lower() in self._allow
                or f"{self._project}.{self._dataset}.{t}".lower() in self._allow)

    def execute(self, sql: str, credential: "str | None" = None,
                principal: "str | None" = None) -> tuple:
        result = self._query(sql, scoped=True, credential=credential)
        cols = [f.name for f in result.schema]
        rows = [tuple(r.values()) if hasattr(r, "values") else tuple(r) for r in result]
        return cols, rows

    def refresh_schema(self) -> None:
        self._schema_cache = None


class BigQueryProvider(StoreProviderPort):
    """config: project + dataset (+ optional `tables` allowlist bounding introspection
    AND the guard's visible schema).

    ADR 0022: a store that declares a `delegation:` block is introspected AND queried as the
    caller - `probe_as` takes that caller's delegated credential. Server-side ADC is used only
    where no delegation is declared, i.e. the operator-owned-warehouse topology.

    It used to be ADC for schema unconditionally, which made the bring-your-own-Google case
    impossible rather than merely awkward: a hosted deployment has no ADC, and could not hold
    a meaningful one for a user's personal GCP project. The user linked Google, consented the
    bigquery scope, configured the store, and still got 'Your default credentials were not
    found' - a wall behind an offer they had done everything right to reach."""

    kind = "bigquery"
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
        self._engine_factory = engine_factory or BigQueryEngine.from_config

    def _make(self, config: dict, credential: "str | None" = None) -> FederatedSqlStore:
        authorizer = self._authorizer
        if authorizer is None and self._broker is not None:
            # bind this store's authorize() to the broker (ADR 0006 precedence:
            # delegation -> row policy -> principals), keyed by the store id
            sid = config["id"]
            authorizer = lambda u, _s=sid: self._broker.access_for(u, _s)  # noqa: E731
        engine = self._engine_factory(config)
        # ADR 0022. `hasattr` rather than isinstance: the demo scope and several tests inject
        # a fixture/fake engine through engine_factory, and those must keep introspecting
        # however they already do. A credential is only ever HANDED to an engine that asked
        # to be able to receive one.
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

        The optional-capability idiom health.py already uses for `build_isolated` - adding a
        parameter to StoreProviderPort.probe would touch eleven implementers, ten of which do
        not want it. `credential=None` is exactly `probe`, so a caller that cannot mint one
        (no delegation declared) loses nothing."""
        return self._make(config, credential=credential).profile()

    def build(self, config: dict) -> FederatedSqlStore:
        return self._make(config)

    def build_as(self, config: dict, credential: "str | None" = None) -> FederatedSqlStore:
        """ADR 0022, and the half `probe_as` alone does not cover.

        probe and build produce SEPARATE engines, and `retrieve` re-reads the schema off the
        BUILT one (FederatedSqlStore.described_schema -> engine.schema()). Credentialing only
        the probe engine therefore moved the wall rather than removing it: the health check
        read the schema as the caller and then failed on ADC 8ms into the round-trip, which
        reads to a user as a broken store rather than an unwired check."""
        return self._make(config, credential=credential)
