"""#336 review finding 2: scripts/e2e_usecase.py must never report the capability
suite PASSED when a whole mode (user) failed to start.

Before this fix, when user-mode setup failed (missing env, a failed ROPC sign-in, a
failed fixture compose - the `except Exception` widened it beyond just missing env),
main() `continue`d past every (cap, "user") pair instead of recording anything for
it. Those pairs never entered `results`, so a green demo-only run printed e.g.
"9/9 passed" and "#337 CAPABILITY SUITE PASSED." with exit code 0, having tested
only the demo half.

The fix: a skipped mode records a FAILED CapabilityResult per (cap, mode) instead of
skipping it outright, so the suite exits non-zero and the demo-only half can never
be mistaken for the whole suite passing. The explanatory "user mode skipped: ..."
message is kept (it is genuinely useful) but must not read as success.

    PYTHONPATH=src python3 tests/selftest_e2e_usecase_skip_gate.py
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
for _k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import e2e_usecase                                        # noqa: E402
from usecase_cases import CapabilityResult                # noqa: E402


def test_user_mode_setup_failure_fails_the_suite_not_skips_it():
    """A real ROPC/compose failure (a bare Exception, not just a missing-env
    KeyError) must surface as recorded failures, never as a quietly-smaller
    passing run."""
    real_mint = e2e_usecase.mint_session
    real_run = e2e_usecase.run_capability

    def _boom(base, who="alice"):
        raise RuntimeError("ROPC sign-in failed (simulated)")

    def _fake_run_capability(cap, mode, base, session, unauth_session=None):
        # demo mode always "passes" here - the point under test is that user mode
        # never runs at all, and that absence must still fail the suite rather
        # than silently shrinking it to the demo half.
        assert mode == "demo", f"user mode must never reach run_capability here: {mode}"
        return CapabilityResult(cap.num, mode, True, "")

    e2e_usecase.mint_session = _boom
    e2e_usecase.run_capability = _fake_run_capability
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = e2e_usecase.main(["--only", "1,2"])
        out = buf.getvalue()
    finally:
        e2e_usecase.mint_session = real_mint
        e2e_usecase.run_capability = real_run

    assert rc != 0, "a failed-to-start user mode must fail the suite (non-zero exit)"
    assert "CAPABILITY SUITE PASSED" not in out, (
        "the suite must never print PASSED when a whole mode did not run:\n" + out)
    assert "user mode setup failed" in out, (
        "the explanatory skip message must still be printed:\n" + out)
    assert "1/4 passed" in out or "2/4 passed" in out or "0/4 passed" in out, (
        "both (cap, user) pairs must be RECORDED as failures, not dropped from the "
        f"tally:\n{out}")
    print("  PASS  a user-mode setup failure fails the suite and never prints PASSED")


def test_demo_only_run_is_unaffected():
    """--demo alone never touches user mode at all - that is a deliberate mode
    selection, not a skip, and must behave exactly as before."""
    real_run = e2e_usecase.run_capability

    def _fake_run_capability(cap, mode, base, session, unauth_session=None):
        assert mode == "demo"
        return CapabilityResult(cap.num, mode, True, "")

    e2e_usecase.run_capability = _fake_run_capability
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = e2e_usecase.main(["--demo", "--only", "1,2"])
        out = buf.getvalue()
    finally:
        e2e_usecase.run_capability = real_run

    assert rc == 0, out
    assert "CAPABILITY SUITE PASSED" in out, out
    assert "2/2 passed" in out, out
    print("  PASS  a deliberate --demo-only run is unaffected and still prints PASSED")


def main():
    print("e2e_usecase skip-is-not-green gate (#336 review finding 2) self-test:")
    test_user_mode_setup_failure_fails_the_suite_not_skips_it()
    test_demo_only_run_is_unaffected()
    print("\nE2E_USECASE SKIP GATE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
