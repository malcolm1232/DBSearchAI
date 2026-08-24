"""Dump the live Azure fleet's demo tables to local CSV fixtures (ADR 0009 one-time ETL).

Requires live Azure creds in the environment (same as scripts/dev_up.sh). NOT part of the test
suite - tests use the committed CSVs so they stay hermetic. Re-run only when the sample data
changes. Usage: set -a; . ./.env; set +a; python3 scripts/snapshot_fleet_fixtures.py
"""
import csv, os, struct, subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src/dbsearch/router/demo/fixtures/azure_sql"


def _token():
    return subprocess.check_output(
        ["az", "account", "get-access-token", "--resource",
         "https://database.windows.net/", "--query", "accessToken", "-o", "tsv"]).decode().strip()


def main():
    import pyodbc
    srv, db = os.environ["AZURE_SQL_SERVER"], os.environ["AZURE_SQL_DATABASE"]
    tb = _token().encode("utf-16-le"); packed = struct.pack("<I", len(tb)) + tb
    conn = pyodbc.connect(
        f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{srv},1433;Database={db};"
        "Encrypt=yes;Connection Timeout=60", attrs_before={1256: packed}, timeout=60)
    cur = conn.cursor(); cur.execute("SELECT id, region, product, amount, closed_on FROM dbo.sales ORDER BY id")
    cols = [c[0] for c in cur.description]
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "sales.csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(cols)
        for row in cur.fetchall():
            w.writerow(row)
    print(f"wrote {OUT / 'sales.csv'}")


if __name__ == "__main__":
    main()
