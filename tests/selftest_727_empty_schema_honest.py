"""#727 - an EMPTY schema is a source-side fault, never "holds no data of this kind".

FOUND ON PROD (260813): a delegated Redshift store composed green and then declined every
question with the no-table-matches shape - the disclosure told the owner his freight store
holds no freight. The schema was EMPTY at ask time, and `_resolve_sql` cannot tell an empty
schema from an honest retrieval miss: both fall through `_subset_for` to the same
`CannotAnswerFromSchema("no table in this source matches the question")`, which the executor
maps to DECLINED - a claim about the DATA - when the truth was a claim about the SOURCE
(credential privileges, a `tables:` allowlist that matched nothing, or a swallowed
`HasResultSet: false`).

The diagnosis (260817) could not recover the original root cause - the rig was gone and the
container logs rotated - but every code path that produces the shape silently is real:
`information_schema` is privilege-filtered and returns zero rows WITHOUT an error; redshift's
`_run` returns `{[], []}` when `HasResultSet` is false and `schema()` CACHED that; a bare
`tables:` allowlist entry matches only the default schema, so a non-public table empties the
schema with no warning.

THE RULE: zero tables raises `SchemaUnavailable` - after ONE `refresh_schema()` retry, so a
transient empty (an expired STS session since repaired, a GRANT fixed after compose) recovers
without a recompose. The executor maps it to ERROR with the remedy as user-facing
instructions; the synthesizer renders the FAILED answer and the `To use <store>:` sentence -
never the DECLINED answer, and never "not connected", which is the unlinked-cloud phrasing
(#680) and would send the user to re-link a cloud that is already linked.

One raise home on purpose (the #799 lesson): `_resolve_sql` is shared by `retrieve` and
`retrieve_bound` and sits above every SQL engine, so redshift, bigquery, azure_sql, postgres,
mysql and synapse are all covered by the same clause. The engines' own change is only "never
CACHE an empty schema" so the retry can actually see a repaired source.

    PYTHONPATH=src python3 tests/selftest_727_empty_schema_honest.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.catalog import CatalogNode, StoreCatalog, STORE  # noqa: E402
from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402
from dbsearch.router import executor  # noqa: E402
from dbsearch.router.executor import StoreOutcome  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    CannotAnswerFromSchema, FederatedSqlStore, SchemaUnavailable, SqliteEngine)
from dbsearch.router.synthesizer import (  # noqa: E402
    DECLINED_ANSWER, FAILED_ANSWER, disclosure_from, no_evidence_answer)

QUESTION = "what is the freight cost per route"

TABLES = {"freight_costs": {
    "columns": ["route", "cost"],
    "rows": [["sin-hkg", 8000], ["sin-nrt", 15000], ["sin-syd", 12000]]}}


class _GatedSchemaEngine:
    """Delegates to a real SqliteEngine but reports an EMPTY schema until `recovers` fires.

    `recovers=None` never recovers (the persistent #727 condition); `recovers="on_refresh"`
    turns the schema on at the first refresh_schema() - the transient case (a GRANT fixed
    after compose) that the ONE retry must rescue without a recompose."""

    def __init__(self, recovers=None):
        self._inner = SqliteEngine.from_tables(TABLES)
        self._recovers = recovers
        self._empty = True
        self.refresh_calls = 0

    def schema(self):
        return [] if self._empty else self._inner.schema()

    def refresh_schema(self):
        self.refresh_calls += 1
        if self._recovers == "on_refresh":
            self._empty = False
        self._inner.refresh_schema()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _store(engine, sid="freight-costs"):
    return FederatedSqlStore(
        sid, "ops", "Freight", "freight shipping routes costs",
        engine,
        sql_generator=lambda *a, **k: "SELECT route, cost FROM freight_costs ORDER BY route")


def _decision(sid="freight-costs"):
    return RoutingDecision(query_type="analytical",
                           stores=[RoutedStore(store_id=sid, business_unit="ops", score=0.9)])


def _catalog(store, sid="freight-costs"):
    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind="tenant", parent_id=None, acl=["alice"]))
    cat.register(CatalogNode(id=sid, kind=STORE, parent_id="t", acl=["alice"],
                             profile=store.profile(), store=store))
    return cat


def test_an_empty_schema_raises_schema_unavailable_not_a_decline():
    """THE DEFECT: with schema [], retrieve declined - a claim about the data."""
    store = _store(_GatedSchemaEngine(recovers=None))
    try:
        store.retrieve(store.authorize("alice"), QUESTION)
    except SchemaUnavailable as exc:
        msg = str(exc)
        assert "0 tables" in msg, f"the error does not say what was measured: {msg!r}"
        assert "schema-qualified" in msg or "allowlist" in msg, (
            f"the error gives the user nothing to check: {msg!r}")
    except CannotAnswerFromSchema as exc:
        raise AssertionError(
            f"an empty schema still declines as if the store held no such data: {exc}")
    else:
        raise AssertionError("retrieve() answered from an empty schema")
    print("  PASS  empty schema raises SchemaUnavailable with an actionable message")


def test_one_refresh_rescues_a_transient_empty():
    """The retry clause ALONE: schema empty at first read, present after ONE refresh -
    the ask must succeed, and with exactly one refresh (never a loop)."""
    eng = _GatedSchemaEngine(recovers="on_refresh")
    store = _store(eng)
    rows = store.retrieve(store.authorize("alice"), QUESTION)
    assert rows, "a schema that recovered on refresh still produced no answer"
    assert eng.refresh_calls == 1, (
        f"expected exactly ONE refresh (never a loop); saw {eng.refresh_calls}")
    print("  PASS  one refresh rescues a transient empty schema")


def test_a_genuine_no_match_still_declines():
    """The control: a store with a real schema that honestly holds nothing of this kind
    must KEEP declining - the fix must not convert honest declines into errors."""
    store = FederatedSqlStore(
        "freight-costs", "ops", "Freight", "freight shipping routes costs",
        SqliteEngine.from_tables(TABLES),
        sql_generator=_raise_cannot)
    try:
        store.retrieve(store.authorize("alice"), QUESTION)
    except CannotAnswerFromSchema:
        pass
    else:
        raise AssertionError("the honest decline path stopped declining")
    print("  PASS  a genuine no-match still declines (control)")


def _raise_cannot(*a, **k):
    raise CannotAnswerFromSchema("this store holds nothing of that kind")


def test_executor_maps_it_to_error_with_the_remedy():
    """The outcome the disclosure will render: ERROR, remedy = the user's instructions."""
    store = _store(_GatedSchemaEngine(recovers=None))
    report = executor.execute(_catalog(store), _decision(), "alice", QUESTION)
    assert len(report.outcomes) == 1
    o = report.outcomes[0]
    assert o.status == executor.ERROR, (
        f"an empty schema reached the user as {o.status!r} - the #727 mislabel")
    assert o.remedy and ("allowlist" in o.remedy or "schema-qualified" in o.remedy), (
        f"the outcome carries no actionable remedy: remedy={o.remedy!r}")
    assert not o.unlinked, "a schema fault must not masquerade as an unlinked cloud"
    print("  PASS  executor maps SchemaUnavailable to ERROR + remedy")


def test_the_answer_and_disclosure_are_honest():
    """All the homes the reader meets (#799): the headline answer and the disclosure."""
    o = StoreOutcome(store_id="freight-costs", business_unit="ops", status=executor.ERROR,
                     error="SchemaUnavailable: introspection returned 0 tables",
                     remedy="Check the delegated credential's privileges on the source, and "
                            "that any tables: allowlist entries are schema-qualified")
    answer = no_evidence_answer(_decision(), [o])
    assert answer == FAILED_ANSWER, (
        f"an all-schema-fault ask renders {answer!r} instead of the failed answer")
    assert answer != DECLINED_ANSWER
    d = disclosure_from([o])
    assert "holds no data of this kind" not in d, (
        f"the disclosure still tells the user their store holds no such data: {d!r}")
    assert "not connected" not in d, (
        f"a schema fault renders as an unlinked cloud ('not connected'): {d!r}")
    assert "To use freight-costs:" in d, f"the remedy sentence is missing: {d!r}"
    print("  PASS  answer + disclosure render the fault honestly")


def test_an_unlinked_cloud_still_says_not_connected():
    """The control for the disclosure change: the #680 unlinked-cloud drop KEEPS its
    'not connected' phrasing - that wording is correct exactly there."""
    o = StoreOutcome(store_id="redshift-1", business_unit="ops", status=executor.ERROR,
                     error="NotSignedIn: connect Amazon",
                     remedy="Connect Amazon from the account menu", unlinked=True)
    d = disclosure_from([o])
    assert "not connected" in d, (
        f"the unlinked-cloud drop lost its 'not connected' phrasing: {d!r}")
    print("  PASS  an unlinked cloud still renders 'not connected' (control)")


def test_engines_never_cache_an_empty_schema():
    """redshift + bigquery: an empty introspection must NOT be cached, or the retry (and
    every later ask) reads the cached [] forever and a fixed GRANT needs a recompose."""
    from dbsearch.router.providers.redshift import RedshiftEngine

    class _Api:
        def __init__(self, payloads):
            self.payloads = list(payloads)
            self.executed = []

        def execute_statement(self, **kw):
            self.executed.append(kw["Sql"])
            return {"Id": f"s{len(self.executed)}"}

        def describe_statement(self, Id):
            return {"Status": "FINISHED", "HasResultSet": bool(self._current())}

        def get_statement_result(self, Id):
            return self._current()

        def _current(self):
            return self.payloads[min(len(self.executed) - 1, len(self.payloads) - 1)]

    def _rows(*cols):
        return {"ColumnMetadata": [{}] * 4,
                "Records": [[{"stringValue": "public"}, {"stringValue": "freight_costs"},
                             {"stringValue": c}, {"stringValue": "integer"}] for c in cols]}

    # empty first (HasResultSet False), populated second - the second schema() call must
    # re-execute rather than serve the cached []
    api = _Api([{"ColumnMetadata": [], "Records": []}, _rows("route", "cost")])
    eng = RedshiftEngine(client_factory=lambda: api, workgroup="wg", database="dev",
                         tables=["freight_costs"], poll_interval=0, timeout=5)
    first = eng.schema()
    assert first == [], f"expected the honest empty read first, got {first!r}"
    second = eng.schema()
    assert len(api.executed) == 2, (
        f"the empty schema was CACHED - the second schema() call never re-introspected "
        f"(executed={len(api.executed)})")
    assert second, "the repaired source still reads empty on the second call"

    # and a POPULATED schema is still cached (the control - do not regress the cache)
    api2 = _Api([_rows("route", "cost")])
    eng2 = RedshiftEngine(client_factory=lambda: api2, workgroup="wg", database="dev",
                          tables=["freight_costs"], poll_interval=0, timeout=5)
    eng2.schema(); eng2.schema()
    assert len(api2.executed) == 1, "a populated schema stopped being cached"
    print("  PASS  redshift never caches an empty schema (and still caches a real one)")

    # bigquery: the same rule, proven behaviorally, not by reading the source
    from dbsearch.router.providers.bigquery import BigQueryEngine

    class _BqClient:
        def __init__(self, payloads):
            self.payloads = list(payloads)
            self.queries = []

        def query(self, sql, job_config=None):
            self.queries.append(sql)
            rows = self.payloads[min(len(self.queries) - 1, len(self.payloads) - 1)]

            class _Job:
                def result(_self):
                    return rows
            return _Job()

    bq = _BqClient([[], [("freight_costs", "route", "STRING"),
                         ("freight_costs", "cost", "INT64")]])
    beng = BigQueryEngine(lambda: bq, project="p", dataset="d")
    assert beng.schema() == [], "expected the honest empty read first"
    assert beng.schema(), "the repaired source still reads empty on the second call"
    assert len(bq.queries) == 2, (
        f"bigquery CACHED the empty schema - second schema() never re-introspected "
        f"(queries={len(bq.queries)})")
    bq2 = _BqClient([[("freight_costs", "route", "STRING")]])
    beng2 = BigQueryEngine(lambda: bq2, project="p", dataset="d")
    beng2.schema(); beng2.schema()
    assert len(bq2.queries) == 1, "a populated bigquery schema stopped being cached"
    print("  PASS  bigquery never caches an empty schema (and still caches a real one)")

    # #807: THE SAME CONTRACT, ON THE REST OF THE RAIL. #727 fixed only the two engines its
    # prod incident happened to involve; azure_sql, postgres and mysql cached an empty schema
    # exactly as redshift had, so the identical defect - a fixed GRANT needing a recompose to
    # be seen - was still live on three engines. Synapse subclasses AzureSqlProvider and
    # reuses AzureSqlEngine verbatim, so it has no engine of its own and is covered here.
    # Table-driven on purpose: this is ONE rule with one home, and a new engine that forgets
    # it should be a line added here rather than a defect found on prod (#799).
    from dbsearch.router.providers.azure_sql import AzureSqlEngine
    from dbsearch.router.providers.mysql import MySqlEngine
    from dbsearch.router.providers.postgres import PostgresEngine

    class _Cur:
        """One cursor per connection, counting the introspections it is actually asked for."""

        def __init__(self, reads):
            self._reads = reads
            self.count = 0

        def execute(self, *a, **k):
            self.count += 1

        def fetchall(self):
            return self._reads[min(self.count - 1, len(self._reads) - 1)]

        def close(self):
            pass

    class _Conn:
        def __init__(self, reads):
            self.cur = _Cur(reads)

        def cursor(self):
            return self.cur

    PG_ROW = [("public", "freight_costs", "route", "text")]      # schema-qualified rails
    MY_ROW = [("freight_costs", "route", "text")]                # mysql has no schema column
    for label, factory, populated in (
            ("azure_sql (and synapse, which reuses it)", AzureSqlEngine, PG_ROW),
            ("postgres", PostgresEngine, PG_ROW),
            ("mysql", MySqlEngine, MY_ROW)):
        conn = _Conn([[], populated])                 # empty first, repaired second
        eng = factory(connect=lambda c=conn: c)
        assert eng.schema() == [], f"{label}: expected the honest empty read first"
        assert eng.schema(), (
            f"#807 {label}: the repaired source still reads empty on the second call - the "
            f"empty schema was CACHED, so a fixed GRANT needs a recompose to be seen")
        assert conn.cur.count == 2, (
            f"#807 {label}: cached the empty schema - the second schema() never "
            f"re-introspected (introspections={conn.cur.count})")

        ctl = _Conn([populated])                      # and a real schema STILL caches
        e2 = factory(connect=lambda c=ctl: c)
        e2.schema(); e2.schema()
        assert ctl.cur.count == 1, (
            f"{label}: a populated schema stopped being cached - every ask now pays a cloud "
            f"round trip for introspection")
        print(f"  PASS  {label} never caches an empty schema (and still caches a real one)")


def test_end_to_end_delegated_redshift_empty_schema_over_http():
    """The whole prod chain, hermetically: a delegated redshift store COMPOSES GREEN over an
    empty schema (that silence is the deceit #727 shipped), and the first /ask must come
    back as an honest source fault - never 'holds no data of this kind'."""
    import json as _json
    import os
    import types

    os.environ["SELFHOST_BACKEND"] = "memory"

    class _Sts:
        def get_session_token(self, DurationSeconds=None):
            return {"Credentials": {"AccessKeyId": "ASIA-stub", "SecretAccessKey": "s",
                                    "SessionToken": "t"}}

    class _Data:
        """Every statement finishes with no result set - the introspection shape that
        redshift's `_run` used to swallow into a cached empty schema."""

        def __init__(self):
            self.executed = []

        def execute_statement(self, **kw):
            self.executed.append(kw.get("Sql", ""))
            return {"Id": f"s{len(self.executed)}"}

        def describe_statement(self, Id):
            return {"Status": "FINISHED", "HasResultSet": False}

        def get_statement_result(self, Id):
            return {"ColumnMetadata": [], "Records": []}

    stub = types.ModuleType("boto3")
    data = _Data()
    stub.client = lambda service, **kw: _Sts() if service == "sts" else data
    saved_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = stub
    try:
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from dbsearch.server.edition import build_edition
        from dbsearch.server import router_api

        def current_user(request: Request) -> str:
            return request.headers["X-Test-User"]

        app = FastAPI()
        app.include_router(router_api.build_router_api(
            build_edition(), current_user, manifest_store=None,
            force_per_user_workspaces=True,
            subject_token_provider=lambda oid, idp="aws": _json.dumps(
                {"access_key_id": "AKIA-stub", "secret_access_key": "s"})))
        client = TestClient(app)
        hdr = {"X-Test-User": "oid-a"}
        man = {"tenant": "acme", "stores": [
            {"id": "freight-costs", "kind": "redshift", "business_unit": "ops",
             "acl": ["oid-a"],
             "config": {"workgroup": "wg", "database": "dev", "region": "ap-southeast-1",
                        "tables": ["freight_costs"]},
             "delegation": {"kind": "aws_keys", "resource": "redshift"}}]}
        rc = client.post("/router/compose", json={"manifest": man}, headers=hdr)
        assert rc.status_code == 200, rc.text
        assert any(s["store_id"] == "freight-costs" for s in rc.json()["stores"]), (
            f"the store must compose GREEN over the empty schema - that silence is the "
            f"deceit under test: {rc.text[:300]}")
        ra = client.post("/router/ask", json={"question": "what is the freight cost per "
                                                          "route"}, headers=hdr)
        assert ra.status_code == 200, ra.text
        body = ra.json()
        o = next((x for x in body.get("outcomes", [])
                  if x["store_id"] == "freight-costs"), None)
        assert o is not None, f"no outcome for the store at all: {body.get('outcomes')}"
        assert o["status"] == "error", (
            f"the empty-schema ask reached the wire as {o['status']!r} - the #727 mislabel "
            f"survives the real HTTP path")
        assert "SchemaUnavailable" in (o.get("error") or ""), o
        assert "holds no data of this kind" not in _json.dumps(body), (
            "the response still tells the user their store holds no such data")
    finally:
        if saved_boto3 is not None:
            sys.modules["boto3"] = saved_boto3
        else:
            sys.modules.pop("boto3", None)
    print("  PASS  the whole HTTP chain renders the empty schema as a source fault")


if __name__ == "__main__":
    test_an_empty_schema_raises_schema_unavailable_not_a_decline()
    test_one_refresh_rescues_a_transient_empty()
    test_a_genuine_no_match_still_declines()
    test_executor_maps_it_to_error_with_the_remedy()
    test_the_answer_and_disclosure_are_honest()
    test_an_unlinked_cloud_still_says_not_connected()
    test_engines_never_cache_an_empty_schema()
    test_end_to_end_delegated_redshift_empty_schema_over_http()
    print("\nEMPTY SCHEMA HONESTY SELF-TEST PASSED.")
