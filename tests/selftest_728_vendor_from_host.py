"""#728 - a citation must never name a vendor the host printed beside it contradicts.

THE DEFECT, from the #723 prod review (Q9/Q10). The provenance card read:

    Amazon RDS (PostgreSQL)
    dbslivepg.postgres.database.azure.com / hr

The reviewer's verdict: the only element on either surface a sceptical reader could call
untrue. It sits on the one card whose entire job is checkability, and a citation that
contradicts itself on screen costs the reader their trust in the citations that are FINE.

THE CAUSE was that the vendor came from the connector KIND - the tile the user clicked - and
a kind cannot know where a database lives. `rds_postgres` is a legitimate pick for any
reachable PostgreSQL, and plain `postgres` has always worked against RDS. The old map
asserted "Amazon RDS (PostgreSQL)" for the first and "Azure Postgres" for the second, so
whichever way a user crossed the two, the label was guaranteed wrong.

THE RULE NOW: the host decides the vendor, the kind decides the engine, an unrecognised host
claims NO vendor, and a config with no host at all keeps the kind's label (nothing on screen
for it to contradict - which is the demo fleet's case, fixture stores with a cloud badge and
no endpoint).

    PYTHONPATH=src python3 tests/selftest_728_vendor_from_host.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.origins import ENGINE, SYSTEM, origin_for, system_for  # noqa: E402

AZURE_PG = "dbslivepg.postgres.database.azure.com"
RDS_PG = "orders.abc123.ap-southeast-1.rds.amazonaws.com"


# ---- the defect, both directions --------------------------------------------------------

def test_the_prod_card_no_longer_names_the_wrong_cloud():
    """The exact pairing from the review: RDS kind, Azure host."""
    o = origin_for("rds_postgres", {"host": AZURE_PG, "database": "hr"}, "hr-db")
    assert "Amazon" not in o["system"], \
        f"the label still claims AWS over an azure.com host: {o}"
    assert o["system"] == "Azure Postgres", o
    assert AZURE_PG in o["location"], "the host must still be shown - it is the checkable part"


def test_the_mirror_case_the_old_map_got_wrong_too():
    """Plain `postgres` pointed at RDS. #672 fixed only the direction it happened to hit."""
    o = origin_for("postgres", {"host": RDS_PG, "database": "orders"}, "orders-db")
    assert "Azure" not in o["system"], f"the label claims Azure over an RDS host: {o}"
    assert o["system"] == "Amazon RDS (PostgreSQL)", o


def test_rds_names_the_engine_because_rds_hosts_several():
    """"Amazon RDS" alone under-describes: the reader cannot tell which engine answered."""
    assert system_for("rds_mysql", {"host": "shop.abc.eu-west-1.rds.amazonaws.com"}) \
        == "Amazon RDS (MySQL)"
    assert system_for("rds_postgres", {"host": RDS_PG}) == "Amazon RDS (PostgreSQL)"


def test_a_host_we_cannot_place_claims_no_vendor_at_all():
    """THE CASE THE OLD CODE GOT MOST CONFIDENTLY WRONG. A self-hosted or on-prem endpoint is
    visible on the card, and the kind's guess is precisely what would be untrue there. Name
    the engine, claim no cloud."""
    for host in ("h1", "db.internal.acme.corp", "10.0.0.4", "localhost"):
        got = system_for("postgres", {"host": host})
        assert got == "PostgreSQL", f"{host} -> {got}"
        assert "Azure" not in got and "Amazon" not in got


def test_every_recognised_cloud_endpoint():
    cases = [
        ("azure_sql", "examplesql1234.database.windows.net", "Azure SQL"),
        ("synapse", "dbs-syn.sql.azuresynapse.net", "Azure Synapse"),
        ("mysql", "shop.mysql.database.azure.com", "Azure MySQL"),
        ("postgres", AZURE_PG, "Azure Postgres"),
        ("redshift", "wg.123.ap-southeast-1.redshift-serverless.amazonaws.com", "Redshift"),
        ("cosmos", "https://dbsdemo.documents.azure.com:443/", "Cosmos DB"),
        ("databricks", "adb-123.4.azuredatabricks.net", "Azure Databricks"),
    ]
    for kind, host, want in cases:
        assert system_for(kind, {"server": host}) == want, (kind, host, want)


def test_the_hostname_shapes_a_real_manifest_carries():
    """SQL Server's port comma, a tcp: prefix and Cosmos's full URL all still resolve - a
    parse failure here silently downgrades a correct label to the engine-only fallback."""
    assert system_for("azure_sql", {"server": "tcp:ex.database.windows.net,1433"}) == "Azure SQL"
    assert system_for("azure_sql", {"server": "ex.database.windows.net,1433"}) == "Azure SQL"
    assert system_for("azure_sql", {"server": "EX.DATABASE.WINDOWS.NET"}) == "Azure SQL"
    assert system_for("cosmos", {"account": "https://a.documents.azure.com:443/"}) == "Cosmos DB"


def test_a_lookalike_suffix_does_not_match():
    """`endswith` on a dotted suffix, not a substring search: an attacker-ish or merely
    unlucky hostname that CONTAINS a cloud's domain must not borrow its name."""
    for host in ("rds.amazonaws.com.acme.internal", "notdatabase.windows.net.example.org"):
        got = system_for("postgres", {"host": host})
        assert got == "PostgreSQL", f"{host} borrowed a vendor name: {got}"


# ---- what must NOT change ---------------------------------------------------------------

def test_no_host_keeps_the_kinds_label():
    """The demo fleet: fixture-backed stores with a cloud badge and no endpoint. Nothing is
    on screen for the label to contradict, so nothing here is a false claim."""
    for kind in ("postgres", "mysql", "synapse", "azure_sql"):
        assert system_for(kind, {"fixture": {"files": []}}) == SYSTEM[kind], kind


def test_non_sql_kinds_are_untouched():
    assert origin_for("csv", {}, "Sales figures") == {"system": "Local CSV",
                                                      "location": "Sales figures"}
    assert origin_for("local", {}, "Staff Handbook")["system"] == "Indexed docs"
    assert origin_for("sharepoint", {"title": "Finance"}, "Finance")["system"] == "SharePoint"


def test_unknown_kind_still_title_cases():
    o = origin_for("neo4j", {}, "Graph")
    assert o["system"].lower() == "neo4j" and o["location"] == "Graph", o


def test_the_location_still_pinpoints_host_and_database():
    """#672's requirement, unchanged: a reader must be able to tell WHICH database answered."""
    o = origin_for("rds_postgres", {"host": RDS_PG, "database": "orders"}, "orders-db")
    assert "orders.abc123" in o["location"] and "orders" in o["location"], o


def test_every_sql_kind_declares_an_engine():
    """A SQL kind with no ENGINE entry falls back to the kind's own cloud guess on an
    unrecognised host - reintroducing exactly this defect for that connector. This is the
    guard that makes the next connector safe."""
    from dbsearch.router.origins import _SQL_KINDS
    missing = sorted(k for k in _SQL_KINDS if k not in ENGINE)
    assert not missing, f"SQL kinds with no engine label: {missing}"


if __name__ == "__main__":
    fails = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                fails.append(name)
                print(f"  FAIL {name}: {e}")
    print(f"\n{'FAILED' if fails else 'PASSED'} - {len(fails)} failed")
    sys.exit(1 if fails else 0)
