"""#680 - the drop reason the user can ACT on must reach the disclosure, not just the outcome.

FOUND IN A BROWSER, not by a test, and that is the point of this file. #676 shipped a correct
refusal: an unlinked caller's S3 store is dropped instead of answered from. selftest_676
asserts on `report.outcomes[0].error` and that assertion is TRUE - the sentence "connect Amazon
to query this source" really is in the outcome object. But `disclosure_from` formats a dropped
store as "{store_id} ({business_unit}: {status})" and never reads `.error`, so what reached the
screen on dbsearch.ai was:

    Partial coverage - unavailable and omitted: s3-1 (unassigned: error).
    I do not have that information in the provided context.

The word "Amazon" appeared nowhere on the page. A missing CREDENTIAL - the one failure the
reader can fix themselves in thirty seconds - rendered identically to a timeout, a dead host
and a bad query.

So this file asserts one layer further out than selftest_676 did: on the string the client
actually renders, `synthesize(...).to_dict()["disclosure"]`. That is the lesson this repo has
now learned four times (a permission and its prose are two channels): assert on CONTENT
REACHING THE USER, not on the object that was supposed to carry it.

WHY THE REMEDY IS CARRIED, NOT PARSED. `executor` stringifies the exception into `.error` as
"NotSignedIn: connect Amazon ...", which throws away the `.idp` attribute that made it
actionable. Sniffing the class name back out of that string in the synthesizer would be a
guess made in the wrong layer. The executor is where the exception object still exists, so
that is where the fact is known and where it gets recorded (see feedback: enforce where the
fact is known).

    PYTHONPATH=src python3 tests/selftest_680_disclosure_carries_the_remedy.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402
from dbsearch.router.executor import DispatchReport, StoreOutcome, execute  # noqa: E402
from dbsearch.router.store import StoreProfile  # noqa: E402
from dbsearch.router.synthesizer import disclosure_from, synthesize  # noqa: E402


class _Llm:
    """The synthesizer never gets this far for an all-dropped report, but a port is required."""

    def complete(self, *a, **k):
        return "unused"

    def generate(self, *a, **k):
        return "unused"


class _NotSignedIn(Exception):
    def __init__(self):
        super().__init__("connect Amazon to query this source - queries run as your own AWS "
                         "identity, and this account has no AWS credential")
        self.idp = "aws"


class _RefusingStore:
    """A store shaped like the one #676's build_unavailable returns."""

    def __init__(self, sid="s3-1"):
        self._sid = sid

    def profile(self):
        return StoreProfile(store_id=self._sid, title=self._sid, description="",
                            kind="indexed", capabilities=set(), business_unit="unassigned")

    def authorize(self, user_oid):
        raise _NotSignedIn()

    def retrieve(self, *a, **k):
        raise AssertionError("retrieve must never be reached past a refused authorize")


class _Cat:
    def __init__(self, store):
        self._store = store

    def get(self, sid):
        class _N:
            pass
        n = _N()
        n.store = self._store
        return n


def _dropped_report():
    """Drive the REAL executor so the outcome is built the way production builds it."""
    decision = RoutingDecision(query_type="semantic", stores=[
        RoutedStore(store_id="s3-1", business_unit="unassigned", score=1.0)])
    report = execute(_Cat(_RefusingStore()), decision, "alice", "what is the escalation window?")
    return report, decision


def test_the_executor_records_an_actionable_remedy_separately_from_the_error():
    """The `.idp` attribute is the signal health.py already checks for structurally. The
    executor is the last place it exists, so it is the place that must record it."""
    report, _ = _dropped_report()
    out = report.outcomes[0]
    assert out.status == "error", out
    remedy = getattr(out, "remedy", "")
    assert "connect Amazon" in remedy, (
        "the outcome carries no separate remedy field, so everything downstream has to "
        f"re-parse a stringified exception to find one: {out!r}")
    print("  PASS  the executor records the actionable remedy on the outcome")


