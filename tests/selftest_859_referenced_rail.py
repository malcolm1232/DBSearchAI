"""#859: the Sources rail shows what the answer POINTS AT, not everything it retrieved.

I caused this with #856. Since the caller's documents are consulted on every routed turn, a
revenue question retrieves HR policies that answer nothing - and prod showed it within minutes:

    /ask "what is the total amount by region?"
    answer: "APAC = 205,000.00 [1] · AMER = 195,000.00 [3] · EMEA = 125,000.00 [5]"
    rail:   Sources (8) - five of them documents the answer never references

canvas.js had already written the rule down under #724, and it is the sentence this file
exists to enforce: "a Sources list is a provenance claim, and there was no answer for it to be
the provenance OF."

TWO SCENARIOS, because the obvious fix breaks the second one:
  · `unreferenced` - the answer names [1] and [3] of four footnotes -> exactly those two rows
  · `unmarked`     - the answer names nothing at all -> ALL FOUR still render, because a
                     reader left with no rail has nothing to check, and #724's own comment
                     keeps the honest line rather than deleting the block

Driven through tests/ask_proofs_dom_probe.mjs: every claim here is about what is on screen.

    PYTHONPATH=src python3 tests/selftest_859_referenced_rail.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import _domgate  # noqa: E402

PROBE = ROOT / "tests/ask_proofs_dom_probe.mjs"
ASK = ROOT / "src/dbsearch/server/static/js/surfaces/ask.js"
JSDOM = ROOT / "tests/node_modules/jsdom/lib/api.js"

_CACHE: dict = {}


def _report(scenario):
    if scenario in _CACHE:
        return _CACHE[scenario]
    if not _domgate.gate(f"the #859 referenced-rail probe ({scenario})"):
        return None
    out = subprocess.run(["node", str(PROBE), str(JSDOM), str(ASK), scenario],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"probe failed:\n{out.stderr[-2000:]}")
    _CACHE[scenario] = json.loads(out.stdout)
    return _CACHE[scenario]


def test_the_rail_drops_a_source_the_answer_never_points_at():
    """THE CARD. Four footnotes retrieved, two referenced, two rendered."""
    r = _report("unreferenced")
    if r is None:
        return
    nums = [row["num"] for row in r["source_rows"]]
    assert nums == ["[1]", "[3]"], (
        f"the rail rendered {len(nums)} rows {nums} for an answer that points at [1] and [3] "
        f"only - every extra row is a provenance claim the answer never made")


def test_the_rows_that_survive_keep_their_own_numbers():
    """#855's rule, one surface out: the answer says [3], so the row it opens must BE [3].
    Renumbering the survivors 1..n would attach the EMEA claim to the APAC row."""
    r = _report("unreferenced")
    if r is None:
        return
    by_num = {row["num"]: row["snippet"] for row in r["source_rows"]}
    assert "205000.00" in (by_num.get("[1]") or ""), by_num
    assert "125000.00" in (by_num.get("[3]") or ""), by_num


def test_no_marker_is_left_dangling_by_the_trim():
    """A trim that removed a row the answer names would be worse than the noise it fixes:
    the reader clicks [3] and lands nowhere. Measured in the DOM, on the real click target."""
    r = _report("unreferenced")
    if r is None:
        return
    assert r["dangling_markers"] == [], \
        f"the trim broke a marker the answer prints: {r['dangling_markers']}"


def test_the_summary_counts_what_is_on_screen():
    """A rail that says 'Sources (4)' over two rows is the same lie in the summary line."""
    r = _report("unreferenced")
    if r is None:
        return
    assert "2" in (r["rail_summary"] or ""), \
        f"the summary does not agree with the {len(r['source_rows'])} rows below it: {r['rail_summary']!r}"


def test_an_answer_with_no_markers_still_shows_everything_it_read():
    """THE CONTROL, and the reason this is not simply `filter(referenced)`. An extractive or
    cautious answer that cites nothing must not be left with an empty rail - the reader would
    have no way to check it, and 'searched and found nothing to cite' is not 'searched
    nothing'."""
    r = _report("unmarked")
    if r is None:
        return
    nums = [row["num"] for row in r["source_rows"]]
    assert nums == ["[1]", "[2]", "[3]", "[4]"], (
        f"an answer that referenced nothing lost its whole rail: {nums}")


def test_the_routed_scenario_is_unchanged():
    """The #689 fixture references both its footnotes, so #859 must be invisible to it."""
    r = _report("routed")
    if r is None:
        return
    nums = [row["num"] for row in r["source_rows"]]
    assert nums == ["[1]", "[2]"], f"the routed rail changed shape: {nums}"


# ---------------------------------------------------------------- the contract, at the wire
def test_a_routed_chat_response_carries_referenced():
    """The rail keys off `referenced` being a LIST. Absent and empty are different states to a
    client (`Array.isArray(r.referenced)`), and only one of them is the honest 'this answer
    cited nothing' - so the key has to be on the routed response, the way #724 has put it on
    the document one since it was written."""
    import os
    os.environ.update(SELFHOST_BACKEND="memory", DBSEARCH_DEV_AUTH="1",
                      DBSEARCH_RATE_LIMIT="0", DBSEARCH_ASK_ROUTES="1")
    sys.path.insert(0, str(ROOT / "src"))
    from fastapi.testclient import TestClient

    from dbsearch.server.app import app
    client = TestClient(app)
    A = {"X-DBSearch-User": "alice"}
    r = client.post("/router/compose", headers=A, json={"manifest": {"tenant": "acme", "stores": [
        {"id": "ledger-859", "kind": "csv", "business_unit": "finance", "acl": ["alice"],
         "title": "Regional totals", "description": "total amount by region revenue",
         "config": {"tables": {"t": {"columns": ["region", "amount"],
                                     "rows": [["apac", 205000], ["emea", 125000]]}}}}]}})
    assert r.status_code == 200, r.text
    r = client.post("/chat", headers=A,
                    json={"conv_id": "c-859-wire", "question": "total amount by region"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("footnotes"), (
        f"PRECONDITION FAILED: this turn is not routed, so it says nothing about the routed "
        f"contract: {sorted(body)}")
    assert "referenced" in body, (
        f"a routed answer does not say what it references, so the rail cannot tell 'cited "
        f"nothing' from 'not told' and shows everything retrieved: {sorted(body)}")
    assert isinstance(body["referenced"], list), body["referenced"]


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
