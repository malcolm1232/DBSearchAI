"""#804 - "Compose failed: ..." must release the button, not become its name.

composeUp's success branch restores the Compose-up label after 2.6 seconds; the failure
branch set `btn.textContent = "Compose failed: ..."` and never restored anything, so one
failed compose renamed the button until a full page reload - no retry affordance, and the
error string sitting where an action belongs. Watched live on prod today (this session's
accidental ask-triggered compose left two nodes failing and the button stuck).

Two halves, both driven through the real canvas bundle in jsdom (the #810 probe harness,
same file, new scenario):

  - the failure IS shown: immediately after a failed compose the button reads
    "Compose failed: <detail>" - the error is real information and must not flash away;
  - and then RELEASED: after the failure-path restore timer the button offers
    "Compose up" again, exactly like the success path does.

    PYTHONPATH=src python3 tests/selftest_804_compose_failed_button.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402

CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
PROBE = ROOT / "tests/canvas_stale_reason_dom_probe.mjs"

_dom = {}


def _report(scenario):
    if scenario not in _dom:
        if not _domgate.gate(f"the compose-failed button check ({scenario})"):
            _dom[scenario] = None
        else:
            _dom[scenario] = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(CANVAS), scenario],
                f"the compose-failed button lifecycle ({scenario})")
    return _domgate.resolve(_dom[scenario])


def test_the_failure_is_shown_with_its_reason():
    out = _report("failed_compose_button_restores")
    assert out["afterFailureText"].startswith("Compose failed:"), out
    assert "tenant" in out["afterFailureText"], (
        f"the server's detail must reach the label: {out['afterFailureText']!r}")


def test_the_button_is_released_after_the_failure():
    out = _report("failed_compose_button_restores")
    assert "Compose up" in out["afterWaitText"], (
        f"a failed compose renamed the button permanently: {out['afterWaitText']!r} - "
        "no retry affordance until a full reload")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
