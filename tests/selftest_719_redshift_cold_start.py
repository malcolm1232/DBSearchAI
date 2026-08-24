"""#719 - a Redshift Serverless cold start must reach the user as "waking", not a bare timeout.

WHAT WAS OBSERVED (260817, workgroup dbsearch-verify-wg, ap-southeast-1 - the design is built
on measurement, not on docs): after ~20 minutes idle, `execute_statement` did NOT raise. The
statement was accepted in 1.1s and sat SUBMITTED -> STARTED for 11.8s total while the
warehouse woke, against 2.2s warm. So there is no fast refusal to retry on (the Azure 40613
shape does not apply here - the Data API QUEUES through a resume), and the engine's own 180s
poll already survives the wake on the introspection/health paths.

The user-visible failure is the EXECUTOR's 8s ask budget (`router_service.py` timeout_s=8.0):
the first cold ask times out at 8s while the statement would have finished at ~12s, and the
outcome said only "timeout" - the user's freight store "looks broken, is just cold", and the
second ask works, which reads as flakiness.

THE FIX: the engine tracks its own idleness (`_last_finished`). When a statement is still
pending past `cold_hint_after` (default 5s) on the ASK path (`wake=False`), and the engine was
cold-idle (`cold_idle_s`, default 300s, or has never finished a statement), it records a
cold-start hint. The executor's timeout branch reads `cold_start_hint()` and attaches it as
the outcome's remedy, so the disclosure says "ask again in about a minute" instead of nothing.

WHY IDLENESS IS THE DISCRIMINATOR: a WARM engine whose query is merely slow must keep its
plain timeout - stamping "the warehouse was waking" on a slow query would be the #727 mislabel
in the other direction. First-statement-after-idle is exactly the cold-start shape.

The hint clears on a successful finish (a statement that beat the budget must not leave a
stale hint for some later, unrelated timeout) and clears on read.

    PYTHONPATH=src python3 tests/selftest_719_redshift_cold_start.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.catalog import CatalogNode, StoreCatalog, STORE  # noqa: E402
from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402
from dbsearch.router import executor  # noqa: E402
from dbsearch.router.providers.redshift import RedshiftEngine  # noqa: E402
from dbsearch.router.synthesizer import FAILED_ANSWER, no_evidence_answer  # noqa: E402


class _ScriptedApi:
    """describe_statement pops the scripted statuses; the last one sticks."""

    def __init__(self, statuses=("FINISHED",)):
        self.statuses = list(statuses)
        self.executed = []

    def execute_statement(self, **kw):
        self.executed.append(kw.get("Sql", ""))
        return {"Id": f"s{len(self.executed)}"}

    def describe_statement(self, Id):
        s = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"Status": s, "HasResultSet": s == "FINISHED"}

    def get_statement_result(self, Id):
        return {"ColumnMetadata": [{}], "Records": [[{"longValue": 1}]]}


def _eng(statuses, **kw):
    api = _ScriptedApi(statuses)
    kw.setdefault("poll_interval", 0)
    kw.setdefault("cold_hint_after", 0.0)   # the threshold clause is tested by time, below
    return RedshiftEngine(lambda: api, workgroup="wg", database="dev", **kw), api


def test_a_cold_pending_ask_records_the_hint():
    """THE FIX: cold engine, ask path, statement pending -> the hint is recorded, and the
    eventual TimeoutError still raises (the engine does not swallow the timeout)."""
    eng, _ = _eng(["SUBMITTED", "SUBMITTED", "STARTED"], timeout=0.01)
    try:
        eng.execute("SELECT 1")
    except TimeoutError:
        pass
    else:
        raise AssertionError("a statement pending past the deadline must still time out")
    hint = eng.cold_start_hint()
    assert hint and "waking" in hint and "ask again" in hint, (
        f"a cold pending ask recorded no usable hint: {hint!r}")
    assert eng.cold_start_hint() == "", "the hint must clear on read"
    print("  PASS  a cold pending ask records the waking hint (and it clears on read)")


def test_a_warm_slow_query_gets_no_hint():
    """The DISCRIMINATOR alone: same pending shape, but the engine finished a statement
    moments ago - a slow warm query must keep its plain timeout, or 'waking' becomes the
    #727 mislabel pointed the other way."""
    eng, _ = _eng(["FINISHED", "SUBMITTED"], timeout=0.01)
    eng.execute("SELECT 1")                       # finishes -> the engine is now WARM
    try:
        eng.execute("SELECT 2")                   # pending forever on the same engine
    except TimeoutError:
        pass
    assert eng.cold_start_hint() == "", (
        "a WARM engine's slow query was labeled as a cold start")
    print("  PASS  a warm slow query keeps its plain timeout (no hint)")


