"""#176 — origin descriptor self-test. Run: python3 tests/selftest_origins.py"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.origins import origin_for  # noqa: E402


def test_azure_sql_system_and_location():
    o = origin_for("azure_sql", {"server": "examplesql1234.database.windows.net",
                                 "database": "exampledb"}, "Azure SQL deals")
    assert o["system"] == "Azure SQL", o
    assert "examplesql1234" in o["location"] and "exampledb" in o["location"], o


def test_sql_location_with_one_field_missing():
    o = origin_for("postgres", {"host": "h1"}, "PG")   # no database
    # #728: this asserted "Azure Postgres" - a cloud claimed over a host that does not
    # support it, which is the defect the prod review caught in its other direction
    # ("Amazon RDS (PostgreSQL)" printed above an azure.com endpoint). `h1` places nowhere,
    # so the label names the engine and claims no vendor. See selftest_728_vendor_from_host.
    assert o["system"] == "PostgreSQL", o
    assert o["location"], o                            # non-empty from host alone


def test_csv_uses_title_as_location():
    o = origin_for("csv", {}, "Sales figures")
    assert o["system"] == "Local CSV" and o["location"] == "Sales figures", o


def test_local_indexed():
    o = origin_for("local", {}, "Staff Handbook")
    assert o["system"] == "Indexed docs" and o["location"] == "Staff Handbook", o


def test_sharepoint():
    o = origin_for("sharepoint", {"title": "Finance"}, "Finance")
    assert o["system"] == "SharePoint", o


def test_unknown_kind_titlecased():
    o = origin_for("neo4j", {}, "Graph")
    assert o["system"] == "Neo4J" or o["system"] == "Neo4j", o     # title-cased fallback
    assert o["location"] == "Graph", o


def test_gdrive_is_named_google_drive_not_titlecased():
    """#770. A public Drive folder link carries no endpoint, so `gdrive` takes the no-host
    fallback - and with no SYSTEM entry that fallback title-cased the kind, printing
    "Gdrive" on every Drive citation. The assertion is written against the FALLBACK's
    output, not just the desired string: `!= "Gdrive"` is what fails if the entry is ever
    removed again, which is the way this broke the first time (a connector shipped without
    its one table entry)."""
    o = origin_for("gdrive", {"link": "https://drive.google.com/drive/folders/abc123"},
                   "Drive notes")
    assert o["system"] == "Google Drive", o
    assert o["system"] != "Gdrive", o
    # There is no host in a folder link, so location falls back to the store's title -
    # the same rule csv/local follow.
    assert o["location"] == "Drive notes", o


def main():
    for fn in (test_azure_sql_system_and_location, test_sql_location_with_one_field_missing,
               test_csv_uses_title_as_location, test_local_indexed, test_sharepoint,
               test_gdrive_is_named_google_drive_not_titlecased,
               test_unknown_kind_titlecased):
        fn()
        print("ok", fn.__name__)
    print("selftest_origins: ALL OK")


if __name__ == "__main__":
    main()
