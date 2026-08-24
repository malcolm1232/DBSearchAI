"""#855: a reopened turn must cite the SAME list the live answer's [n] markers indexed.

Found on PROD, minutes after DBSEARCH_ASK_ROUTES=1 went live, and invisible to every test
that existed: /ask answered "APAC 205,000.00[1], AMER 195,000.00[2], EMEA 125,000.00[3]" with
three Azure SQL citations, and reopening the same thread showed ONE citation while the answer
still said [2] and [3]. Two changes conspired:

  · `ask_delegate` gave every proof citation of one (store, sql) the JOINED snippet of all its
    rows, so three genuinely different citations became byte-identical, and
  · `_slim_citations` DEDUPED the persisted list, so the identical three collapsed to one -
    while the answer text, already cleaned against the live count by `_drop_dangling_markers`,
    kept pointing at 2 and 3.

The codebase had already written the rule down, one file away, in `_citation_rows`:
"removing row n silently renumbers every later marker... A row that says less is honest; a
row that has moved is a lie." This file makes that rule hold on the PROOF plane too.

ONE MUTATION PER CLAUSE. The two halves are asserted separately and each fixture goes red on
its own half alone: restore the join and `test_a_proof_row_keeps_its_own_snippet` fails while
the length test still passes (rows differ by nothing else); restore the dedupe and
`test_slim_citations_never_moves_a_row` fails on rows the join never touched.

    PYTHONPATH=src python3 tests/selftest_855_reopened_markers.py
"""
import json
import os
import re
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
os.environ["DBSEARCH_RATE_LIMIT"] = "0"
os.environ.pop("DBSEARCH_ASK_ROUTES", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.query.conversation import _slim_citations  # noqa: E402
from dbsearch.server.app import app  # noqa: E402
from dbsearch.server.router_api import pair_proof_snippets  # noqa: E402

client = TestClient(app)
ALICE = {"X-DBSearch-User": "alice"}

LEDGER = {"regional_totals": {"columns": ["region", "amount"],
                              "rows": [["apac", 205000], ["emea", 125000],
                                       ["amer", 195000]]}}


def _compose(store_id="ledger-855"):
    r = client.post("/router/compose", headers=ALICE, json={"manifest": {
        "tenant": "acme", "stores": [{
            "id": store_id, "kind": "csv", "business_unit": "finance",
            "title": "Regional totals", "description": "total amount by region",
            "acl": ["alice"], "config": {"tables": LEDGER}}]}})
    assert r.status_code == 200, r.text


def _stream(question, conv_id):
    r = client.post("/chat/stream", headers=ALICE,
                    json={"conv_id": conv_id, "question": question})
    assert r.status_code == 200, r.text
    events = [json.loads(line[6:]) for line in r.text.splitlines()
              if line.startswith("data: ")]
    done = [e for e in events if e.get("type") == "done"]
    assert len(done) == 1, f"expected one done event, got {len(done)}"
    return done[0]


# ---------------------------------------------------------------- clause 1: the snippet
def test_a_proof_row_keeps_its_own_snippet():
    """Three rows of one SELECT are three DIFFERENT pieces of evidence.

    Joining them onto every citation was the fix for a dict-comprehension that kept only the
    LAST snippet (all three rows read "region=emea"). It solved that by making all three rows
    identical, which is what let the dedupe collapse them - so the transcript went from
    showing one row three times to showing three rows as one."""
    done = {
        "footnotes": [
            {"store_id": "s1", "sql": "SELECT region, SUM(amount) FROM t GROUP BY region",
             "snippet": "region=apac, amount=205000"},
            {"store_id": "s1", "sql": "SELECT region, SUM(amount) FROM t GROUP BY region",
             "snippet": "region=amer, amount=195000"},
            {"store_id": "s1", "sql": "SELECT region, SUM(amount) FROM t GROUP BY region",
             "snippet": "region=emea, amount=125000"},
        ],
        "citations": [
            {"store_id": "s1", "proof": {"sql": "SELECT region, SUM(amount) FROM t GROUP BY region"}},
            {"store_id": "s1", "proof": {"sql": "SELECT region, SUM(amount) FROM t GROUP BY region"}},
            {"store_id": "s1", "proof": {"sql": "SELECT region, SUM(amount) FROM t GROUP BY region"}},
        ],
    }
    pair_proof_snippets(done)
    got = [c.get("snippet") for c in done["citations"]]
    assert got == ["region=apac, amount=205000",
                   "region=amer, amount=195000",
                   "region=emea, amount=125000"], \
        f"each citation must carry ITS OWN row of the result, got {got}"
    assert len(set(got)) == 3, \
        f"the three proof rows are indistinguishable, so any dedupe will collapse them: {got}"


def test_a_citation_that_already_had_a_snippet_is_left_alone():
    """The producer's own snippet wins: this pass fills gaps, it does not overwrite."""
    done = {
        "footnotes": [{"store_id": "s1", "sql": "Q", "snippet": "from the footnote"}],
        "citations": [{"store_id": "s1", "sql": "Q", "snippet": "already mine"}],
    }
    pair_proof_snippets(done)
    assert done["citations"][0]["snippet"] == "already mine", done["citations"]


def test_more_citations_than_rows_says_all_of_it_rather_than_the_wrong_row():
    """Running out of rows must not attach row 1 to citation 4 - that is the invented
    provenance `_drop_dangling_markers` refuses to manufacture. Saying "all the rows this
    query returned" is true; naming one of them would not be."""
    done = {
        "footnotes": [{"store_id": "s1", "sql": "Q", "snippet": "row one"},
                      {"store_id": "s1", "sql": "Q", "snippet": "row two"}],
        "citations": [{"store_id": "s1", "sql": "Q"}, {"store_id": "s1", "sql": "Q"},
                      {"store_id": "s1", "sql": "Q"}],
    }
    pair_proof_snippets(done)
    got = [c.get("snippet") for c in done["citations"]]
    assert got[0] == "row one" and got[1] == "row two", got
    assert got[2] and "row one" in got[2] and "row two" in got[2], \
        f"the extra citation must say all of it, not one arbitrary row: {got[2]!r}"


# ---------------------------------------------------------------- clause 2: the list
def test_slim_citations_never_moves_a_row():
    """Length and order are the contract, because [n] is an INDEX.

    Two identical proof rows are two positions the answer may point at. Whether the rail
    wants to render them as one entry is a RENDER question and is answered on the live
    surface, which shows both today - so a persisted list that shows fewer is not tidier, it
    is a different answer."""
    cites = [
        {"doc": "d1", "quote": "q"},
        {"store_id": "s1", "kind": "sql", "sql": "Q", "origin": "o", "snippet": "same"},
        {"store_id": "s1", "kind": "sql", "sql": "Q", "origin": "o", "snippet": "same"},
        {"doc": "d1", "quote": "q"},
        {"store_id": "s2", "kind": "sql", "sql": "Q2", "origin": "o2", "snippet": "other"},
    ]
    out = _slim_citations(cites)
    assert len(out) == len(cites), (
        f"the persisted list lost {len(cites) - len(out)} row(s): every [n] after the first "
        f"dropped row now points at somebody else's evidence. got {out}")
    assert out[1] == out[2], f"order moved: {out}"
    assert out[4].get("store_id") == "s2", f"the last row is not last any more: {out}"


def test_slim_citations_holds_the_slot_for_a_row_it_cannot_classify():
    """A row that is neither a document nor a proof still OCCUPIES a marker. Dropping it
    renumbers every later one - the lie `_citation_rows` names in its own docstring."""
    cites = [{"store_id": "s1", "kind": "sql", "sql": "Q", "origin": "o"},
             {"nonsense": True},
             {"doc": "d9"}]
    out = _slim_citations(cites)
    assert len(out) == 3, f"an unclassifiable row shifted the ones after it: {out}"
    assert out[2].get("doc") == "d9", f"[3] no longer resolves to d9: {out}"


# ---------------------------------------------------------------- the claim, at the wire
def test_a_reopened_routed_turn_cites_what_the_live_answer_indexed():
    """THE PROD FINDING, end to end: stream a routed turn, then reopen it.

    The load-bearing assertion is the LENGTH, not the marker text: what the model chooses to
    write varies, but "the reopened list is shorter than the one the answer was numbered
    against" is the defect itself and does not depend on the model saying anything in
    particular. The precondition - that this turn cites one (store, sql) more than once - is
    asserted, because a turn with no repeated proof could never show the collapse."""
    os.environ["DBSEARCH_ASK_ROUTES"] = "1"
    _compose()
    conv = "c-855-reopen"
    done = _stream("what is the total amount by region", conv)
    live = done.get("citations") or []
    proofs = [(c.get("store_id"), (c.get("proof") or {}).get("sql") or c.get("sql"))
              for c in live if c.get("store_id")]
    assert len(proofs) > len(set(proofs)), (
        "PRECONDITION FAILED: this turn cites no (store, sql) twice, so nothing here could "
        f"collapse and a green would mean nothing. live citations: {live}")

    r = client.get(f"/conversations/{conv}/transcript", headers=ALICE)
    assert r.status_code == 200, r.text
    turn = r.json()["turns"][-1]
    stored = turn.get("citations") or []
    assert len(stored) == len(live), (
        f"the live answer was numbered against {len(live)} citations and the reopened turn "
        f"offers {len(stored)}: every marker past {len(stored)} now points at nothing")
    dangling = [n for n in {int(x) for x in re.findall(r"\[(\d+)\]", turn.get("answer") or "")}
                if n > len(stored)]
    assert not dangling, f"dangling markers on the reopened turn: {dangling}"


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
