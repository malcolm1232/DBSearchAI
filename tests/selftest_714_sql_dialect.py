"""#714: the NL2SQL generator must know the target engine's SQL dialect.

Found live by the #713 routing matrix: a compare question against Synapse made the
model emit `... LIMIT 5`, and T-SQL has no LIMIT — ProgrammingError 42000, surfaced
to the user as "the source did not respond successfully". Every engine tolerated
the generic SQL except the two T-SQL ones (azure_sql and synapse share
AzureSqlEngine), and single-table aggregates never emit LIMIT, which is why the
demo questions never caught it.

The fix threads `engine.dialect` -> FederatedSqlStore._generate -> the generator ->
the adapter prompt, binding by PARAMETER NAME (the #673 rail pattern): a generator
or adapter that never declared `dialect` keeps its exact old call shape, so the
seam stays drop-in for stubs and the keyword generator.

Run: PYTHONPATH=src python3 tests/selftest_714_sql_dialect.py
"""
import sys

sys.path.insert(0, "src")

FAILED = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


# ---- 1. every pushdown engine declares its dialect --------------------------------
from dbsearch.router.structured import SqliteEngine
from dbsearch.router.providers.azure_sql import AzureSqlEngine
from dbsearch.router.providers.postgres import PostgresEngine
from dbsearch.router.providers.mysql import MySqlEngine
from dbsearch.router.providers.redshift import RedshiftEngine
from dbsearch.router.providers.bigquery import BigQueryEngine
from dbsearch.router.providers.databricks import DatabricksEngine

for cls, frag in ((SqliteEngine, "sqlite"), (AzureSqlEngine, "t-sql"),
                  (PostgresEngine, "postgres"), (MySqlEngine, "mysql"),
                  (RedshiftEngine, "redshift"), (BigQueryEngine, "bigquery"),
                  (DatabricksEngine, "spark")):
    d = getattr(cls, "dialect", "")
    check(f"{cls.__name__}.dialect names {frag!r}", frag in d.lower(), f"dialect={d!r}")

# the T-SQL dialect string must carry the TOP-not-LIMIT instruction — that IS the bug
d = AzureSqlEngine.dialect.lower()
check("T-SQL dialect string bans LIMIT and names TOP", "limit" in d and "top" in d, d)


# ---- 2. FederatedSqlStore passes dialect to a generator that declares it ----------
from dbsearch.router.structured import FederatedSqlStore

seen = {}


def spy_gen(question, schema, dialect=""):
    seen["dialect"] = dialect
    return "SELECT region FROM sales GROUP BY region"


def legacy_gen(question, schema):
    seen["legacy_called"] = True
    return "SELECT region FROM sales GROUP BY region"


class FakeEngine:
    dialect = "T-SQL (test)"

    def schema(self):
        return [{"table": "sales", "columns": [{"name": "region", "type": "varchar"}]}]

    def refresh_schema(self):
        pass

    def execute(self, sql, access=None):
        return ["region"], [["emea"]], 1


store = FederatedSqlStore("s1", "bu", "t", "d", FakeEngine(), sql_generator=spy_gen)
sql, subset, schema = store._resolve_sql("total by region")
check("dialect-aware generator receives the engine dialect",
      seen.get("dialect") == "T-SQL (test)", f"seen={seen}")

store2 = FederatedSqlStore("s2", "bu", "t", "d", FakeEngine(), sql_generator=legacy_gen)
sql2, _, _ = store2._resolve_sql("total by region")
check("legacy generator without dialect param still works",
      seen.get("legacy_called") is True and "SELECT" in sql2)


# ---- 3. llm_sql_generator threads dialect to an adapter that declares it ----------
from dbsearch.router.structured import llm_sql_generator


class SpyLLM:
    def generate_sql(self, question, schema, dialect=""):
        seen["llm_dialect"] = dialect
        return "SELECT region FROM sales GROUP BY region"


class LegacyLLM:
    def generate_sql(self, question, schema):
        seen["legacy_llm"] = True
        return "SELECT region FROM sales GROUP BY region"


gen = llm_sql_generator(SpyLLM())
gen("q", [{"table": "sales", "columns": []}], dialect="T-SQL (test)")
check("llm_sql_generator threads dialect to the adapter",
      seen.get("llm_dialect") == "T-SQL (test)", f"seen={seen}")

gen2 = llm_sql_generator(LegacyLLM())
out = gen2("q", [{"table": "sales", "columns": []}], dialect="T-SQL (test)")
check("legacy adapter without dialect param still works",
      seen.get("legacy_llm") is True and "SELECT" in out)


