"""#288: a prefilter fan-out is scoped to the store the question DISTINCTIVELY names.

The embedding prefilter ties on shared tokens ("amount", "region"), so "total DEAL amount by
region" used to fan out to every analytical "amount" store (azure-deals + storefront + warehouse)
and the synthesizer blended their unrelated rows into one wrong total. `distinctive_narrow`
scopes such a fan-out to the single store whose profile the question distinctively names, while
leaving a genuine cross-store ask ("total amount by region", no distinctive term) fanned out.

    PYTHONPATH=src python3 tests/selftest_router_distinctive_scope.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
for _k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.server.edition import build_edition  # noqa: E402
from dbsearch.server.router_api import compose_demo_catalog  # noqa: E402


def _picked(svc, q, user="alice"):
    return [s["store_id"] for s in svc.route(user, q).to_dict().get("stores", [])]


def test_distinctive_measure_scopes_to_one_store():
    svc = compose_demo_catalog(build_edition()).service
    assert _picked(svc, "total deal amount by region") == ["azure-deals"]
    assert _picked(svc, "total order amount by region") == ["storefront"]
    assert _picked(svc, "total units by region") == ["warehouse"]
    got = _picked(svc, "average ticket resolution hours by region")
    assert got == ["support-tickets"], got
    print("  PASS  distinctive measure ('deal'/'order'/'units'/'ticket') scopes to its one store")


def test_true_cross_store_ask_still_fans_out():
    """No distinctive term (amount/region are shared) -> the fan-out is preserved, so a genuine
    cross-database aggregate still spans the stores."""
    svc = compose_demo_catalog(build_edition()).service
    picked = set(_picked(svc, "What is the total amount by region?"))
    assert len(picked) >= 2, picked
    assert {"azure-deals", "storefront"} <= picked, picked
    print("  PASS  a shared-token cross-store ask still fans out (no false narrowing)")


def test_never_widens_or_leaves_the_visible_set():
    """bob cannot see fin-ledger; narrowing must never surface it or any store bob can't see."""
    svc = compose_demo_catalog(build_edition()).service
    for q in ("total deal amount by region", "confidential revenue", "total amount by region"):
        assert "fin-ledger" not in _picked(svc, q, user="bob"), q
    print("  PASS  narrowing never widens the set or reaches an invisible store (LAW 2)")


def main():
    print("Router distinctive-scope (#288) self-test:")
    test_distinctive_measure_scopes_to_one_store()
    test_true_cross_store_ask_still_fans_out()
    test_never_widens_or_leaves_the_visible_set()
    print("\nROUTER DISTINCTIVE-SCOPE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
