"""`kind: cosmos_db` — Azure Cosmos DB (Core / NoSQL API) as a federated DOCUMENT store
(#160, ADR 0007/0008). This is NOT on the SQL-pushdown rail: Cosmos is schemaless JSON
documents, so it has its OWN StorePort (CosmosStore, kind FEDERATED_DOC) rather than reusing
FederatedSqlStore. Same PRINCIPLE though — the query runs INSIDE Cosmos (pushdown, LAW 1) and
only the matching items come back as RECORD evidence; the store is never copied into an index.

The query is Cosmos DB SQL — a JSON-shaped SQL subset (`SELECT c.field FROM c WHERE ...`,
aggregates + GROUP BY). NL2query is a SEAM (`cosmos_generator(question, schema) -> query`),
mirroring structured.py: the default `keyword_cosmos_query` is deterministic and honest about
being naive (COUNT/SUM 'by X' -> GROUP BY, else SELECT *). A read-only guard runs before every
query (single SELECT over the container alias only). LAW 7: azure-cosmos imports lazily.

Permissions (LAW 2): store-level ACL trims whether the caller sees the store at all (gate #1,
via the broker/authorizer); a per-item row_policy predicate can further trim the result set.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable, Optional

from dbsearch.router.structured import CannotAnswerFromSchema
from dbsearch.router.evidence import Evidence, RECORD
from dbsearch.router.provider import StoreProviderPort
from dbsearch.router.store import (
    ANALYTICAL, AccessContext, EXACT, FEDERATED_DOC, StorePort, StoreProfile,
)

CosmosGenerator = Callable[[str, list], str]      # (question, schema) -> cosmos SQL


def _token_exp(token: str) -> int:
    """Best-effort `exp` (unix seconds) from a JWT so the Cosmos SDK's TokenCredential reports a
    real expiry; falls back to 0 (treated as already-stale, forcing the SDK to request fresh — but
    the token IS the credential here, so a small floor keeps the single delegated query working)."""
    import base64
    import json

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0

_FORBIDDEN = re.compile(r"\b(insert|update|delete|replace|upsert|merge|create|drop)\b", re.I)
# shape of a generated grouped-aggregate query, for the cross-partition rollup fallback:
#   SELECT c.<group>, <AGG>(c.<col> | 1) AS <label> FROM c GROUP BY c.<group>
_GROUPBY = re.compile(
    r"SELECT\s+c\.(\w+)\s*,\s*(COUNT|SUM|AVG)\(\s*(?:c\.(\w+)|1)\s*\)\s+AS\s+(\w+)"
    r"\s+FROM\s+c\s+GROUP\s+BY\s+c\.\1\s*$", re.I)


def validate_cosmos_query(query: str) -> None:
    """Read-only, single-statement guard. Cosmos' query API is read-only by construction,
    but we defend in depth (esp. once an LLM generates the query)."""
    s = query.strip()
    if ";" in s.rstrip(";"):
        raise ValueError("multiple statements are not allowed")
    if not s.lower().startswith("select"):
        raise ValueError("only SELECT queries are allowed")
    if _FORBIDDEN.search(s):
        raise ValueError("write keywords are not allowed")


def keyword_cosmos_query(question: str, schema: list, top_k: int = 20) -> str:
    """Deterministic naive NL2query over the container alias `c` — the demo default, loudly
    not a real generator (an LLM Cosmos generator is the follow-up, same seam)."""
    q = question.lower()
    fields = [f["name"] for f in (schema[0]["fields"] if schema else [])]

    def mentioned(cands):
        for c in cands:
            if re.search(r"\b" + re.escape(c.lower()) + r"\b", q):
                return c
        return None

    by = re.search(r"\bby\s+(\w+)", q)
    group = by.group(1) if by and by.group(1) in [f.lower() for f in fields] else None
    group = next((f for f in fields if f.lower() == group), None) if group else None

    if re.search(r"\b(how many|count|number of)\b", q):
        if group:
            return f"SELECT c.{group}, COUNT(1) AS count FROM c GROUP BY c.{group}"
        return "SELECT VALUE COUNT(1) FROM c"
    agg = "SUM" if re.search(r"\b(total|sum)\b", q) else \
          "AVG" if re.search(r"\b(average|avg|mean)\b", q) else None
    if agg:
        col = mentioned([f for f in fields if f != group])
        if group:
            col = col or next((f for f in fields if f != group),
                              fields[-1] if fields else "id")
            label = ("total_" if agg == "SUM" else "avg_") + col
            return f"SELECT c.{group}, {agg}(c.{col}) AS {label} FROM c GROUP BY c.{group}"
        if col:
            # #228: an UNGROUPED aggregate. Cosmos SQL supports it natively, and without this
            # branch the question fell through to `SELECT TOP k * FROM c` - a blind SAMPLE - so
            # the synthesizer averaged the handful of documents it was handed and stated THAT as
            # the population figure. Measured live: "what is the average customer review rating"
            # answered 4.2 out of 5 off 5 sampled docs; the true average over all 84 was 3.33.
            # A wrong number, cited, stated as fact - the #211 class.
            #
            # The field must be NAMED in the question. Guessing WHICH field to average (the way
            # the grouped branch above still falls back to "some other field") is that same
            # fabrication wearing a different hat, so an unnamed field falls through to the
            # sample below, where total_rows now discloses that it IS a sample.
            return f"SELECT VALUE {agg}(c.{col}) FROM c"
    return f"SELECT TOP {top_k} * FROM c"


def _strip_fence(text: str) -> str:
    """Model output -> bare query: drop ```sql fences and any language tag."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[A-Za-z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    return t.strip().rstrip(";").strip()


def llm_cosmos_generator(llm, fallback: "CosmosGenerator" = keyword_cosmos_query):
    """#229: a schema-grounded LLM NL2query for Cosmos, drop-in behind the SAME seam the naive
    `keyword_cosmos_query` sits behind - exactly as `llm_sql_generator` (#135) does for the SQL
    rail. Cosmos was the ONLY engine with no LLM seam: every SQL store got one, so Cosmos alone
    was answering production questions with a regex. That asymmetry is what produced #228 (an
    ungrouped aggregate fell through to `SELECT TOP 20 * FROM c` and the sample got averaged).

    The model only PROPOSES the query. This wrapper is deterministic about safety:
      - strips fences,
      - CANNOT_ANSWER (#211) is a RESULT, not a failure: it must NOT fall through to the keyword
        generator, because a broad `SELECT TOP k *` over "the most relevant container" is the
        same fabrication wearing different clothes - so it raises past the fallback and the
        store DECLINES,
      - validates with the SAME read-only guard the store enforces,
      - ANY other failure (empty, API error, an unsafe query) degrades to the keyword generator,
        so a store never errors or leaks on a bad generation.

    LAW 1: `schema` is field names and types only (cosmos's pseudo-schema keeps
    `type(v).__name__`, never a value), so no document content reaches the model.
    """
    def generate(question: str, schema: list) -> str:
        try:
            query = _strip_fence(llm.generate_cosmos_query(question, schema))
            if not query:
                raise ValueError("empty generation")
            if query.strip().upper().startswith("CANNOT_ANSWER"):
                raise CannotAnswerFromSchema(
                    "this container holds no data of the kind the question asks about")
            validate_cosmos_query(query)
            return query
        except CannotAnswerFromSchema:
            raise
        except Exception:
            return fallback(question, schema)

    return generate


_AGG_CALL = re.compile(r"\b(COUNT|AVG|SUM|MIN|MAX)\s*\(\s*(?:c\.)?([A-Za-z_]\w*|1|\*)\s*\)", re.I)


def _scalar_label(query: str) -> str:
    """#231: name a bare scalar result.

    `SELECT VALUE AVG(c.rating) FROM c` returns the naked number `3.33` - Cosmos's VALUE form
    strips the column name. The store used to hand the synthesizer `str(item)`, i.e. the string
    "2", with nothing saying WHAT it counts. The synthesizer, reasonably, replied "I don't have
    that information" - to a question the database had answered correctly. The SQL rail never hits
    this because SQL rows carry their column names (`total_amount=200`).

    Recover the label from the aggregate in the query, so the evidence reads `count=2` /
    `avg_rating=3.33`. Done at the STORE, so it holds for BOTH generators - the naive keyword one
    and the LLM (#229) - regardless of which form either emits.
    """
    m = _AGG_CALL.search(query or "")
    if not m:
        return "value"
    fn, arg = m.group(1).lower(), m.group(2)
    if fn == "count" or arg in ("1", "*"):
        return "count" if fn == "count" else fn
    return f"{fn}_{arg}"


class CosmosEngine:
    """Where the query runs: INSIDE a Cosmos container. Introspects a pseudo-schema by
    sampling documents (Cosmos is schemaless) and executes a Cosmos-SQL query."""

    def __init__(self, container_factory: Callable[[], object], container_name: str = "c",
                 sample: int = 20,
                 user_factory: "Optional[Callable[[str], object]]" = None) -> None:
        self._factory = container_factory
        self._name = container_name
        self._sample = sample
        self._container = None
        self._schema_cache = None
        # ADR 0006 (query-as-user, #191): a delegated ask opens its OWN container client bound to
        # the signed-in user's Entra token (Cosmos data-plane RBAC enforces source-side); the key
        # path (below) is only for the schema probe. No user_factory + a delegated ask => FAIL
        # CLOSED (LAW 2), never the shared account key.
        self._user_factory = user_factory
        self._user_containers: dict = {}     # token -> live container (ADR 0006)

    def _c(self):
        if self._container is None:
            self._container = self._factory()
        return self._container

    def _container_for(self, credential):
        """Non-delegated (schema probe / no-signin store) -> the key-based container. Delegated
        (a signed-in user's Cosmos token) -> a per-token container the user's data-plane role
        governs. A delegated ask on a store with no user_factory fails closed (LAW 2)."""
        if credential is None:
            return self._c()
        if self._user_factory is None:
            raise RuntimeError("cosmos_db delegated (query-as-user) path not configured for this "
                               "store — failing closed (LAW 2), never the account key")
        cont = self._user_containers.get(credential)
        if cont is None:
            cont = self._user_containers[credential] = self._user_factory(credential)
        return cont

    def schema(self) -> list:
        if self._schema_cache is not None:
            return self._schema_cache
        items = list(self._c().query_items(
            query=f"SELECT TOP {self._sample} * FROM c",
            enable_cross_partition_query=True))
        fields: dict = {}
        for it in items:
            if not isinstance(it, dict):
                continue                       # scalar sample (unusual) — nothing to infer
            for k, v in it.items():
                if k.startswith("_") or k == "id":   # skip Cosmos system fields
                    continue
                fields.setdefault(k, type(v).__name__)
        self._schema_cache = [{"container": self._name,
                               "fields": [{"name": k, "type": t} for k, t in fields.items()]}]
        return self._schema_cache

    def refresh_schema(self) -> None:
        self._schema_cache = None

    def query(self, cosmos_sql: str, credential: "str | None" = None) -> list:
        cont = self._container_for(credential)
        try:
            return list(cont.query_items(
                query=cosmos_sql, enable_cross_partition_query=True))
        except Exception as exc:
            # Cosmos quirk: a CROSS-PARTITION `GROUP BY` with a projected aggregate is rejected
            # ("Cross partition query only supports 'VALUE <AggregateFunc>'"). Fall back to a
            # pushed-down PROJECTION (still runs IN Cosmos — only the grouped/aggregated fields
            # come back, LAW 1) and roll up in-tenant. Any other error re-raises.
            m = _GROUPBY.match(cosmos_sql.strip())
            if m and "cross partition" in str(exc).lower():
                return self._rollup(m, cont)
            raise

    def _rollup(self, m, cont) -> list:
        group, agg = m.group(1), m.group(2).upper()
        col, label = m.group(3), m.group(4)
        proj = f"SELECT c.{group}" + (f", c.{col}" if col else "") + " FROM c"
        items = list(cont.query_items(query=proj, enable_cross_partition_query=True))
        buckets: dict = defaultdict(list)
        for it in items:
            buckets[it.get(group)].append(it.get(col) if col else 1)
        out = []
        for g, vals in buckets.items():
            nums = [v for v in vals if isinstance(v, (int, float))]
            if agg == "COUNT":
                v = len(vals)
            elif agg == "SUM":
                v = sum(nums)
            else:  # AVG
                v = (sum(nums) / len(nums)) if nums else 0
            out.append({group: g, label: v})
        return out


class CosmosStore(StorePort):
    def __init__(self, store_id: str, business_unit: str, title: str, description: str,
                 engine: CosmosEngine, *, generator: Optional[CosmosGenerator] = None,
                 authorizer=None, topics: Optional[list] = None) -> None:
        self._store_id = store_id
        self._bu = business_unit
        self._title = title
        self._description = description
        self._engine = engine
        self._gen = generator or keyword_cosmos_query
        self._authorizer = authorizer
        self._topics = topics or []
        self.audit_trail: list = []

    def profile(self) -> StoreProfile:
        return StoreProfile(store_id=self._store_id, title=self._title,
                            description=self._description, kind=FEDERATED_DOC,
                            capabilities={ANALYTICAL, EXACT}, business_unit=self._bu,
                            topics=list(self._topics), schema=self._engine.schema(),
                            freshness="live")

    def authorize(self, user_oid: str) -> AccessContext:
        if self._authorizer is not None:
            return self._authorizer(user_oid)
        return AccessContext(user_oid=user_oid, principals=[])

    def retrieve(self, access: AccessContext, question: str, top_k: int = 5) -> list:
        schema = self._engine.schema()
        query = self._gen(question, schema)
        validate_cosmos_query(query)
        self.audit_trail.append({"user": access.user_oid, "store": self._store_id,
                                 "query": query, "row_policy": access.row_policy or "",
                                 # LAW 8: record THAT delegation happened, never the credential
                                 "delegated": access.delegated_credential is not None})
        items = self._engine.query(query, credential=access.delegated_credential)
        # #206 parity, finally applied to Cosmos (#228). The query's TRUE row count travels with
        # the evidence. Without it the answer is built from `top_k` documents and NOTHING
        # downstream can tell 5-of-5 from 5-of-84 - so the synthesizer averaged five sampled
        # reviews and reported it as "the average customer review rating" (4.2, when the truth
        # over all 84 was 3.33). structured.py has carried this since #206; cosmos.py never did.
        total = len(items)
        out = []
        for i, item in enumerate(items[:top_k]):
            if isinstance(item, dict):
                clean = {k: v for k, v in item.items() if not k.startswith("_")}
                content = ", ".join(f"{k}={v}" for k, v in clean.items())
                item_id = clean.get("id", i)
            else:
                # scalar result (VALUE COUNT/AVG/SUM). Label it (#231) - a bare "2" tells the
                # synthesizer nothing, and it answers "I don't have that information".
                content = f"{_scalar_label(query)}={item}"
                item_id = i
            out.append(Evidence(store_id=self._store_id, business_unit=self._bu,
                                kind=RECORD, content=content,
                                provenance={"query": query,
                                            "container": self._engine._name,
                                            "item_ids": [item_id],
                                            "total_rows": total},
                                score=None))
        return out


class CosmosProvider(StoreProviderPort):
    """config: endpoint/key (${ENV} refs) + database + container. mode: pushdown (queried in
    place, never copied). NoSQL — its own FEDERATED_DOC store, not the SQL-pushdown rail."""

    kind = "cosmos_db"
    modes = ("pushdown",)

    def __init__(self, *, generator: Optional[CosmosGenerator] = None,
                 authorizer=None, engine_factory: Optional[Callable[[dict], CosmosEngine]] = None,
                 broker=None) -> None:
        self._gen = generator
        self._authorizer = authorizer
        self._broker = broker
        self._engine_factory = engine_factory or self._default_engine

    @staticmethod
    def _default_engine(config: dict) -> CosmosEngine:
        missing = [k for k in ("endpoint", "key", "database", "container")
                   if not config.get(k)]
        if missing:
            raise ValueError(f"cosmos_db config missing {missing} "
                             "(use ${ENV} refs in stores.yml — resolved server-side)")

        def _container(credential):
            from azure.cosmos import CosmosClient   # lazy: optional dep (LAW 7)

            client = CosmosClient(config["endpoint"], credential=credential)
            return client.get_database_client(config["database"]).get_container_client(
                config["container"])

        def factory():
            # schema-probe / no-signin path: the account key (a str credential).
            return _container(config["key"])

        def user_factory(token: str):
            # query-as-user path: the signed-in user's Cosmos AAD access token wrapped as a
            # TokenCredential; Cosmos data-plane RBAC then governs what THAT user can read (#191).
            from azure.core.credentials import AccessToken

            exp = _token_exp(token)

            class _StaticTokenCredential:
                def get_token(self, *scopes, **kwargs):
                    return AccessToken(token, exp)

            return _container(_StaticTokenCredential())

        return CosmosEngine(factory, container_name=config["container"],
                            user_factory=user_factory)

    def _make(self, config: dict) -> CosmosStore:
        authorizer = self._authorizer
        if authorizer is None and self._broker is not None:
            sid = config["id"]
            authorizer = lambda u, _s=sid: self._broker.access_for(u, _s)  # noqa: E731
        return CosmosStore(
            store_id=config["id"], business_unit=config.get("business_unit", ""),
            title=config.get("title", config["id"]),
            description=config.get("description", ""),
            engine=self._engine_factory(config), generator=self._gen,
            authorizer=authorizer, topics=config.get("topics") or [])

    def probe(self, config: dict) -> StoreProfile:
        return self._make(config).profile()

    def build(self, config: dict) -> CosmosStore:
        return self._make(config)
