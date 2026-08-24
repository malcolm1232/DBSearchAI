#!/usr/bin/env python3
"""#156 — in-DB provisioning: Entra users + differentiated permissions (LAW 2 lives
in the database, not in DBSearch). Auth = the admin's az token (no SQL passwords).

  python3 scripts/provision_sql_users.py            # provision
  python3 scripts/provision_sql_users.py --teardown # remove users + RLS

alice-test: plain SELECT on dbo.sales (all rows).
bob-test:   SELECT + RLS filter to a deterministic row subset.
The RLS predicate column is discovered at run time: first a text column named like
region/country/segment/category; else fall back to `<first int column> % 2 = 0`.
"""
import os
import struct
import subprocess
import sys

# #685/#686: these used to default to one specific deployment's server, database and Entra
# domain. They are configuration, not constants - a default that names somebody else's tenant
# is worse than no default, because it silently points a provisioning script that CREATES
# USERS AND ALTERS SECURITY POLICIES at the wrong database. Required, and named in the error.
SERVER = os.environ.get("AZURE_SQL_SERVER", "")
DB = os.environ.get("AZURE_SQL_DATABASE", "")
DOMAIN = os.environ.get("AZURE_TEST_USER_DOMAIN", "")
USERS = [f"alice-test@{DOMAIN}", f"bob-test@{DOMAIN}"]
BOB = USERS[1]


def _require_config():
    """Fail before touching a database, naming what is missing.

    Deliberately NOT called at import time: tests/selftest_provision_rls_escaping.py imports
    `pick_filter` from this module to prove the RLS predicate escapes an apostrophe rather
    than letting it break the DDL, and that test must run anywhere - no Azure, no env, no
    network. Import stays free; only the paths that actually connect check.
    """
    missing = [n for n, v in (("AZURE_SQL_SERVER", SERVER), ("AZURE_SQL_DATABASE", DB),
                              ("AZURE_TEST_USER_DOMAIN", DOMAIN)) if not v]
    if missing:
        sys.exit("set " + ", ".join(missing) + " - this script creates in-database users and "
                 "alters security policies, so it will not guess which database you mean")

SQL_COPT_SS_ACCESS_TOKEN = 1256


def admin_conn():
    _require_config()
    import pyodbc
    tok = subprocess.check_output(
        ["az", "account", "get-access-token",
         "--resource", "https://database.windows.net/",
         "--query", "accessToken", "-o", "tsv"]).decode().strip()
    body = tok.encode("utf-16-le")
    packed = struct.pack("<I", len(body)) + body
    return pyodbc.connect(
        f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{SERVER},1433;"
        f"Database={DB};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=75",
        attrs_before={SQL_COPT_SS_ACCESS_TOKEN: packed}, timeout=75, autocommit=True)


def pick_filter(cur):
    """-> (param_decl, column_ref, predicate, colname): the RLS filter column,
    discovered from the live schema with its REAL SQL type."""
    cur.execute("SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME='sales' ORDER BY ORDINAL_POSITION")
    cols = cur.fetchall()
    if not cols:
        sys.exit("no dbo.sales table — adjust TABLE in this script")
    for name, dtype, maxlen in cols:
        if dtype in ("varchar", "nvarchar", "char", "nchar") and any(
                k in name.lower() for k in ("region", "country", "segment", "category")):
            cur.execute(f"SELECT TOP 1 [{name}] FROM dbo.sales GROUP BY [{name}] "
                        f"ORDER BY COUNT(*) DESC")
            val = cur.fetchone()[0]
            if val is None:
                continue  # no non-NULL value to filter bob on — fall through to int-modulo branch
            safe = val.replace("'", "''")  # T-SQL literal escape: double embedded single quotes
            sqltype = f"{dtype}({maxlen if maxlen and maxlen > 0 else 'max'})"
            return f"@v {sqltype}", f"[{name}]", f"@v = N'{safe}'", name
    for name, dtype, _ in cols:
        if dtype in ("int", "bigint", "smallint", "tinyint", "decimal", "numeric"):
            return f"@v {dtype}", f"[{name}]", "CAST(@v AS bigint) % 2 = 0", name
    sys.exit("no usable filter column on dbo.sales")


def provision(cur):
    for upn in USERS:
        cur.execute(f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name=N'{upn}') "
                    f"CREATE USER [{upn}] FROM EXTERNAL PROVIDER")
        cur.execute(f"GRANT SELECT ON dbo.sales TO [{upn}]")
        print(f"user + GRANT SELECT: {upn}")
    param_decl, colref, predicate, colname = pick_filter(cur)
    cur.execute("IF EXISTS (SELECT 1 FROM sys.security_policies WHERE name='sales_policy') "
                "DROP SECURITY POLICY rls.sales_policy")
    cur.execute("IF OBJECT_ID('rls.fn_sales_filter') IS NOT NULL DROP FUNCTION rls.fn_sales_filter")
    cur.execute("IF SCHEMA_ID('rls') IS NULL EXEC('CREATE SCHEMA rls')")
    cur.execute(
        f"CREATE FUNCTION rls.fn_sales_filter({param_decl}) RETURNS TABLE "
        f"WITH SCHEMABINDING AS RETURN SELECT 1 AS ok "
        f"WHERE USER_NAME() <> N'{BOB}' OR {predicate}")
    cur.execute(f"CREATE SECURITY POLICY rls.sales_policy "
                f"ADD FILTER PREDICATE rls.fn_sales_filter({colref}) ON dbo.sales "
                f"WITH (STATE = ON)")
    print(f"RLS on dbo.sales: bob filtered by {colname} ({predicate}); alice unfiltered")


def teardown(cur):
    cur.execute("IF EXISTS (SELECT 1 FROM sys.security_policies WHERE name='sales_policy') "
                "DROP SECURITY POLICY rls.sales_policy")
    cur.execute("IF OBJECT_ID('rls.fn_sales_filter') IS NOT NULL DROP FUNCTION rls.fn_sales_filter")
    for upn in USERS:
        cur.execute(f"IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name=N'{upn}') "
                    f"DROP USER [{upn}]")
    print("SQL users + RLS removed")


# Note: identifiers ([name] table/column names, dtype) interpolated into DDL here come
# from INFORMATION_SCHEMA metadata and our own constants — safe to splice directly.
# The discovered filter VALUE (val, from actual table data) is column data, not metadata,
# so it is quote-escaped (see `safe` above) before interpolation into the DDL literal.
# Admin-only provisioning script, not a query path; do not reuse this pattern in server code.

if __name__ == "__main__":
    cur = admin_conn().cursor()
    teardown(cur) if "--teardown" in sys.argv else provision(cur)