# ---- 3b. the PRODUCTION wrapper: memoized_sql_generator (the layer that actually
# swallowed the dialect on the live rig — router_api wires memoized(llm_sql_generator),
# and a wrapper that never declared `dialect` makes the store's bind-by-name pass skip it)
from dbsearch.router.structured import memoized_sql_generator

calls = []


def counting_gen(question, schema, dialect=""):
    calls.append(dialect)
    return f"SELECT 1 -- {dialect}"


memo = memoized_sql_generator(counting_gen)
a = memo("q", [{"table": "t", "columns": []}], dialect="T-SQL")
b = memo("q", [{"table": "t", "columns": []}], dialect="PostgreSQL")
c = memo("q", [{"table": "t", "columns": []}], dialect="T-SQL")
check("memoized wrapper threads dialect through", calls[:2] == ["T-SQL", "PostgreSQL"],
      f"calls={calls}")
check("memo key includes dialect (two dialects, two generations, then a hit)",
      a != b and c == a and len(calls) == 2, f"a={a!r} b={b!r} calls={len(calls)}")


# ---- 3c. the FALLBACK layer: keyword_sql_generator (fires when a model generation
# fails validation on the identity path — a dialect-blind fallback turns a recoverable
# bad generation into the hard 42000 the matrix caught live)
from dbsearch.router.structured import keyword_sql_generator

schema_one = [{"table": "dw_spend", "columns": [{"name": "region", "type": "varchar"}]}]
kw_tsql = keyword_sql_generator("show everything", schema_one,
                                dialect=AzureSqlEngine.dialect)
kw_generic = keyword_sql_generator("show everything", schema_one)
check("keyword fallback emits TOP for T-SQL", "TOP" in kw_tsql and "LIMIT" not in kw_tsql,
      kw_tsql)
check("keyword fallback keeps LIMIT elsewhere", "LIMIT" in kw_generic, kw_generic)


class FailingLLM:
    def generate_sql(self, question, schema, dialect=""):
        raise ValueError("bad generation")


fb = llm_sql_generator(FailingLLM())
out_fb = fb("show everything", schema_one, dialect=AzureSqlEngine.dialect)
check("fallback path threads dialect (degraded generation stays valid T-SQL)",
      "TOP" in out_fb and "LIMIT" not in out_fb, out_fb)


# ---- 3d. predicate probes (#476/#479): the LIMIT-form probe is a parse error on
# T-SQL, and a probe that always fails means the literal-repair rail silently never
# ran on Azure SQL / Synapse
from dbsearch.router.structured import predicate_probes

sql_w = "SELECT region FROM dw_spend WHERE LOWER(region) = LOWER('apac')"
probes_t = predicate_probes(sql_w, dialect=AzureSqlEngine.dialect)
probes_g = predicate_probes(sql_w)
check("T-SQL probes use TOP 1", bool(probes_t) and all(
    "TOP 1 1" in p and "LIMIT" not in p for _, p in probes_t), str(probes_t))
check("generic probes keep LIMIT 1", bool(probes_g) and all(
    "LIMIT 1" in p for _, p in probes_g), str(probes_g))


# ---- 3e. #717: a DEGRADED generation (model failed -> keyword fallback) must not be
# memoized — one bad roll would otherwise freeze the fallback SQL for the process
# lifetime, which is exactly what the memo's docstring promises never happens.
flaky_calls = {"n": 0}


class FlakyLLM:
    def generate_sql(self, question, schema, dialect=""):
        flaky_calls["n"] += 1
        if flaky_calls["n"] == 1:
            raise ValueError("one bad roll")
        return "SELECT region FROM dw_spend GROUP BY region"


memo_flaky = memoized_sql_generator(llm_sql_generator(FlakyLLM()))
first = memo_flaky("q717", schema_one)    # roll 1 fails -> fallback, must NOT cache
second = memo_flaky("q717", schema_one)   # roll 2 succeeds -> real SQL, cached
third = memo_flaky("q717", schema_one)    # cache hit
check("degraded fallback is not memoized (second ask re-rolls the model)",
      "GROUP BY" in second and second == third and flaky_calls["n"] == 2,
      f"first={first!r} second={second!r} calls={flaky_calls['n']}")


# ---- 4. the anthropic prompt carries the dialect ----------------------------------
from dbsearch.adapters.anthropic import sql_user_prompt

p = sql_user_prompt("q", [{"table": "sales", "columns": []}],
                    dialect="T-SQL: use SELECT TOP n, never LIMIT")
check("sql_user_prompt names the target dialect", "T-SQL" in p and "LIMIT" in p, p[-200:])
p2 = sql_user_prompt("q", [{"table": "sales", "columns": []}])
check("sql_user_prompt without dialect is unchanged shape", "dialect" not in p2.lower())


print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("selftest_714_sql_dialect: ALL PASS")
