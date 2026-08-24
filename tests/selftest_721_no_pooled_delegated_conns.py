"""#721: a delegated (query-as-user) pyodbc connection must never come from the
ODBC connection pool.

Mechanism, demonstrated live during #713: `user_connect` builds an IDENTICAL
connection string for every user — the per-user AAD token rides only in
`attrs_before` (SQL_COPT_SS_ACCESS_TOKEN). pyodbc pools process-wide with the
STRING as the key and applies `attrs_before` only to genuinely new connections,
so after one user's connection closes (engine rebuild on recompose, error path,
GC), the NEXT user's connect() can receive the PREVIOUS user's live
authenticated connection. Probed against the real fleet: bob counted 6 rows
(alice's RLS view) same-process-after-alice, 2 rows in a fresh process.

The fix sets `pyodbc.pooling = False` before the delegated connect. Pooling is
a process-wide pyodbc flag that only takes effect for connections created after
it is set; the engine caches per-credential connections itself (ADR 0006), so
the delegated path loses nothing.

Run: PYTHONPATH=src python3 tests/selftest_721_no_pooled_delegated_conns.py
"""
import sys
import types

sys.path.insert(0, "src")

FAILED = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


# A fake pyodbc whose pooling flag starts True (the real default) and which
# records the flag's value AT CONNECT TIME — that is the moment that matters.
fake = types.ModuleType("pyodbc")
fake.pooling = True
observed = []


def _connect(*a, **kw):
    observed.append(fake.pooling)
    return object()


fake.connect = _connect
sys.modules["pyodbc"] = fake

from dbsearch.router.providers.azure_sql import AzureSqlEngine

engine = AzureSqlEngine.from_config({
    "server": "s.example.net", "database": "db", "user": "u", "password": "p",
    "use_odbc": True,
})
check("engine builds with a user_connect seam", engine._user_connect is not None)

engine._user_connect("fake-aad-token")
check("delegated connect observed pooling DISABLED",
      observed and observed[-1] is False, f"observed={observed}")
check("pyodbc.pooling left False for subsequent delegated connects",
      fake.pooling is False)

print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("selftest_721_no_pooled_delegated_conns: ALL PASS")
