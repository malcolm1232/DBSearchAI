"""#808 - a store that composed but can answer NOTHING says so at compose time.

THE DEFECT. A `tables:` allowlist entry is matched against the introspected tables, and a
BARE entry matches only the default schema - deliberately, because a bare name must not drag
in a same-named table from another schema; that is a different table (LAW 2). So a store
configured `tables: [freight_costs]` whose table lives in `logistics` matches nothing, and
the store composes GREEN, turns `live`, is routed to, and then declines every question. #727
made that ANSWER honest (SchemaUnavailable, with schema-qualification named in the remedy).
This makes the COMPOSE that created it honest, so the owner learns about it when they press
the button rather than by asking a question and reading a failure.

The matching rule is NOT what changes. It is right and it stays; the silence around it was
the defect. `test_a_bare_entry_still_refuses_a_non_default_schema` pins that.

BIGQUERY HAD THE SAME SILENCE FROM THE OPPOSITE DIRECTION. Its filter was a bare-name
membership test, so an operator who wrote `analytics.orders` - the shape redshift REQUIRES,
and the shape #727's own remedy tells people to use - matched nothing and silently emptied
the store. Now it accepts bare, `dataset.table` and `project.dataset.table`, but only for its
OWN dataset: `other_dataset.orders` still matches nothing, because it names a table this
engine genuinely cannot see. That is the same LAW 2 reasoning, not an exception to it.

FIVE CLAUSES, EACH WITH ITS OWN MUTATION (the #793 lesson):
  1. the allowlist warning exists and names the fix -> test_an_allowlist_that_matched_nothing_warns
  2. the no-allowlist case says something DIFFERENT -> test_an_empty_schema_without_an_allowlist_blames_privileges
  3. the wire carries it                            -> test_the_compose_summary_carries_warnings
  4. bigquery accepts the qualified forms           -> test_bigquery_accepts_a_schema_qualified_entry
  5. the canvas RENDERS it on a connected node      -> test_a_connected_node_shows_the_warning (jsdom)

    PYTHONPATH=src python3 tests/selftest_808_allowlist_warning.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _domgate  # noqa: E402  the shared jsdom gate (#792)

from dbsearch.router.providers.bigquery import BigQueryEngine  # noqa: E402
from dbsearch.router.providers.postgres import PostgresEngine  # noqa: E402
from dbsearch.router.structured import FederatedSqlStore  # noqa: E402
from dbsearch.server.router_api import _profile_summary  # noqa: E402

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_compose_reason_dom_probe.mjs"

#: the table the operator wants, living OUTSIDE the default schema - the #808 shape verbatim
ROWS = [("logistics", "freight_costs", "route", "text"),
        ("logistics", "freight_costs", "cost", "integer")]


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def execute(self, *a, **k):
        pass

    def close(self):
        pass


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)


def _pg(tables, rows=ROWS):
    return PostgresEngine(connect=lambda: _Conn(rows), tables=tables)


def _store(engine, sid="freight-costs"):
    return FederatedSqlStore(sid, "ops", "Freight", "freight shipping routes costs", engine)


def _bq(tables, rows=(("orders", "id", "INT64"),)):
    class _Job:
        def __init__(self, r):
            self._r = r

        def result(self):
            return self._r

    class _Client:
        def __init__(self, r):
            self._r = r

        def query(self, sql, job_config=None):
            return _Job(self._r)

    return BigQueryEngine(lambda: _Client(list(rows)), project="p", dataset="analytics",
                          tables=tables)


# ---- the server half ---------------------------------------------------------------------

def test_an_allowlist_that_matched_nothing_warns():
    """CLAUSE 1. The defect verbatim: green, live, and unable to answer anything."""
    p = _store(_pg(["freight_costs"])).profile()
    assert p.schema == [], "fixture is broken: the allowlist was supposed to match nothing"
    assert p.warnings, (
        "#808: a store whose allowlist matched NO tables composed with no warning at all - "
        "it turns green, gets routed to, and declines every question.")
    w = " ".join(p.warnings).lower()
    assert "allowlist" in w, f"the warning does not name the allowlist: {p.warnings!r}"
    assert "qualif" in w, (
        f"the warning does not tell the operator the fix (schema-qualify the entry), which "
        f"is the whole reason it exists: {p.warnings!r}")


def test_an_empty_schema_without_an_allowlist_blames_privileges():
    """CLAUSE 2. Independent of clause 1: no allowlist is filtering, so the cause is the
    credential, and a warning that blamed a non-existent allowlist would send the operator
    to edit config that is already correct."""
    p = _store(_pg(None, rows=[])).profile()
    assert p.warnings, "an empty schema with no allowlist warned about nothing"
    w = " ".join(p.warnings).lower()
    assert "privilege" in w, f"the warning does not point at the credential: {p.warnings!r}"
    assert "allowlist" not in w, (
        f"the warning blames an allowlist that is not configured: {p.warnings!r}")


def test_a_healthy_store_carries_no_warnings():
    """CONTROL. A fix that stamps a warning on every store is worse than the silence."""
    p = _store(_pg(["logistics.freight_costs"])).profile()
    assert [t["table"] for t in p.schema] == ["logistics.freight_costs"], p.schema
    assert p.warnings == [], f"a healthy store was warned about: {p.warnings!r}"


def test_a_bare_entry_still_refuses_a_non_default_schema():
    """THE RULE THAT MUST NOT MOVE (LAW 2). The temptation is to 'fix' #808 by making a bare
    entry match any schema - which would silently widen every allowlist in the product to
    same-named tables the operator never named."""
    eng = _pg(["freight_costs"])
    assert eng.schema() == [], (
        "a BARE allowlist entry now matches a table in a non-default schema - that is a "
        "DIFFERENT table, and the allowlist just widened itself (LAW 2)")


def test_the_compose_summary_carries_warnings():
    """CLAUSE 3. The seam that puts it on the wire. Server-side warnings that never leave the
    process are the same silence with extra steps."""
    warned = _profile_summary(_store(_pg(["freight_costs"])).profile())
    assert "warnings" in warned, (
        f"the compose per-store entry has no `warnings` key, so the canvas cannot render "
        f"one: {sorted(warned)}")
    assert warned["warnings"], "the warning was computed but dropped on the way to the wire"
    healthy = _profile_summary(_store(_pg(["logistics.freight_costs"])).profile())
    assert healthy["warnings"] == [], f"a healthy store put warnings on the wire: {healthy}"


def test_every_sql_engine_records_its_allowlist_where_the_warning_looks():
    """The duck-typed assumption, made checkable. `empty_schema_warnings` reads `_allow` off
    the engine because the engines share no base class; a rename would silently downgrade
    every allowlist warning to the generic privileges one, which is a WRONG remedy rather
    than a missing one. This fails loudly instead."""
    from dbsearch.router.structured import _ALLOWLIST_ATTR

    seen = []
    for mod, cls in (("redshift", "RedshiftEngine"), ("bigquery", "BigQueryEngine"),
                     ("postgres", "PostgresEngine"), ("mysql", "MySqlEngine"),
                     ("azure_sql", "AzureSqlEngine")):
        m = __import__(f"dbsearch.router.providers.{mod}", fromlist=[cls])
        engine_cls = getattr(m, cls, None)
        if engine_cls is None:
            continue
        seen.append(cls)
    assert len(seen) >= 4, f"only found {seen} - the engine sweep is not reaching the rail"
    # the attribute itself, on a real instance of the two that need no live connection
    for eng in (_pg(["x"]), _bq(["x"])):
        assert getattr(eng, _ALLOWLIST_ATTR, None), (
            f"{type(eng).__name__} does not record its allowlist under {_ALLOWLIST_ATTR!r} - "
            f"empty_schema_warnings would silently blame the credential instead")


# ---- bigquery's inverse trap --------------------------------------------------------------

def test_bigquery_accepts_a_schema_qualified_entry():
    """CLAUSE 4. The honest form - the one redshift REQUIRES and #727's remedy recommends -
    used to silently empty a bigquery store."""
    for entry in ("analytics.orders", "p.analytics.orders", "orders"):
        tables = [t["table"] for t in _bq([entry]).schema()]
        assert tables == ["orders"], (
            f"#808 (inverse): bigquery allowlist entry {entry!r} matched nothing, so the "
            f"store composes green and answers nothing. Saw {tables!r}")


def test_bigquery_still_refuses_a_foreign_dataset():
    """CONTROL for clause 4, and the LAW 2 half: accepting qualified forms must not accept
    a qualifier naming a dataset this engine cannot see."""
    assert _bq(["other_dataset.orders"]).schema() == [], (
        "a qualifier naming a DIFFERENT dataset matched this engine's table - the allowlist "
        "just widened itself past the dataset it is scoped to")


# ---- the canvas half (jsdom) ---------------------------------------------------------------

_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the #808 compose-warning DOM check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the compose surface ({scenario})")
    return _domgate.resolve(_dom[scenario])


def _node(r, store_id):
    n = next((n for n in r["nodes"] if n["id"] == store_id), None)
    assert n is not None, f"store {store_id} never rendered as a canvas node"
    return n


def test_a_connected_node_shows_the_warning():
    """CLAUSE 5. The node is CONNECTED, so `.nreason` (gated on "planned") can never carry
    this - before #808 the card had no element for it at all."""
    r = _report("warned")
    if r is None:
        return
    warned = _node(r, "azure_sql-1")
    assert warned["dotTitle"] and warned["dotTitle"].startswith("connected"), (
        f"fixture is broken - this node must be CONNECTED for the test to mean anything: "
        f"{warned['dotTitle']!r}")
    assert warned["warnText"], (
        "#808: a live store that can answer NOTHING rendered no warning on its card. The "
        "owner's only way to find out is to ask a question and read the failure.")
    assert "allowlist" in warned["warnText"].lower(), warned["warnText"]
    assert r["composeBtn"] and "needs attention" in r["composeBtn"], (
        f"the compose button counted the warned store as plain 'live': {r['composeBtn']!r}")
    print("  PASS  a connected-but-empty store warns on its card and in the button")


