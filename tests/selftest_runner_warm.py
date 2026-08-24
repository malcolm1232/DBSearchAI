"""#491 - the golden runner warms the rig instead of discarding a whole first run.

#483 measured the first run after any server start losing 5-8 points to Ollama
cold-start - generations fail while the model loads and degrade silently to the keyword
stub - so the rule was "discard run 1", ~8-20 wasted minutes per session. `warm_rig`
issues throwaway asks until the rig answers cleanly twice IN A ROW; not-ready is
reported, never silent.

Run: PYTHONPATH=src python3 tests/selftest_runner_warm.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.eval.http_probe import warm_rig  # noqa: E402


def _script(replies):
    it = iter(replies)
    return lambda: next(it)


CLEAN = (200, {"outcomes": [{"store_id": "s", "status": "ok"}]})
DEGRADED = (200, {"outcomes": [{"store_id": "s", "status": "ok",
                                "note": "answered with the fallback query - SQL "
                                        "generation degraded (timeout)"}]})
TIMEOUT = (200, {"outcomes": [{"store_id": "s", "status": "timeout"}]})
ERROR = (500, {})


def test_two_clean_answers_in_a_row_mean_ready():
    spent, ready = warm_rig(_script([CLEAN, CLEAN]))
    assert (spent, ready) == (2, True), (spent, ready)


def test_a_degraded_answer_resets_the_streak():
    spent, ready = warm_rig(_script([CLEAN, DEGRADED, CLEAN, CLEAN]))
    assert (spent, ready) == (4, True), (spent, ready)


def test_cold_start_shapes_are_all_recognized():
    spent, ready = warm_rig(_script([ERROR, TIMEOUT, DEGRADED, CLEAN, CLEAN]))
    assert (spent, ready) == (5, True), (spent, ready)


def test_a_rig_that_never_warms_is_reported_not_silent():
    spent, ready = warm_rig(_script([DEGRADED] * 6))
    assert (spent, ready) == (6, False), (spent, ready)


def main():
    test_two_clean_answers_in_a_row_mean_ready()
    test_a_degraded_answer_resets_the_streak()
    test_cold_start_shapes_are_all_recognized()
    test_a_rig_that_never_warms_is_reported_not_silent()
    print("  PASS  #491 warm_rig: two clean in a row = ready; degraded/timeout/error "
          "reset the streak; never-ready is reported")
    print("\nRUNNER-WARM SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
