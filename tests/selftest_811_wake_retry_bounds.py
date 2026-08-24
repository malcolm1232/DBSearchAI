"""#811 - the Azure 40613 wake-retry says which of the two things went wrong.

FOUND in wave 2's #780 prod audit: Test connection on an azure_sql store with a nonexistent
server name took 120,225ms to fail and surfaced the driver's raw `(40613, b'...')` tuple.

WHY IT CANNOT JUST "DETECT" A BAD NAME. Azure's gateway answers 40613 - "Database 'x' on
server 'y' is not currently available" - for a paused serverless database that is resuming
AND for a database that does not exist, with the same code and the same sentence. There is no
field to discriminate on; a resuming database eventually connects and a nonexistent one never
does, so the only way to tell them apart is to wait out the budget. Waiting is therefore
correct. What was wrong is what happened at the end of the wait: the raw driver tuple, which
tells a user who fat-fingered a database name to keep waiting for a resume that is never
coming. Contrast #719's redshift cold start, where idleness genuinely discriminates - that
mechanism does NOT apply here and is deliberately not copied.

THREE CLAUSES, THREE MUTATIONS (the #793 lesson):
  1. exhaustion names BOTH readings  -> test_exhaustion_names_both_readings
  2. the budget is configurable      -> test_the_wake_budget_is_reachable_from_config
  3. ONE budget per call             -> test_a_reconnect_shares_the_calls_budget

    PYTHONPATH=src python3 tests/selftest_811_wake_retry_bounds.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.providers.azure_sql import (  # noqa: E402
    AzureDatabaseUnavailable, AzureSqlEngine)

#: Azure's real 40613 text, verbatim in shape - the SAME sentence for both causes.
FORTY_SIX_THIRTEEN = ("(40613, b\"Database 'nosuchdb' on server 'x.database.windows.net' is "
                      "not currently available. Please retry the connection later.\")")

BASE_CONFIG = {"server": "s.database.windows.net", "database": "d",
               "user": "u", "password": "p"}


def _always_40613(counter=None):
    def connect():
        if counter is not None:
            counter["n"] += 1
        raise Exception(FORTY_SIX_THIRTEEN)
    return connect


def test_exhaustion_names_both_readings():
    """CLAUSE 1. The defect verbatim: two minutes, then the driver's raw bytes."""
    eng = AzureSqlEngine(connect=_always_40613(), resume_wait=0.01, resume_timeout=0.15)
    try:
        eng.schema()
    except AzureDatabaseUnavailable as exc:
        msg = str(exc)
    except Exception as exc:                       # pre-fix: the raw driver exception
        raise AssertionError(
            f"#811: the wake-retry re-raised the driver error unchanged, so the user waits "
            f"the whole budget and is then shown {str(exc)[:80]!r}")
    else:
        raise AssertionError("the exhausted wake-retry did not raise at all")

    low = msg.lower()
    assert "auto-pause" in low or "resum" in low, (
        f"the message does not offer the resuming reading: {msg!r}")
    assert "name" in low, (
        f"the message does not offer the wrong-name reading, which is the one the user in "
        f"#780 actually hit: {msg!r}")
    assert "40613, b" not in msg, (
        f"the raw driver tuple is still what the user reads: {msg!r}")


def test_the_original_error_is_still_the_cause():
    """The honest message must not COST the diagnosis - an operator reading logs still needs
    the driver's own text, so it rides as __cause__ rather than being discarded."""
    eng = AzureSqlEngine(connect=_always_40613(), resume_wait=0.01, resume_timeout=0.1)
    try:
        eng.schema()
    except AzureDatabaseUnavailable as exc:
        assert exc.__cause__ is not None and "40613" in str(exc.__cause__), (
            "the driver's original error was thrown away instead of chained")