def test_the_wake_path_never_labels_asks():
    """Introspection (wake=True) rides its own long poll; it must not set the ask hint."""
    eng, _ = _eng(["SUBMITTED", "SUBMITTED"], timeout=0.01, tables=None)
    try:
        eng.schema()
    except TimeoutError:
        pass
    assert eng.cold_start_hint() == "", (
        "the introspection path set the ask-facing cold hint")
    print("  PASS  the wake path never sets the ask hint")


def test_a_finish_clears_the_pending_hint():
    """A statement that goes pending (hint set) but FINISHES inside the engine budget must
    clear the hint - otherwise a later unrelated timeout inherits a stale 'waking'."""
    eng, _ = _eng(["SUBMITTED", "STARTED", "FINISHED"], timeout=5)
    eng.execute("SELECT 1")
    assert eng.cold_start_hint() == "", (
        "a successful statement left a stale cold-start hint behind")
    print("  PASS  a successful finish clears the hint")


def test_the_threshold_clause_is_real():
    """cold_hint_after alone: with a HIGH threshold, a briefly-pending statement must not
    hint - the clause that stops every 300ms queue blip reading as a cold start."""
    eng, _ = _eng(["SUBMITTED", "FINISHED"], timeout=5, cold_hint_after=60.0)
    eng.execute("SELECT 1")
    assert eng.cold_start_hint() == ""
    eng2, _ = _eng(["SUBMITTED", "SUBMITTED", "STARTED"], timeout=0.01,
                   cold_hint_after=60.0)
    try:
        eng2.execute("SELECT 2")
    except TimeoutError:
        pass
    assert eng2.cold_start_hint() == "", (
        "the pending time never reached the threshold, but the hint was set anyway")
    print("  PASS  the cold_hint_after threshold is load-bearing")


class _SlowStore:
    """A store whose retrieve outlives the executor budget, exposing a cold engine."""

    def __init__(self, hint):
        class _E:
            def cold_start_hint(self_e):
                return hint
        self._engine = _E()

    def authorize(self, user_oid):
        return None

    def retrieve(self, access, question, top_k=5):
        time.sleep(0.2)
        return []

    def profile(self):
        return None


def test_executor_timeout_carries_the_hint():
    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind="tenant", parent_id=None, acl=["alice"]))
    store = _SlowStore("this source's warehouse was waking - ask again in about a minute")
    cat.register(CatalogNode(id="freight-costs", kind=STORE, parent_id="t", acl=["alice"],
                             profile=None, store=store))
    decision = RoutingDecision(query_type="analytical", stores=[
        RoutedStore(store_id="freight-costs", business_unit="ops", score=0.9)])
    report = executor.execute(cat, decision, "alice", "q", timeout_s=0.01)
    o = report.outcomes[0]
    assert o.status == executor.TIMEOUT, o
    assert "waking" in o.remedy, (
        f"the timeout outcome carries no cold-start remedy: remedy={o.remedy!r}")
    assert no_evidence_answer(decision, [o]) == FAILED_ANSWER
    print("  PASS  the executor timeout carries the engine's cold-start hint")


def test_executor_timeout_without_an_engine_is_unchanged():
    """Control: a store with no engine (document stores) times out exactly as before."""
    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind="tenant", parent_id=None, acl=["alice"]))
    store = _SlowStore("")
    del store._engine
    cat.register(CatalogNode(id="docs", kind=STORE, parent_id="t", acl=["alice"],
                             profile=None, store=store))
    decision = RoutingDecision(query_type="semantic", stores=[
        RoutedStore(store_id="docs", business_unit="hr", score=0.9)])
    report = executor.execute(cat, decision, "alice", "q", timeout_s=0.01)
    o = report.outcomes[0]
    assert o.status == executor.TIMEOUT and o.remedy == "", o
    print("  PASS  an engineless timeout is unchanged (control)")


if __name__ == "__main__":
    test_a_cold_pending_ask_records_the_hint()
    test_a_warm_slow_query_gets_no_hint()
    test_the_wake_path_never_labels_asks()
    test_a_finish_clears_the_pending_hint()
    test_the_threshold_clause_is_real()
    test_executor_timeout_carries_the_hint()
    test_executor_timeout_without_an_engine_is_unchanged()
    print("\nREDSHIFT COLD START SELF-TEST PASSED.")
