"""#279 Task 1 (3a): the demo compose seam (`compose_demo_catalog`) over a fixture-backed
`_State` — the alice/bob doc stores (hr-wiki, fin-ledger) PLUS the four badged fixture SQL
connectors (DEMO_FLEET_STORES: azure_sql/postgres/mysql/synapse). Proves:
  - all 4 fixture SQL stores compose + badge correctly (origin.system), alongside the docs;
  - alice sees fin-ledger (deal-team) and the azure-deals store answers a real tally from
    its local fixture;
  - bob does NOT see fin-ledger (LAW 2), but DOES see the 4 DB stores + hr-wiki;
  - a `_State(fixture_backed=False)` given DEMO_FLEET_STORES ignores the `fixture:` blocks
    (its providers stay on the default cloud factory) — mirrors the Slice-1/2 LAW-2 test.

Run: python3 tests/selftest_demo_catalog.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
# Hermetic default model (ExtractiveLlm) regardless of a dev machine's local env — matches
# selftest_force_extractive.py's convention.
for _k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.structured import SqliteEngine  # noqa: E402
from dbsearch.server.edition import build_edition  # noqa: E402
from dbsearch.server.router_api import (  # noqa: E402
    DEMO_FLEET_STORES, DemoCatalog, _State, compose_demo_catalog, demo_fleet_display,
)

_BADGES = {
    "azure-deals": "Azure SQL",
    "support-tickets": "Azure Postgres",
    "storefront": "Azure MySQL",
    "warehouse": "Azure Synapse",
}


def test_demo_catalog_has_badged_fixture_stores_plus_docs():
    demo = compose_demo_catalog(build_edition())
    assert isinstance(demo, DemoCatalog)
    assert demo.skipped == [], demo.skipped   # every store built + probed clean
    ids = {n.id for n in demo.catalog.stores()}
    assert ids == {"azure-deals", "support-tickets", "storefront", "warehouse",
                   "hr-wiki", "fin-ledger"}, ids
    for store_id, system in _BADGES.items():
        origin = demo.catalog.get(store_id).profile.origin
        assert origin["system"] == system, (store_id, origin)
    print("  PASS  demo catalog composes 4 badged fixture SQL stores + hr-wiki/fin-ledger, "
          "zero skipped")


def test_alice_sees_fin_ledger_and_azure_deals_tallies():
    demo = compose_demo_catalog(build_edition())
    edition = build_edition()
    visible = {n.id for n in
               demo.catalog.visible_stores(edition.identity.expand_groups("alice"))}
    assert "fin-ledger" in visible, visible

    store = demo.catalog.get("azure-deals").store
    access = store.authorize("alice")
    evidence = store.retrieve(access, "total amount by region")
    assert evidence, "expected non-empty evidence from azure-deals"
    cols, rows, n = store.rerun_sql(
        access,
        "SELECT region, SUM(amount) AS total FROM sales GROUP BY region ORDER BY region")
    got = {r[0]: int(r[1]) for r in rows}
    assert got == {"amer": 195000, "apac": 205000, "emea": 125000}, got
    print("  PASS  alice sees fin-ledger; azure-deals tallies "
          "amer=195000 apac=205000 emea=125000")


def test_bob_law2_no_fin_ledger_but_sees_db_stores_and_hr_wiki():
    demo = compose_demo_catalog(build_edition())
    edition = build_edition()
    visible = {n.id for n in
               demo.catalog.visible_stores(edition.identity.expand_groups("bob"))}
    assert "fin-ledger" not in visible, visible   # LAW 2: deal-team only, bob is all-staff
    assert visible == {"azure-deals", "support-tickets", "storefront", "warehouse",
                       "hr-wiki"}, visible
    print("  PASS  bob (LAW 2): fin-ledger invisible; 4 DB stores + hr-wiki visible")


def test_demo_project_falcon_alice_sees_bob_denied():
    """The demo's named-deal LAW-2 case: 'Project Falcon valuation' is a deal-team doc, so
    the alice/bob picker shows alice the $4.2B valuation and denies bob - the same contrast
    as the $4.2M revenue doc, on a differently-numbered document so neither can echo the
    other."""
    from dbsearch.adapters.local import ExtractiveLlm
    demo = compose_demo_catalog(build_edition())
    llm = ExtractiveLlm()
    q = "What is the Project Falcon valuation"

    a = demo.service.ask("alice", q, llm).to_dict()
    a_titles = " ".join(c.get("title") or "" for c in a.get("citations", []))
    assert "Project Falcon Plan" in a_titles, ("alice should see Falcon", a_titles)
    assert "4.2 billion" in (a.get("answer") or ""), ("alice should get the valuation", a)

    b = demo.service.ask("bob", q, llm).to_dict()
    b_titles = " ".join(c.get("title") or "" for c in b.get("citations", []))
    assert "Falcon" not in b_titles, ("LAW 2: bob must not see Falcon", b_titles)
    assert "4.2 billion" not in (b.get("answer") or ""), ("bob must not learn the valuation", b)
    print("  PASS  demo Project Falcon: alice sees $4.2B valuation, bob denied (LAW 2)")


def test_live_state_fixture_backed_false_ignores_fixture_blocks():
    """Slice-1/2 LAW-2 pattern, widened to all four connectors via _State itself: on the
    live/default path (fixture_backed=False) the providers keep the default cloud factory,
    so a fixture: block in a submitted config is inert and never yields a local engine."""
    fake_creds = {
        "azure_sql": {"server": "unreachable.example", "database": "d", "user": "u", "password": "p"},
        "postgres": {"host": "unreachable.example", "database": "d", "user": "u", "password": "p"},
        "mysql": {"host": "unreachable.example", "database": "d", "user": "u", "password": "p"},
        "synapse": {"server": "unreachable.example", "database": "d", "user": "u", "password": "p"},
    }
    st = _State(fixture_backed=False)
    for entry in DEMO_FLEET_STORES:
        provider = st.registry.get(entry["kind"])
        config = {"id": entry["id"], "business_unit": entry["business_unit"],
                  "title": entry["title"], "description": entry["description"],
                  **entry["config"], **fake_creds[entry["kind"]]}
        store = provider.build(config)          # lazy connect: no network
        assert not isinstance(store._engine, SqliteEngine), \
            entry["kind"] + ": fixture must NOT produce a local engine on the live/default factory"
        print("  PASS  " + entry["kind"] + " LAW 2: fixture inert on _State(fixture_backed=False)")


def test_demo_fleet_display_matches_the_composed_catalog():
    """#279 (canvas demo mode): the /router/demo display list must name EXACTLY the stores a
    demo identity's catalog composes (display == ask target), badged by connector kind, with
    no fixture/seed internals leaked to the client."""
    fleet = demo_fleet_display()
    ids = {s["id"] for s in fleet}
    composed = {n.id for n in compose_demo_catalog(build_edition()).catalog.stores()}
    assert ids == composed, (ids, composed)
    kinds = {s["id"]: s["kind"] for s in fleet}
    assert kinds["azure-deals"] == "azure_sql" and kinds["support-tickets"] == "postgres"
    assert kinds["storefront"] == "mysql" and kinds["warehouse"] == "synapse"
    for s in fleet:                       # no fixture/seed internals reach the browser
        assert "fixture" not in s and "seed" not in s and "config" not in s, s
    print("  PASS  demo_fleet_display names exactly the composed demo stores, badged, no internals")


def main():
    print("Demo catalog (#279 Task 1) self-test:")
    test_demo_fleet_display_matches_the_composed_catalog()
    test_demo_catalog_has_badged_fixture_stores_plus_docs()
    test_alice_sees_fin_ledger_and_azure_deals_tallies()
    test_bob_law2_no_fin_ledger_but_sees_db_stores_and_hr_wiki()
    test_demo_project_falcon_alice_sees_bob_denied()
    test_live_state_fixture_backed_false_ignores_fixture_blocks()
    print("\nDEMO CATALOG SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
