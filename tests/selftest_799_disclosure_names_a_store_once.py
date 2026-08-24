"""#799 - a disclosure line must name each store ONCE, however many sub-questions it answered.

FOUND IN A BROWSER ON PROD, on the first compound ask asked of the live product, and then again
after a redeploy. The screen said, verbatim:

    ⚠ Asked but holds no data of this kind — not used: bigquery-1, bigquery-1.

The same store, twice, with nothing else beside it - so it cannot be read as anything but a
rendering fault. The cause is that `disclosure_from` builds every one of its lines by joining
over OUTCOMES, and a compound ask produces one outcome per store PER SUB-QUESTION. bigquery-1
declined both halves of the question, so it was named both times.

WHY DEDUPE THE RENDERED FRAGMENT AND NOT THE STORE ID. Two of these lines carry per-outcome
detail that is genuinely different and must survive: `Truncated` names "showing 5 of 295 rows",
and `Cross-source alignment` carries `o.note`. Deduping by store_id would silently drop the
second row count or the second note - trading a cosmetic duplicate for real information loss.
Two outcomes that render the IDENTICAL fragment are, by definition, saying the same thing twice;
two that render different fragments are different facts and both belong on screen. So the rule
is "collapse identical fragments, preserve order", which is exactly the shape the canvas already
uses for the outcome-row label (#753/#761).

THE RULE HAS FIVE HOMES, not one. Fixing only the line found on prod would leave four siblings
with the identical defect - the #788 shape, where a rule is guarded on one surface and unguarded
on the others. All five are asserted here.

    PYTHONPATH=src python3 tests/selftest_799_disclosure_names_a_store_once.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.executor import StoreOutcome  # noqa: E402
from dbsearch.router.synthesizer import disclosure_from  # noqa: E402


def _twice(**kw):
    """The same store answering TWO sub-questions - the compound-ask shape that produced this."""
    return [StoreOutcome(store_id="bigquery-1", business_unit="unassigned",
                         sub_question="first half", **kw),
            StoreOutcome(store_id="bigquery-1", business_unit="unassigned",
                         sub_question="second half", **kw)]


def test_a_declined_store_is_named_once_on_a_compound_ask():
    """THE DEFECT, exactly as prod rendered it."""
    d = disclosure_from(_twice(status="declined"))
    assert d.count("bigquery-1") == 1, \
        f"a store that declined both sub-questions is named twice: {d!r}"
    assert "not used: bigquery-1" in d, f"the store stopped being named at all: {d!r}"


def test_every_disclosure_line_names_a_store_once():
    """#788's lesson applied: the rule has FIVE homes and fixing one leaves four.

    Each case is a store that produced the same outcome for two sub-questions. Whatever the
    line says about it, it must say it once."""
    for label, kw in [
        ("unavailable/omitted", dict(status="error", error="boom")),
        ("capped by budget", dict(status="budget")),
        ("declined", dict(status="declined")),
        ("truncated", dict(status="ok", count=5, total=295)),
        ("cross-source note", dict(status="ok", note="keys not aligned")),
    ]:
        # `truncated` is derived (total > count > 0), not settable - so the fixture states the
        # real condition rather than forcing the flag.
        d = disclosure_from(_twice(**kw))
        assert d.count("bigquery-1") == 1, \
            f"the {label} line names the store {d.count('bigquery-1')} times: {d!r}"


def test_a_remedy_is_offered_once_not_per_sub_question():
    """The remedy loop is a separate `for o in dropped`, so it duplicates independently of the
    joined line above it - two identical "To use X: connect Amazon" sentences in a row."""
    d = disclosure_from(_twice(status="error", error="NotSignedIn",
                               remedy="connect Amazon to query this source"))
    assert d.count("connect Amazon") == 1, \
        f"the same remedy is offered once per sub-question: {d!r}"


def test_genuinely_different_facts_are_all_kept():
    """THE CONTROL, and the reason this dedupes fragments rather than store ids.

    Two outcomes for one store that carry DIFFERENT detail are two different facts. A fix that
    deduped by store_id would pass every assertion above and silently delete the second row
    count - trading a cosmetic duplicate for real information loss."""
    a = StoreOutcome(store_id="azure_sql-1", business_unit="finance", status="ok",
                     count=5, total=295)
    b = StoreOutcome(store_id="azure_sql-1", business_unit="finance", status="ok",
                     count=7, total=42)
    d = disclosure_from([a, b])
    assert "5 of 295" in d and "7 of 42" in d, \
        f"two different row counts for one store must BOTH survive: {d!r}"
    assert d.count("azure_sql-1") == 2, \
        f"two genuinely different facts were collapsed into one: {d!r}"


def test_distinct_stores_are_all_named():
    """The other direction: dedup must not swallow a second store."""
    d = disclosure_from([
        StoreOutcome(store_id="bigquery-1", business_unit="unassigned", status="declined"),
        StoreOutcome(store_id="azure_sql-1", business_unit="finance", status="declined"),
    ])
    assert "bigquery-1" in d and "azure_sql-1" in d, \
        f"a distinct store was deduped away: {d!r}"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = []
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            fails.append(t.__name__)
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{'FAILED' if fails else 'PASSED'} - {len(tests) - len(fails)} ok, "
          f"{len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