def test_the_other_connected_nodes_are_not_warned():
    """CONTROL for clause 5 - both the empty-list and the absent-key shapes, because a
    render gated on `node.warnings.length` and one gated on `node.warnings` differ exactly
    there, and the wire produces both."""
    r = _report("warned")
    if r is None:
        return
    for sid in ("rds_postgres-1", "sharepoint"):
        n = _node(r, sid)
        assert not n["warnText"], f"{sid} was warned about with no warning on the wire: {n}"
    print("  PASS  healthy connected nodes carry no warning element")


def test_a_clean_compose_warns_about_nothing():
    """CONTROL: the scenario where every store is healthy must render no warning anywhere."""
    r = _report("clean")
    if r is None:
        return
    assert not any(n["warnText"] for n in r["nodes"]), (
        f"a clean compose stamped warnings on healthy nodes: {r['nodes']}")
    assert "needs attention" not in (r["composeBtn"] or ""), r["composeBtn"]
    print("  PASS  a clean compose warns about nothing")


def test_the_warning_cannot_inject(then=None):
    """#786's lesson applied to the NEW sinks. The warning is server text landing in a title
    ATTRIBUTE and in element CONTENT, so the fixture carries `\"` and `<img onerror>` in one
    string - and the evidence is what the DOM actually built, not whether the text looks
    escaped (a guard reading textContent gets GREENER as the surface gets less safe)."""
    r = _report("warned")
    if r is None:
        return
    assert r["injected_imgs"] == 0, (
        f"the warning string created {r['injected_imgs']} <img> element(s) - it reached "
        f"innerHTML unescaped")
    assert r["handler_attrs"] == [], (
        f"the warning string created event handlers: {r['handler_attrs']}")
    print("  PASS  a hostile warning creates no elements and no handlers")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}\n        {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
