"""#156 regression: RLS filter-value escaping + NULL-guard in provision_sql_users.pick_filter.

Drives pick_filter() with a fake pyodbc-shaped cursor (no Azure, no pyodbc import — the
module only imports pyodbc inside admin_conn(), so importing it here is safe). Proves:
  1. an apostrophe in the discovered value gets doubled, not left to break/inject the DDL
  2. a NULL discovered value falls through to the int-modulo predicate, not `N'None'`
  3. a plain value still produces the simple `@v = N'<value>'` predicate

Run: PYTHONPATH=src python3 tests/selftest_provision_rls_escaping.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from provision_sql_users import pick_filter  # noqa: E402

COLS = [("region", "nvarchar", 50)]
INT_COLS = [("region", "nvarchar", 50), ("segment_id", "int", None)]


class FakeCursor:
    """Records SELECTs it was given; returns canned rows in call order."""

    def __init__(self, cols_row, top1_value):
        self._cols_row = cols_row
        self._top1_value = top1_value
        self.executed = []
        self._last = None

    def execute(self, sql):
        self.executed.append(sql)
        self._last = "cols" if "INFORMATION_SCHEMA.COLUMNS" in sql else "top1"

    def fetchall(self):
        assert self._last == "cols"
        return self._cols_row

    def fetchone(self):
        assert self._last == "top1"
        return (self._top1_value,)


def test_apostrophe_value_is_escaped():
    cur = FakeCursor(COLS, "Côte d'Ivoire")
    _param_decl, _colref, predicate, colname = pick_filter(cur)
    assert colname == "region", colname
    assert "N'Côte d''Ivoire'" in predicate, predicate
    # a lone unescaped d'Ivoire' (single quote, no doubling) would prematurely close the
    # DDL string literal — make sure that broken form is NOT what we produced.
    assert "d'Ivoire'" not in predicate.replace("d''Ivoire'", ""), predicate
    print(f"  PASS  apostrophe value escaped -> predicate={predicate!r}")


def test_none_value_falls_through_to_int_modulo():
    cur = FakeCursor(INT_COLS, None)
    _param_decl, colref, predicate, colname = pick_filter(cur)
    assert "N'None'" not in predicate, predicate
    assert "% 2 = 0" in predicate, predicate
    assert colname == "segment_id", colname
    assert colref == "[segment_id]", colref
    print(f"  PASS  NULL discovered value falls through -> predicate={predicate!r} col={colname!r}")


def test_plain_value_unaffected():
    cur = FakeCursor(COLS, "EMEA")
    _param_decl, _colref, predicate, colname = pick_filter(cur)
    assert predicate == "@v = N'EMEA'", predicate
    assert colname == "region", colname
    print(f"  PASS  plain value unchanged -> predicate={predicate!r}")


def main():
    print("provision_sql_users RLS-escaping self-test:")
    test_apostrophe_value_is_escaped()
    test_none_value_falls_through_to_int_modulo()
    test_plain_value_unaffected()
    print("\nPROVISION RLS ESCAPING SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