def test_a_non_40613_error_still_raises_immediately():
    """CONTROL. The retry loop is for 40613 ONLY. A wrong password must fail at once, not
    after two minutes of retrying something that will never change."""
    def bad_password():
        raise Exception("(18456, b\"Login failed for user 'u'.\")")

    eng = AzureSqlEngine(connect=bad_password, resume_wait=5.0, resume_timeout=120.0)
    t0 = time.monotonic()
    try:
        eng.schema()
        raise AssertionError("a login failure did not raise")
    except AzureDatabaseUnavailable:
        raise AssertionError("a login failure was misreported as a wake-retry timeout")
    except Exception:
        pass
    assert time.monotonic() - t0 < 1.0, "a non-40613 error was retried instead of raised"


def test_the_wake_budget_is_reachable_from_config():
    """CLAUSE 2. It was constructor-only, so nothing in the product could set it: every
    deployment got 120s at 5s steps, including on a store whose name was simply wrong."""
    tuned = AzureSqlEngine.from_config(dict(BASE_CONFIG, resume_wait=1, resume_timeout=9))
    assert (tuned._resume_wait, tuned._resume_timeout) == (1.0, 9.0), (
        f"#811: from_config ignored the wake-budget keys, so the 120s default is still the "
        f"only possible value ({tuned._resume_wait}, {tuned._resume_timeout})")

    default = AzureSqlEngine.from_config(dict(BASE_CONFIG))
    assert (default._resume_wait, default._resume_timeout) == (5.0, 120.0), (
        f"an absent key changed the default - every existing deployment would shift "
        f"behaviour on upgrade ({default._resume_wait}, {default._resume_timeout})")


def test_a_nonsense_budget_is_refused_loudly():
    """A typo'd budget must not silently become a default - that is the #815 shape (a bad
    value impersonating a working one)."""
    try:
        AzureSqlEngine.from_config(dict(BASE_CONFIG, resume_timeout="soon"))
        raise AssertionError("a non-numeric wake budget was accepted silently")
    except ValueError as exc:
        assert "resume_timeout" in str(exc), str(exc)


def test_a_reconnect_shares_the_calls_budget():
    """CLAUSE 3. A statement that opens a connection and then loses it reconnects - and both
    _open sites used to compute their OWN deadline, so one query could spend the full budget
    twice while the ask path's own timeout expired and a worker thread kept burning."""
    class _Cur:
        def execute(self, *a, **k):
            raise Exception("connection severed by auto-pause")

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    # THE FIRST OPEN MUST BURN MOST OF THE BUDGET, or this fixture cannot reach the defect
    # at all. The first version let the first open succeed instantly, so it consumed none of
    # the deadline and a reconnect that restarted the budget cost the same wall-clock as one
    # that shared it - the mutation SURVIVED and the guard was decorative. So: wake-retry for
    # most of the budget, then connect, then lose the connection, then never reconnect.
    budget, wait = 0.6, 0.02
    burn = 22                              # 22 * 0.02 = 0.44s of a 0.6s budget
    state = {"phase": 1, "fails": 0}

    def flaky():
        if state["phase"] == 1:
            state["fails"] += 1
            if state["fails"] <= burn:
                raise Exception(FORTY_SIX_THIRTEEN)
            state["phase"] = 2
            return _Conn()                 # opens at last, with ~0.16s of budget left
        raise Exception(FORTY_SIX_THIRTEEN)  # every reconnect hits 40613 forever

    eng = AzureSqlEngine(connect=flaky, resume_wait=wait, resume_timeout=budget)
    t0 = time.monotonic()
    try:
        eng._run("SELECT 1")
    except AzureDatabaseUnavailable:
        pass
    elapsed = time.monotonic() - t0
    assert state["phase"] == 2 and state["fails"] > burn, (
        f"fixture never reached the reconnect path: {state}")
    # sharing the deadline lands near `budget`; restarting it lands near `budget * 2`
    assert elapsed < budget * 1.35, (
        f"#811: one call spent {elapsed:.2f}s against a {budget:.2f}s budget - the reconnect "
        f"started a SECOND full budget instead of sharing this call's deadline")


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