def test_the_disclosure_names_why_not_just_which():
    """disclosure_from's own caller comments claim it says 'which store and why'. It said
    which."""
    report, _ = _dropped_report()
    line = disclosure_from(report.outcomes)
    assert "s3-1" in line, line
    assert "connect Amazon" in line, (
        "the disclosure names the store and collapses the reason to the bare status word, so "
        f"a fixable credential problem is indistinguishable from a timeout: {line!r}")
    print("  PASS  the disclosure names WHY, not just which store")


def test_the_string_the_user_actually_reads_contains_the_remedy():
    """THE ONE THAT WOULD HAVE CAUGHT #680. One layer further out than selftest_676: the
    rendered payload, not the outcome object that was supposed to carry the message."""
    report, decision = _dropped_report()
    payload = synthesize("what is the escalation window?", report, decision, _Llm()).to_dict()
    assert "connect Amazon" in payload["disclosure"], (
        "the remedy did not survive into the payload the client renders - which is exactly "
        f"how #680 passed every unit test and still failed on prod: {payload['disclosure']!r}")
    print("  PASS  the remedy survives into the rendered payload the client reads")


def test_the_omitted_list_marks_the_actionable_drop_as_not_connected():
    """The list entry and the instruction are two separate renderings and each can regress on
    its own. Without this, reverting the list to the bare status word left the test green on
    the strength of the follow-up sentence alone."""
    report, _ = _dropped_report()
    line = disclosure_from(report.outcomes)
    assert "not connected" in line, (
        "the omitted list still labels a missing credential with the generic status word, so "
        f"the list itself cannot be told apart from a timeout: {line!r}")
    print("  PASS  the omitted list marks an actionable drop as 'not connected'")


class _PlainlyBrokenStore(_RefusingStore):
    """A store that died of a real fault: no `.idp`, nothing the reader can do."""

    def authorize(self, user_oid):
        raise RuntimeError("connection reset by peer")


def test_an_ordinary_failure_gains_no_invented_remedy():
    """THE CONTROL, and it has to go through the EXECUTOR to be worth anything.

    The first version of this built a StoreOutcome by hand with no remedy and asserted the
    disclosure stayed quiet - which is true of any implementation, including one that hands a
    remedy to every exception it sees. Mutation-testing caught it: setting `remedy=str(exc)`
    unconditionally left this green. It now drives the real executor with an exception that
    carries no `.idp`, so the branch under test is actually taken."""
    decision = RoutingDecision(query_type="semantic", stores=[
        RoutedStore(store_id="azure_sql-1", business_unit="eng", score=1.0)])
    report = execute(_Cat(_PlainlyBrokenStore()), decision, "alice", "anything")
    out = report.outcomes[0]
    assert out.status == "error" and "connection reset" in out.error, out
    assert not out.remedy, (
        "a plain fault was handed a remedy, so the disclosure will read a diagnostic string "
        f"out to the user as though it were instructions: {out.remedy!r}")
    line = disclosure_from(report.outcomes)
    # Assert the PROPERTY, not the copy: the driver's own words must not be read out to the
    # user as though they were instructions. An earlier version asserted `"To use" not in
    # line`, which pins a prefix string - rename the prefix and the control silently stops
    # guarding anything, which is the shape of a test that protects nothing.
    assert "azure_sql-1 (eng: error)" in line, line
    assert "connection reset" not in line, (
        "a driver's diagnostic was read out to the user as advice - a plain fault has no "
        f"remedy, and inventing one is worse than staying quiet: {line!r}")
    print("  PASS  a plain fault gains no invented remedy (driven through the executor)")


if __name__ == "__main__":
    test_the_executor_records_an_actionable_remedy_separately_from_the_error()
    test_the_disclosure_names_why_not_just_which()
    test_the_string_the_user_actually_reads_contains_the_remedy()
    test_the_omitted_list_marks_the_actionable_drop_as_not_connected()
    test_an_ordinary_failure_gains_no_invented_remedy()
    print("ALL PASS  #680")
