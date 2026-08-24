"""Cluster gate + scorecard (spec 2026-07-31 sections 3 and 6).

    python3 tests/selftest_golden_gate.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.eval.golden import gate, scorecard  # noqa: E402


def _row(rid, cluster, passed, cap="A", tags=("plain",), mode="demo"):
    return {"id": rid, "cluster": cluster, "capability": cap, "hardness": list(tags),
            "mode": mode, "passed": passed, "attribution": "pass" if passed else "retrieval-miss",
            "stage1": {}, "stage2": {}}


def test_variant_cluster_counts_once():
    base = [_row("A-1", "A-1", True), _row("A-1v1", "A-1", True), _row("A-1v2", "A-1", True),
            _row("A-2", "A-2", True)]
    cur = [_row("A-1", "A-1", False), _row("A-1v1", "A-1", False), _row("A-1v2", "A-1", False),
           _row("A-2", "A-2", True)]
    result = gate.compare(cur, base)
    assert not result.red  # one cluster lost = within max_lost_per_slice=1


def test_two_clusters_lost_goes_red():
    base = [_row("A-1", "A-1", True), _row("A-2", "A-2", True), _row("A-3", "A-3", True)]
    cur = [_row("A-1", "A-1", False), _row("A-2", "A-2", False), _row("A-3", "A-3", True)]
    result = gate.compare(cur, base)
    assert result.red and "A|plain|demo" in result.regressions[0]
    # MINOR-6: the regression names each cluster's CURRENT failure attribution.
    assert "A-1 (retrieval-miss)" in result.regressions[0]
    assert "A-2 (retrieval-miss)" in result.regressions[0]


def test_capability_aggregate_three_clusters_three_slices_goes_red():
    """One cluster newly failing per slice never trips the slice bound (1 each), but
    three different slices of the SAME capability push the capability's dedup'd
    newly-failed count to 3, over the default max_lost_per_capability=2."""
    base = [_row("A-1", "A-1", True, tags=("t1",)),
            _row("A-2", "A-2", True, tags=("t2",)),
            _row("A-3", "A-3", True, tags=("t3",))]
    cur = [_row("A-1", "A-1", False, tags=("t1",)),
           _row("A-2", "A-2", False, tags=("t2",)),
           _row("A-3", "A-3", False, tags=("t3",))]
    result = gate.compare(cur, base)
    assert not any("slice bound" in r for r in result.regressions)
    assert result.red
    assert any("capability A" in r and "capability bound 2 exceeded" in r
              for r in result.regressions)


def test_capability_aggregate_boundary_two_stays_green():
    """Exactly max_lost_per_capability (2) newly-failed clusters across two slices of
    one capability stays green: at the bound, not over it."""
    base = [_row("A-1", "A-1", True, tags=("t1",)),
            _row("A-2", "A-2", True, tags=("t2",)),
            _row("A-3", "A-3", True, tags=("t3",))]
    cur = [_row("A-1", "A-1", False, tags=("t1",)),
           _row("A-2", "A-2", False, tags=("t2",)),
           _row("A-3", "A-3", True, tags=("t3",))]
    result = gate.compare(cur, base)
    assert not result.red


def test_already_failed_in_baseline_is_not_a_regression():
    base = [_row("A-1", "A-1", False), _row("A-2", "A-2", False), _row("A-3", "A-3", True)]
    cur = [_row("A-1", "A-1", False), _row("A-2", "A-2", False), _row("A-3", "A-3", True)]
    assert not gate.compare(cur, base).red


def test_key_mismatch_refused():
    k1 = scorecard.baseline_key("hermetic-lexical", "hashing", "extractive", "abc")
    k2 = scorecard.baseline_key("hermetic-lexical", "hashing", "extractive", "DIFFERENT")
    try:
        gate.check_keys(k1, k2)
        raise AssertionError("mismatched keys accepted")
    except ValueError as e:
        assert "pack_hash" in str(e)


def test_scorecard_baseline_round_trip():
    rows = [_row("A-1", "A-1", True), _row("B-1", "B-1", False, cap="B")]
    key = scorecard.baseline_key("hermetic-lexical", "hashing", "extractive", "abc123def456")
    card = scorecard.build_scorecard(rows, key)
    assert card["slices"]["A|plain|demo"]["passed"] == 1
    assert card["slices"]["B|plain|demo"]["failed_clusters"] == ["B-1"]
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        scorecard.save_baseline(card, d)
        assert scorecard.load_baseline(d, key)["key"] == key
        other = scorecard.baseline_key("semantic", "hashing", "extractive", "abc123def456")
        assert scorecard.load_baseline(d, other) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    print("selftest_golden_gate: all green")
