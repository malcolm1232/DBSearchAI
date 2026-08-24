"""#322: the demo SQL connectors read bundled CSV fixtures. If those data files are not
declared in [tool.setuptools.package-data], `pip install` (the Docker image) drops them,
every SQL demo store fails to build with "No such file", and the hosted demo silently loses
azure-deals / storefront / warehouse / support-tickets - the canvas still shows them, but
they never route. Local dev hid this because it runs from src/ where the files exist.

This test ties the DECLARED package globs to the fixtures the demo ACTUALLY references, so a
fixture added in a path the globs don't cover fails here instead of in production.

    python3 tests/selftest_demo_fixtures_packaged.py
"""
import fnmatch
import os
import re
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

PKG = SRC / "dbsearch"


def _package_data_globs():
    """The dbsearch package-data patterns from pyproject.toml (a light parse, no toml dep)."""
    text = (ROOT / "pyproject.toml").read_text()
    block = re.search(r"\[tool\.setuptools\.package-data\].*?dbsearch\s*=\s*\[(.*?)\]",
                      text, re.S)
    assert block, "no [tool.setuptools.package-data] dbsearch list in pyproject.toml"
    return re.findall(r'"([^"]+)"', block.group(1))


def _referenced_fixtures():
    """Every fixture path the demo fleet wires, as a path relative to the dbsearch package."""
    api = (PKG / "server/router_api.py").read_text()
    # _fx("azure_sql", "sales.csv") -> router/demo/fixtures/azure_sql/sales.csv
    refs = re.findall(r'_fx\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)', api)
    assert refs, "no _fx(...) fixture references found - test is stale"
    return [f"router/demo/fixtures/{a}/{b}" for a, b in refs]


def test_every_referenced_fixture_exists_on_disk():
    missing = [r for r in _referenced_fixtures() if not (PKG / r).exists()]
    assert not missing, f"demo references fixtures that don't exist: {missing}"


def test_every_fixture_is_covered_by_a_package_data_glob():
    globs = _package_data_globs()
    for rel in _referenced_fixtures():
        assert any(fnmatch.fnmatch(rel, g) for g in globs), (
            f"{rel} is not matched by any package-data glob {globs} - it will be dropped "
            "from the wheel and the SQL demo store will silently fail to build (#322)")


def test_demo_compose_skips_nothing_from_src():
    """From src/ the files exist, so a clean compose must skip zero stores. Catches a fixture
    that is referenced but unreadable (e.g. renamed) before it reaches a deployment."""
    from dbsearch.server.edition import build_edition
    from dbsearch.server.router_api import compose_demo_catalog
    demo = compose_demo_catalog(build_edition())
    assert not demo.skipped, f"demo compose skipped stores from src: {demo.skipped}"
    ids = {n.id for n in demo.catalog.stores()}
    for must in ("azure-deals", "storefront", "warehouse", "support-tickets"):
        assert must in ids, f"{must} not composed - fixture backing broke"


def main():
    print("Demo fixture packaging self-test (#322):")
    test_every_referenced_fixture_exists_on_disk()
    print("  PASS  every _fx(...) fixture exists on disk")
    test_every_fixture_is_covered_by_a_package_data_glob()
    print("  PASS  every fixture is covered by a package-data glob (ships in the wheel)")
    test_demo_compose_skips_nothing_from_src()
    print("  PASS  demo compose skips nothing; all 4 SQL fixture stores present")
    print("\nDEMO FIXTURE PACKAGING SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
