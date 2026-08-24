"""#940 - a store still READING your folder must not report that the folder holds nothing.

FOUND ON PROD 260823, twice, once per deploy. `docker compose up -d api` recreates the
container; a connector store's index lives in that process (providers/connector.py:
`index = InMemoryIndex(obj)`), so it comes back empty and re-crawls. During that window the
SAME question that had just answered with three citations returned:

    "That query ran against your data and matched no records. The source is there and
     readable - it simply holds nothing that fits."

Every clause of that is a positive claim, and the last one is false: the source holds the
answer, and we had not finished reading it. It is the shape this session spent the day
removing - a confident sentence about state nobody measured - and it is the last honesty gap
on #895's "survives a deploy".

THE FIX IS NOT TO SOFTEN THE SENTENCE. `EMPTY_RESULT_ANSWER` is RIGHT when a settled store
genuinely matched nothing; that is #218's whole point, that an empty result should say WHY
rather than blame permissions. What was missing is that "still syncing" is a different why.
The executor already has the store in scope where it records EMPTY, and #939 gave
ConnectorBackedStore a public `freshness()`, so the outcome can carry it - duck-typed exactly
as `_engine` (#719) and `.idp` (#680) already are on that path.

  PYTHONPATH=src python3 tests/selftest_940_warming_store_is_not_empty.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.catalog import STORE, CatalogNode, StoreCatalog  # noqa: E402
from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402
from dbsearch.router.executor import EMPTY, StoreOutcome, execute  # noqa: E402
from dbsearch.router.store import (  # noqa: E402
    INDEXED, SEMANTIC, AccessContext, StoreProfile, StorePort,
)
from dbsearch.router.synthesizer import (  # noqa: E402
    DECLINED_ANSWER, EMPTY_RESULT_ANSWER, WARMING_ANSWER, no_evidence_answer,
)


class _Decision:
    """The router decided to ask these stores - the branch under test is what happens when
    they all come back with nothing."""
    def __init__(self, stores=("gdrive-1",), reason=""):
        self.stores = list(stores)
        self.reason = reason


def test_a_warming_store_says_it_is_still_reading():
    """THE DEFECT. The store returned nothing because the crawl has not finished, and the
    product told the user their folder holds nothing that fits."""
    out = [StoreOutcome("gdrive-1", "unassigned", EMPTY, warming=True)]
    answer = no_evidence_answer(_Decision(), out)
    assert answer == WARMING_ANSWER, answer
    assert "holds nothing" not in answer, answer


def test_a_settled_store_still_says_it_matched_nothing():
    """CONTROL, and the one a careless fix breaks. A store that HAS finished reading and
    genuinely matched nothing must keep #218's sentence - replacing it with "still syncing"
    would be the same dishonesty pointed the other way, and would tell a user to wait for
    something that already finished."""
    out = [StoreOutcome("gdrive-1", "unassigned", EMPTY, warming=False)]
    assert no_evidence_answer(_Decision(), out) == EMPTY_RESULT_ANSWER


def test_a_store_that_cannot_report_freshness_is_treated_as_settled():
    """CONTROL. `warming` defaults False, so a SQL store - which has no crawl and no
    freshness to report - keeps exactly the answer it had before this card. A default of True
    would make every federated store tell users to wait for a sync that does not exist."""
    out = [StoreOutcome("azure_sql-1", "finance", EMPTY)]
    assert no_evidence_answer(_Decision(("azure_sql-1",)), out) == EMPTY_RESULT_ANSWER


def test_one_warming_store_is_enough_to_withhold_the_claim():
    """A mixed answer cannot claim the corpus was fully searched. If ANY store consulted is
    still reading, "your data matched no records" is a statement about a search that has not
    finished - so the warming sentence wins over the settled one."""
    out = [StoreOutcome("azure_sql-1", "finance", EMPTY, warming=False),
           StoreOutcome("gdrive-1", "unassigned", EMPTY, warming=True)]
    assert no_evidence_answer(_Decision(("azure_sql-1", "gdrive-1")), out) == WARMING_ANSWER


def test_warming_does_not_override_an_honest_decline():
    """CONTROL. #211's DECLINED means a healthy store looked at the question and said it holds
    no such KIND of data - a statement about the schema, not about how much has been read. A
    warming flag must not convert that into "wait a moment", which would be advice to wait for
    an answer that is never coming."""
    out = [StoreOutcome("gdrive-1", "unassigned", "declined", warming=True)]
    assert no_evidence_answer(_Decision(), out) == DECLINED_ANSWER


def test_the_warming_sentence_claims_nothing_about_the_contents():
    """The point of the card. Whatever the wording, it must not assert what the source does or
    does not hold, and it must tell the user the one useful thing: this is temporary."""
    a = WARMING_ANSWER.lower()
    for forbidden in ("holds nothing", "matched no records", "no such data"):
        assert forbidden not in a, f"the warming answer still claims emptiness: {WARMING_ANSWER}"
    assert "sync" in a or "reading" in a, WARMING_ANSWER
    assert "again" in a or "moment" in a, (
        f"the warming answer does not tell the user it is temporary: {WARMING_ANSWER}")


# ── the OTHER half: the executor is what SETS `warming` ──────────────────────────────────
#
# The tests above exercise the sentence-choosing half. Without the ones below, deleting the
# executor's freshness read leaves every assertion above green while prod behaves exactly as
# it did before the fix - a guard that covers the easy half and calls the card done.

class _EmptyStore(StorePort):
    """A store that retrieves NOTHING. `freshness` is optional on purpose: the executor
    duck-types it, so a store without one (every federated SQL store) must come back settled."""

    def __init__(self, sid, freshness=None):
        self._id = sid
        self._bu = "unassigned"
        if freshness is not None:
            self.freshness = lambda: freshness

    def profile(self):
        return StoreProfile(store_id=self._id, title=self._id, description="",
                            kind=INDEXED, capabilities={SEMANTIC}, business_unit=self._bu)

    def authorize(self, user_oid):
        return AccessContext(user_oid=user_oid, principals=["p"])

    def retrieve(self, access, question, top_k=5):
        return []


def _run(store):
    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind="tenant", parent_id=None, acl=["p"]))
    cat.register(CatalogNode(id=store._id, kind=STORE, parent_id="t", acl=["p"],
                             profile=store.profile(), store=store))
    routed = [RoutedStore(store._id, store._bu, 1.0)]
    decision = RoutingDecision(query_type="semantic", stores=routed, candidates=routed)
    return execute(cat, decision, "u1", "what is #731")


def test_the_executor_marks_a_syncing_store_as_warming():
    """The half prod actually runs. A store whose freshness says `syncing` must produce an
    outcome that carries it, or the sentence-choosing half above never fires."""
    report = _run(_EmptyStore("gdrive-1", freshness="syncing@2026-08-23T10:00:00Z"))
    out = report.outcomes[0]
    assert out.status == EMPTY, out
    assert out.warming is True, f"the executor did not read the store's freshness: {out}"


def test_the_executor_leaves_a_settled_store_alone():
    """CONTROL. An ingested store must NOT be marked warming, or every empty answer everywhere
    turns into "ask again in a moment"."""
    report = _run(_EmptyStore("gdrive-1", freshness="ingested@2026-08-23T10:00:00Z"))
    assert report.outcomes[0].warming is False, report.outcomes[0]


def test_the_executor_tolerates_a_store_with_no_freshness_at_all():
    """CONTROL for the duck-type. A federated SQL store has no crawl and no `freshness`; the
    executor must not raise, and must treat it as settled."""
    report = _run(_EmptyStore("azure_sql-1"))
    assert report.outcomes[0].warming is False, report.outcomes[0]


if __name__ == "__main__":
    fails = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                fails.append(name)
                print(f"FAIL {name}\n     {exc}")
            except Exception as exc:
                fails.append(name)
                print(f"FAIL {name}\n     {type(exc).__name__}: {exc}")
    print(f"\n{'FAILED' if fails else 'PASSED'}: {len(fails)} failure(s)")
    sys.exit(1 if fails else 0)
