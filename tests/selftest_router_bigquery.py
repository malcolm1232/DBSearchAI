"""#107 (bigquery slice) — BigQueryEngine + BigQueryProvider: BigQuery as a pushdown
SqlEnginePort sibling (ADR 0007), proven WITHOUT network or the google-cloud SDK
(a fake client is injected). Guard/audit/NL2SQL stay in FederatedSqlStore.

Run: python3 tests/selftest_router_bigquery.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router import BigQueryEngine, BigQueryProvider  # noqa: E402
from dbsearch.router.store import FEDERATED_SQL  # noqa: E402


_SCHEMA_ROWS = [
    ("notes", "body", "STRING"),
    ("sales", "id", "INT64"),
    ("sales", "region", "STRING"),
    ("sales", "amount", "FLOAT64"),
]
_DATA_COLS = ["region", "total_amount"]
_DATA_ROWS = [("apac", 205000.0), ("emea", 125000.0)]


class _FakeResult:
    def __init__(self, cols, rows):
        self.schema = [SimpleNamespace(name=c) for c in cols]
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeJob:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


class _FakeClient:
    def __init__(self):
        self.calls = []

    def query(self, sql, job_config=None):
        self.calls.append({"sql": sql, "job_config": job_config})
        if "INFORMATION_SCHEMA" in sql:
            return _FakeJob(_FakeResult(["table_name", "column_name", "data_type"],
                                        list(_SCHEMA_ROWS)))
        return _FakeJob(_FakeResult(_DATA_COLS, list(_DATA_ROWS)))


def _eng(tables=None):
    client = _FakeClient()
    return BigQueryEngine(lambda: client, project="p", dataset="dbsearch_demo",
                          tables=tables), client


def test_schema_groups_and_scopes_to_dataset():
    eng, client = _eng()
    schema = eng.schema()
    assert [t["table"] for t in schema] == ["notes", "sales"], schema
    sales = next(t for t in schema if t["table"] == "sales")
    assert sales["columns"][1] == {"name": "region", "type": "STRING"}, sales
    # introspection reads the DATASET's INFORMATION_SCHEMA, not the whole project
    assert "dbsearch_demo.INFORMATION_SCHEMA.COLUMNS" in client.calls[0]["sql"], client.calls


def test_schema_honors_table_allowlist():
    eng, _ = _eng(tables=["SALES"])
    assert [t["table"] for t in eng.schema()] == ["sales"]


def test_schema_is_cached_per_engine():
    eng, client = _eng()
    eng.schema(); eng.schema()
    assert len(client.calls) == 1, client.calls


def test_execute_returns_cols_rows_with_default_dataset():
    eng, client = _eng()
    cols, rows = eng.execute("SELECT region, SUM(amount) AS total_amount FROM sales "
                             "GROUP BY region")
    assert cols == _DATA_COLS and rows == _DATA_ROWS, (cols, rows)
    # unqualified table names must resolve inside the configured dataset — the
    # generated SQL says `FROM sales`, so the job must carry a default dataset
    jc = client.calls[-1]["job_config"]
    assert jc is not None and "dbsearch_demo" in str(jc.default_dataset), jc


def test_from_config_requires_project_and_dataset():
    try:
        BigQueryEngine.from_config({"project": "p"})
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "dataset" in str(e), e


def test_provider_builds_federated_store():
    assert BigQueryProvider.kind == "bigquery"
    assert tuple(BigQueryProvider.modes) == ("pushdown",)
    prov = BigQueryProvider(engine_factory=lambda cfg: _eng(cfg.get("tables"))[0])
    store = prov.build({"id": "sales-bq", "business_unit": "sales",
                        "description": "revenue", "tables": ["sales"]})
    p = store.profile()
    assert p.kind == FEDERATED_SQL and [t["table"] for t in p.schema] == ["sales"], p


def test_delegated_execute_uses_the_user_client_not_adc():
    """ADR 0006: with a delegated credential the query must run through a client built from
    the USER's token. The service/ADC client must never serve a user query."""
    seen = {"service": 0, "user": []}

    class FakeResult:
        schema = [type("F", (), {"name": "region"})()]

        def __iter__(self):
            return iter([("emea",)])

    class FakeClient:
        def __init__(self, tag):
            self.tag = tag

        def query(self, sql, job_config=None):
            seen["user"].append(self.tag)
            return type("Job", (), {"result": lambda _self: FakeResult()})()

    def service_factory():
        seen["service"] += 1
        return FakeClient("service")

    eng = BigQueryEngine(service_factory, project="p", dataset="d",
                         user_client_factory=lambda cred: FakeClient("user:" + cred))
    cols, rows = eng.execute("SELECT region FROM sales", credential="at-alice")
    assert cols == ["region"] and rows == [("emea",)]
    assert seen["user"] == ["user:at-alice"]     # ran as alice
    assert seen["service"] == 0                  # ADC client never even constructed


