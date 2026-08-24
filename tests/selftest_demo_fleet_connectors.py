"""Slice 2: the SQL-family fleet connectors (Postgres/MySQL/Synapse) run on LOCAL fixtures in
demo scope, badged as the real Azure services, and a user-submitted fixture is inert on the live
path (LAW 2). Same seam as Slice 1's azure_sql, widened.
Run: python3 tests/selftest_demo_fleet_connectors.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.demo_backing import fixture_or_cloud_factory, demo_fixture_path  # noqa: E402
from dbsearch.router.origins import origin_for  # noqa: E402
from dbsearch.router.structured import SqliteEngine, keyword_sql_generator  # noqa: E402
from dbsearch.router import (  # noqa: E402
    AzureSqlEngine, MySqlEngine, MySqlProvider,
    PostgresEngine, PostgresProvider, SynapseProvider,
)

# (kind, ProviderClass, cloud_from_config, badge_system, table, sum_col, expected, live_creds)
CONNECTORS = [
    ("postgres", PostgresProvider, PostgresEngine.from_config, "Azure Postgres",
     "support_tickets", "resolution_hours", {"amer": 16, "apac": 16, "emea": 23},
     {"host": "unreachable.example", "database": "d", "user": "u", "password": "p"}),
    ("mysql", MySqlProvider, MySqlEngine.from_config, "Azure MySQL",
     "storefront_orders", "amount", {"amer": 800, "apac": 800, "emea": 900},
     {"host": "unreachable.example", "database": "d", "user": "u", "password": "p"}),
    ("synapse", SynapseProvider, AzureSqlEngine.from_config, "Azure Synapse",
     "warehouse_sales", "units", {"amer": 200, "apac": 250, "emea": 155},
     {"server": "unreachable.example", "database": "d", "user": "u", "password": "p"}),
]


def _fixture_config(kind, table):
    return {"id": "demo-" + kind, "business_unit": "sales", "title": kind + " demo",
            "description": table + " region totals sum",
            "fixture": {"files": [demo_fixture_path(kind, table + ".csv")]}}


def test_badged_local_answer():
    for kind, Provider, _from_config, badge, table, col, expected, _creds in CONNECTORS:
        config = _fixture_config(kind, table)
        assert origin_for(kind, config, config["title"])["system"] == badge, (kind, badge)

        def _cloud(_c, _k=kind):
            raise AssertionError(_k + ": cloud factory must NOT be called in demo scope")

        provider = Provider(engine_factory=fixture_or_cloud_factory(_cloud))
        store = provider.build(config)
        store._gen = keyword_sql_generator
        access = store.authorize("alice")
        evidence = store.retrieve(access, "total " + col + " by region")
        assert evidence, kind + ": expected non-empty evidence from the fixture-backed store"
        cols, rows, n = store.rerun_sql(
            access,
            "SELECT region, SUM(" + col + ") AS total FROM " + table
            + " GROUP BY region ORDER BY region")
        got = {r[0]: int(r[1]) for r in rows}
        assert got == expected, (kind, got)
        print("  PASS  " + kind + " -> badged " + badge + ", answers from local fixture, tally "
              + str(got))


def test_law2_user_fixture_inert_on_live():
    for kind, Provider, _from_config, _badge, table, _col, _expected, creds in CONNECTORS:
        provider = Provider()   # default cloud factory, no fixture-awareness
        config = {"id": "evil-" + kind, "business_unit": "x", "title": "evil",
                  "fixture": {"files": [demo_fixture_path(kind, table + ".csv")]}, **creds}
        store = provider.build(config)          # lazy connect: no network
        assert not isinstance(store._engine, SqliteEngine), \
            kind + ": user fixture must NOT produce a local engine on the live factory"
        print("  PASS  " + kind + " LAW 2: user fixture inert on the live/default factory")


def main():
    print("Demo fleet connectors (Slice 2) self-test:")
    test_badged_local_answer()
    test_law2_user_fixture_inert_on_live()
    print("\nDEMO FLEET CONNECTORS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
