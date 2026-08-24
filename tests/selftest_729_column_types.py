"""#729(a) - the evidence payload learns the COLUMN TYPE, so the rail can be read.

WHY THIS EXISTS AT ALL. The Sources rail renders a row as `TotalDue=43962.7901,
period=2024.06` and a reader has to squint at it. The obvious fix - group values that carry a
decimal point - shipped, was handed to an independent reviewer, and was falsified in the exact
shape it was written to guarantee: `app_version=2024.1.0` became `2,024.1.0`. Every narrower
version leaks too, because **`period=2024.06` and `price=1200.50` are the same string**. No
rule reading the value can separate them; only the column's declared type can.

So the client formatter was REMOVED rather than narrowed - the rail's job is to be checkable
against the source, and a value the reader cannot paste back into a query is a worse defect
than an ugly one. This is the other half: the type is knowable at the point the row is
flattened and nowhere downstream, so it is settled there and carried, and the render becomes a
lookup instead of a guess.

The rule this follows is #481's, one layer up: "the server KNOWS the type, so this is settled
here rather than argued with the model" - `strip_string_ops_on_numeric` reads the same schema
for the same reason, and `_numeric_columns`' certainty rule is reused verbatim.

WHAT IS DELIBERATELY NOT DONE, and asserted below so it stays that way:
  - integers are never grouped (nothing in a type separates a count from an identifier)
  - no currency symbol is invented, and no digit is rounded away
  - an unresolvable column carries no type and renders exactly as it did before

    PYTHONPATH=src python3 tests/selftest_729_column_types.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.store import AccessContext  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    FederatedSqlStore, SqliteEngine, value_classes)

ACCESS = AccessContext(user_oid="u1", principals=[])

#: Real `information_schema.data_type` strings, one dialect per line - these are what every
#: engine's schema() already returns (Azure SQL / Postgres / MySQL / BigQuery / Redshift).
#: Cursor type codes are NOT used and this is why: they are integers in some drivers, absent
#: in others, and identical across the money/identifier split that matters here.
DIALECTS = [
    ("money", "num"), ("decimal(19,4)", "num"), ("numeric", "num"), ("smallmoney", "num"),
    ("float", "num"), ("double precision", "num"), ("real", "num"), ("NUMERIC", "num"),
    ("BIGNUMERIC", "num"), ("FLOAT64", "num"), ("number(12,2)", "num"),
    ("date", "date"), ("DATE", "date"),
    ("timestamp", "ts"), ("timestamp with time zone", "ts"), ("datetime2", "ts"),
    ("datetimeoffset", "ts"), ("smalldatetime", "ts"), ("DATETIME", "ts"), ("TIMESTAMP", "ts"),
    # NOT classified: an integer is a count or an identifier and the type cannot say which.
    ("int", None), ("bigint", None), ("INT64", None), ("int4", None), ("smallint", None),
    ("serial", None), ("tinyint", None),
    # NOT classified: text of every spelling.
    ("varchar(50)", None), ("nvarchar(max)", None), ("text", None), ("STRING", None),
    ("char(2)", None), ("uuid", None), ("boolean", None), ("bit", None), ("", None),
]


def _one(dtype):
    return value_classes([{"table": "t", "columns": [{"name": "c", "type": dtype}]}]).get("c")


def test_every_dialects_declared_type_is_read_the_same_way():
    """One table per row of DIALECTS, because the classifier is the whole load-bearing part
    and a single dialect's spelling proves nothing about the next connector's."""
    wrong = [(d, want, _one(d)) for d, want in DIALECTS if _one(d) != want]
    assert not wrong, "declared types read wrongly (type, expected, got): " + repr(wrong)


def test_a_datetime_is_never_read_as_a_date():
    """Ordering, asserted rather than assumed: `datetime` and `smalldatetime` both CONTAIN
    "date", so a date-first classifier would strip a real instant's midnight and call it a
    driver artefact. That is the one way this feature can delete data."""
    for spelling in ("datetime", "datetime2", "smalldatetime", "DATETIME",
                     "timestamp without time zone"):
        assert _one(spelling) == "ts", f"{spelling!r} was read as {_one(spelling)!r}"


def test_an_integer_is_never_grouped_however_it_is_spelled():
    """`bigint` and `smallint` both contain "int"; `int4` and `INT64` do not end there. None of
    them may be classed "num": `customer_id=29485` rendered as `29,485` is the falsification
    that killed the first attempt, arriving from the other direction."""
    for spelling in ("int", "int4", "int8", "INT64", "bigint", "smallint", "tinyint",
                     "integer", "serial", "bigserial"):
        assert _one(spelling) is None, f"{spelling!r} was classed {_one(spelling)!r}"


def test_a_name_typed_two_ways_across_tables_is_refused():
    """`_numeric_columns`' rule, and for its reason: without full alias resolution a name that
    is numeric in one table and text in another cannot be attributed, so it is dropped rather
    than guessed. Being wrong here would rewrite a value; being silent only loses a comma."""
    schema = [{"table": "a", "columns": [{"name": "code", "type": "decimal(9,2)"},
                                         {"name": "amount", "type": "money"}]},
              {"table": "b", "columns": [{"name": "code", "type": "varchar(9)"},
                                         {"name": "amount", "type": "numeric"}]}]
    got = value_classes(schema)
    assert "code" not in got, f"an ambiguous column was typed anyway: {got}"
    assert got.get("amount") == "num", f"an unambiguous column was lost with it: {got}"


def test_an_empty_or_broken_schema_is_not_an_error():
    """This runs on every SQL ask. A connector that returns nothing, None, or a column with no
    name must cost the answer a formatting opportunity, never the answer."""
    assert value_classes([]) == {}
    assert value_classes(None) == {}
    assert value_classes([{"table": "t"}]) == {}
    assert value_classes([{"table": "t", "columns": [{"type": "money"}]}]) == {}


# ---- the payload, end to end through a real engine ------------------------------------------

#: `_sniffed_types` declares a whole-number column INTEGER and a fractional one REAL, so this
#: rig produces the money/identifier split for real rather than by fixture.
TABLES = {
    "sales": {"columns": ["customer_id", "TotalDue", "region"],
              "rows": [[29485, 43962.7901, "EMEA"], [30112, 1200.50, "APAC"]]},
}


def _evidence(sql):
    store = FederatedSqlStore("aw-sales", "finance", "Sales", "orders and totals",
                              SqliteEngine.from_tables(TABLES),
                              sql_generator=lambda *a, **k: sql)
    return store.retrieve(ACCESS, "anything", top_k=5)


def test_the_evidence_carries_the_types_of_the_columns_it_returned():
    ev = _evidence("SELECT customer_id, TotalDue, region FROM sales")
    assert ev, "the rig returned no evidence at all"
    types = (ev[0].provenance or {}).get("column_types") or {}
    assert types.get("TotalDue") == "num", f"the measure carries no type: {types}"
    assert "customer_id" not in types, f"an integer identifier was typed: {types}"
    assert "region" not in types, f"a text column was typed: {types}"


def test_the_raw_value_is_never_rewritten_on_its_way_out():
    """The types ride in PROVENANCE, never in `content`. `content` is what the MODEL is shown
    and what the rail must stay checkable against, so the digits the database returned reach
    every consumer unchanged - the formatting is a render decision and stays one."""
    ev = _evidence("SELECT customer_id, TotalDue, region FROM sales")
    assert "TotalDue=43962.7901" in ev[0].content, ev[0].content
    assert "," not in ev[0].content.split("TotalDue=")[1].split(",")[0], ev[0].content


def test_an_alias_the_schema_cannot_resolve_carries_no_type():
    """`SUM(TotalDue) AS total` returns a column no schema declares. It must be absent from the
    map - the render then leaves it exactly as it arrived, which is the pre-#729(a) behaviour
    and the only honest answer when the type is genuinely unknown."""
    ev = _evidence("SELECT SUM(TotalDue) AS total FROM sales")
    assert ev, "the rig returned no evidence at all"
    types = (ev[0].provenance or {}).get("column_types") or {}
    assert "total" not in types, f"an alias was typed on a guess: {types}"


def test_a_store_with_nothing_to_type_omits_the_key_entirely():
    """No entry rather than an empty dict: the payload travels to a browser on every ask, and
    a key that is always there and always empty is noise on every citation forever."""
    ev = _evidence("SELECT customer_id, region FROM sales")
    assert "column_types" not in (ev[0].provenance or {}), ev[0].provenance


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