def test_delegated_clients_are_isolated_per_credential():
    made = []

    class FakeClient:
        def query(self, sql, job_config=None):
            return type("Job", (), {
                "result": lambda _self: type("R", (), {"schema": [], "__iter__": lambda s: iter([])})()
            })()

    def user_factory(cred):
        made.append(cred)
        return FakeClient()

    eng = BigQueryEngine(lambda: FakeClient(), project="p", dataset="d",
                         user_client_factory=user_factory)
    eng.execute("SELECT 1", credential="at-alice")
    eng.execute("SELECT 1", credential="at-alice")     # cached: same client
    eng.execute("SELECT 1", credential="at-bob")
    assert made == ["at-alice", "at-bob"]              # one client per credential, no leak


def test_from_config_pins_the_quota_project_for_user_clients():
    """Personal-gmail user tokens have no billing project of their own: without a pinned
    quota project the job 403s with a userProject error (spec §4)."""
    eng = BigQueryEngine.from_config({"project": "your-gcp-project", "dataset": "d"})
    captured = {}

    class FakeBQ:
        @staticmethod
        def Client(project=None, credentials=None, client_options=None):
            captured["project"] = project
            captured["quota"] = getattr(client_options, "quota_project_id", None)
            return object()

    import sys as _sys
    import types as _types
    fake_gc = _types.ModuleType("google.cloud")
    fake_gc.bigquery = FakeBQ
    fake_opts = _types.ModuleType("google.api_core.client_options")
    fake_opts.ClientOptions = lambda quota_project_id=None: type(
        "O", (), {"quota_project_id": quota_project_id})()
    fake_creds = _types.ModuleType("google.oauth2.credentials")
    fake_creds.Credentials = lambda token=None: object()
    saved = {k: _sys.modules.get(k) for k in
             ["google.cloud", "google.api_core.client_options", "google.oauth2.credentials"]}
    _sys.modules["google.cloud"] = fake_gc
    _sys.modules["google.api_core.client_options"] = fake_opts
    _sys.modules["google.oauth2.credentials"] = fake_creds
    try:
        eng._default_user_client("at-alice")
    finally:
        for k, v in saved.items():
            if v is None:
                _sys.modules.pop(k, None)
            else:
                _sys.modules[k] = v
    assert captured["project"] == "your-gcp-project"
    assert captured["quota"] == "your-gcp-project"


def main():
    print("#107 bigquery engine+provider self-test:")
    test_schema_groups_and_scopes_to_dataset()
    test_schema_honors_table_allowlist()
    test_schema_is_cached_per_engine()
    test_execute_returns_cols_rows_with_default_dataset()
    print("  PASS  dataset-scoped introspection (+allowlist) / execute w/ default dataset")
    test_from_config_requires_project_and_dataset()
    test_provider_builds_federated_store()
    print("  PASS  config validation / provider -> FederatedSqlStore (pushdown)")
    test_delegated_execute_uses_the_user_client_not_adc()
    test_delegated_clients_are_isolated_per_credential()
    test_from_config_pins_the_quota_project_for_user_clients()
    print("  PASS  query-as-user: delegated client (#193), per-credential isolation, "
          "quota project pinned")
    print("\n#107 BIGQUERY SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
