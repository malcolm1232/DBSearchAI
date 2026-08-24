import os, sys, tempfile
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.demo_backing import fixture_or_cloud_factory, demo_fixture_path  # noqa: E402
from dbsearch.router.structured import SqliteEngine, keyword_sql_generator  # noqa: E402
from dbsearch.router.providers.azure_sql import AzureSqlProvider  # noqa: E402
from dbsearch.router.origins import origin_for  # noqa: E402


def _sales_csv(dirpath):
    p = Path(dirpath) / "sales.csv"
    p.write_text(
        "id,region,product,amount,closed_on\n"
        "1,apac,Platform License,125000,2026-01-14\n"
        "2,apac,Support,80000,2026-02-03\n"
        "3,emea,Platform License,95000,2026-01-22\n"
        "4,emea,Services,30000,2026-03-11\n"
        "5,amer,Platform License,150000,2026-02-28\n"
        "6,amer,Support,45000,2026-03-19\n"
    )
    return str(p)


def test_fixture_builds_local_sqlite():
    def cloud_factory(_config):
        raise AssertionError("cloud factory must NOT be called when a fixture is present")
    factory = fixture_or_cloud_factory(cloud_factory)
    with tempfile.TemporaryDirectory() as d:
        engine = factory({"id": "azure-deals", "fixture": {"files": [_sales_csv(d)]}})
    assert isinstance(engine, SqliteEngine), type(engine)
    cols, rows = engine.execute(
        "SELECT region, SUM(amount) AS total FROM sales GROUP BY region ORDER BY region")
    got = {r[0]: int(r[1]) for r in rows}
    assert got == {"amer": 195000, "apac": 205000, "emea": 125000}, got
    print("  PASS  fixture -> local SqliteEngine, tally correct")


def test_no_fixture_delegates_to_cloud():
    sentinel = object()
    def cloud_factory(config):
        assert config["id"] == "azure-deals"
        return sentinel
    factory = fixture_or_cloud_factory(cloud_factory)
    assert factory({"id": "azure-deals", "config": {"server": "x"}}) is sentinel
    print("  PASS  no fixture -> delegates to cloud factory")


def test_badged_azure_sql_store_answers_from_fixture():
    def cloud_factory(_c):
        raise AssertionError("must not touch cloud in demo scope")
    provider = AzureSqlProvider(engine_factory=fixture_or_cloud_factory(cloud_factory))
    config = {"id": "azure-deals", "business_unit": "sales", "title": "Azure SQL deals",
              "description": "closed deals revenue amount by region product",
              "fixture": {"files": [demo_fixture_path("azure_sql", "sales.csv")]}}
    store = provider.build(config)
    # badge: kind azure_sql -> origins.py renders system "Azure SQL"
    assert origin_for("azure_sql", config, config["title"])["system"] == "Azure SQL"
    # answers from the local fixture (no delegated credential needed for the embedded engine)
    store._gen = keyword_sql_generator
    access = store.authorize("alice")
    evidence = store.retrieve(access, "total amount by region")
    assert evidence, "expected non-empty evidence from the fixture-backed store"
    # provenance carries the SQL; re-run it to tally deterministically
    cols, rows, n = store.rerun_sql(access,
        "SELECT region, SUM(amount) AS total FROM sales GROUP BY region ORDER BY region")
    got = {r[0]: int(r[1]) for r in rows}
    assert got == {"amer": 195000, "apac": 205000, "emea": 125000}, got
    print("  PASS  badged azure_sql store answers from local fixture, tally correct")


def test_law2_user_fixture_is_inert_on_cloud_factory():
    """The LIVE/user path uses the DEFAULT AzureSqlProvider (no fixture-awareness). A user-
    submitted `fixture:` must be ignored there so it can never be a bypass around real Azure
    auth (ADR 0009 / LAW 2)."""
    from dbsearch.router.structured import SqliteEngine
    provider = AzureSqlProvider()   # no engine_factory => default cloud factory
    config = {"id": "evil", "business_unit": "x", "title": "evil",
              "server": "unreachable.example", "database": "d", "user": "u", "password": "p",
              "fixture": {"files": [demo_fixture_path("azure_sql", "sales.csv")]}}
    store = provider.build(config)          # lazy connect: does NOT touch the network
    assert not isinstance(store._engine, SqliteEngine), \
        "user-submitted fixture must NOT produce a local engine on the live/default factory"
    print("  PASS  LAW 2: user fixture inert on the live/default factory")


def main():
    print("Demo fixture-backing self-test:")
    test_fixture_builds_local_sqlite()
    test_no_fixture_delegates_to_cloud()
    test_badged_azure_sql_store_answers_from_fixture()
    test_law2_user_fixture_is_inert_on_cloud_factory()
    print("\nDEMO FIXTURE-BACKING SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
