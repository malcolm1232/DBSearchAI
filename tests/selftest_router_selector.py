"""Phase E E2 — hybrid selector self-test (single / fan-out / tiebreak / fallback).
Run: python3 tests/selftest_router_selector.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.decision import RoutedStore  # noqa: E402
from dbsearch.router.selector import select_stores  # noqa: E402


def test_empty_is_fallback():
    sel, method, conf = select_stores([])
    assert sel == [] and method == "fallback" and conf == 0.0


def test_dominant_single():
    scored = [RoutedStore("a", "x", 0.9), RoutedStore("b", "y", 0.4)]
    sel, method, conf = select_stores(scored, margin=0.15)
    assert [s.store_id for s in sel] == ["a"], sel
    assert method == "prefilter" and abs(conf - 0.9) < 1e-9


def test_ambiguous_fans_out():
    scored = [RoutedStore("a", "x", 0.80), RoutedStore("b", "y", 0.78), RoutedStore("c", "z", 0.20)]
    sel, method, conf = select_stores(scored, margin=0.15, floor_frac=0.6, fanout_cap=3)
    # a and b are close (< margin) and both above floor (0.6*0.8=0.48); c is dropped
    assert {s.store_id for s in sel} == {"a", "b"}, sel
    assert method == "prefilter", method


def test_fanout_cap():
    scored = [RoutedStore("a", "x", 0.80), RoutedStore("b", "y", 0.79),
              RoutedStore("c", "z", 0.78), RoutedStore("d", "w", 0.77)]
    sel, method, conf = select_stores(scored, margin=0.15, floor_frac=0.5, fanout_cap=2)
    assert len(sel) == 2, sel   # capped


def test_llm_tiebreak_used():
    scored = [RoutedStore("a", "x", 0.80), RoutedStore("b", "y", 0.78)]
    picked = {"calls": 0}
    def tiebreak(ids):
        picked["calls"] += 1
        return ["b"]   # LLM insists on b
    sel, method, conf = select_stores(scored, margin=0.15, tiebreak=tiebreak)
    assert method == "llm" and picked["calls"] == 1, method
    assert [s.store_id for s in sel] == ["b"], sel


def test_tiebreak_bad_output_falls_back():
    scored = [RoutedStore("a", "x", 0.80), RoutedStore("b", "y", 0.78)]
    sel, method, conf = select_stores(scored, margin=0.15, tiebreak=lambda ids: ["nonexistent"])
    # unknown id -> ignore the tiebreak, keep the prefilter fan-out pool
    assert {s.store_id for s in sel} == {"a", "b"}, sel
    assert method == "prefilter", method


def main():
    print("Phase E E2 selector self-test:")
    test_empty_is_fallback()
    test_dominant_single()
    test_ambiguous_fans_out()
    test_fanout_cap()
    test_llm_tiebreak_used()
    test_tiebreak_bad_output_falls_back()
    print("  PASS  fallback / dominant / fan-out / cap / llm tiebreak / bad-tiebreak fallback")
    print("\nE2 SELECTOR SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
