"""#633: a citation quotes the passage it names, in one of two HONESTLY LABELLED kinds.

THE DEFECT the owner reported: "[1] hr-leave-policy.txt" names a file and nothing else, so a
reader who wants to check "26 days" has to go and open the document. The claim is sourced in
form and unverifiable in practice.

TWO KINDS, AND THE DISTINCTION IS THE WHOLE POINT, not a implementation detail:

  pointed   - sliced from the model's OWN 【n†Lx-Ly】 range over context block n. This is what
              the answer points at, and it exists only when the model said so.
  retrieved - the head of that document's context block. Chunks arrive relevance-ordered, so
              the head is its best-ranked passage. This is evidence the model was HANDED.

Collapsing the two labels would present retrieval as attribution. What a model was SHOWN is
knowable; what it USED is not, and only the first is safe to build a claim on. So a quote
whose provenance is "we gave it this" never gets to wear the words "this is where it came
from".

The slice runs over the RAW answer, before `_drop_dangling_markers` deletes out-of-range
markers, because that is the only moment the line ranges still exist.

    python3 tests/selftest_633_citation_quotes.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.query.service import QueryService, _cap_quote  # noqa: E402

BLOCK1 = "line one\nline two\nthe leave entitlement is 26 days\nline four"
BLOCK2 = "remote work is allowed two days a week\nmore remote policy text"
CITES = [{"doc": "a", "title": "A", "uri": None, "locator": {}},
         {"doc": "b", "title": "B", "uri": None, "locator": {}}]


def _cites():
    return [dict(c) for c in CITES]


def test_pointed_slice_comes_from_the_models_own_range():
    cites = _cites()
    QueryService._citation_quotes("leave is 26 days 【1†L3-L3】", [BLOCK1, BLOCK2], cites)
    assert cites[0]["quote"] == "the leave entitlement is 26 days", cites[0]
    assert cites[0]["quote_kind"] == "pointed", cites[0]
    print("  PASS  a marked citation quotes the lines the model pointed at")


def test_a_multi_line_range_is_joined():
    cites = _cites()
    QueryService._citation_quotes("x 【1†L1-L2】", [BLOCK1, BLOCK2], cites)
    assert cites[0]["quote"] == "line one line two", cites[0]
    print("  PASS  a multi-line range is joined into one quotable passage")


def test_an_unmarked_source_falls_back_to_retrieved_and_says_so():
    cites = _cites()
    QueryService._citation_quotes("leave is 26 days 【1†L3-L3】", [BLOCK1, BLOCK2], cites)
    assert cites[1]["quote"].startswith("remote work is allowed"), cites[1]
    assert cites[1]["quote_kind"] == "retrieved", (
        "an unmarked source's quote must not claim to be what the answer points at")
    print("  PASS  an unmarked source is labelled retrieved, never pointed")


def test_an_out_of_range_marker_degrades_rather_than_crashing():
    """The model picks numbers out of the CONTENT (the #257 failure), so a range naming
    lines that do not exist is not hypothetical. It degrades to the retrieved head."""
    cites = [dict(CITES[0])]
    QueryService._citation_quotes("x 【1†L900-L901】", [BLOCK1], cites)
    assert cites[0]["quote_kind"] == "retrieved", cites[0]
    assert cites[0]["quote"], "an out-of-range marker left the row with no quote at all"
    print("  PASS  an out-of-range range degrades to the retrieved head")


def test_a_marker_beyond_the_citation_list_is_ignored():
    """`_drop_dangling_markers` deletes these from the prose; they must not reach in here
    and index into a block that belongs to a different document."""
    cites = [dict(CITES[0])]
    QueryService._citation_quotes("x 【7†L1-L1】", [BLOCK1], cites)
    assert cites[0]["quote_kind"] == "retrieved", cites[0]
    print("  PASS  a marker outside 1..N cannot point a quote at the wrong document")


def test_an_empty_block_gets_no_quote_key_at_all():
    """An absent key reads as "no quote"; an empty string renders as a broken blockquote."""
    cites = [dict(CITES[0])]
    QueryService._citation_quotes("x [1]", [""], cites)
    assert "quote" not in cites[0], cites[0]
    print("  PASS  an empty block yields no quote key rather than an empty one")


def test_cap_cuts_at_a_sentence_boundary():
    long = ("A sentence that ends right here. " * 40).strip()
    capped = _cap_quote(long)
    assert len(capped) <= 401, len(capped)
    assert capped.endswith("."), capped[-40:]
    print("  PASS  a long passage is capped at a sentence boundary")


def test_turn_roundtrips_its_citations_through_the_store():
    from dbsearch.query.conversation import Turn
    from dbsearch.server.conversation_store import InMemoryConversationStore
    s = InMemoryConversationStore()
    t = Turn(question="q", standalone="q", answer="a [1]", cited_docs=["a"],
             citations=[{"doc": "a", "quote": "the quote", "quote_kind": "pointed",
                         "locator": {}}])
    s.append("c1", "alice", t)
    back = s.history("c1", "alice")[0]
    assert back.citations and back.citations[0]["quote"] == "the quote", back.citations
    assert back.cited_docs == ["a"], (
        "cited_docs is what the share machinery is defined over and must be untouched")
    print("  PASS  a Turn round-trips its quotes without disturbing cited_docs")


def test_a_quote_survives_the_reopen_and_is_the_same_one():
    """The whole point of persisting rather than recomputing. Retrieval is not stable, so a
    re-derived quote could be a passage the answer above it was never built from - the
    reader would be checking a claim against the wrong evidence and could not tell."""
    os.environ["DBSEARCH_DEV_AUTH"] = "1"
    from fastapi.testclient import TestClient

    from dbsearch.server.app import app
    client = TestClient(app)
    h = {"X-DBSearch-User": "alice"}

    r = client.post("/ingest", headers=h, json={
        "external_id": "q633.txt", "title": "Leave policy",
        "text": "Full-time employees receive 26 days of paid annual leave per year.",
        "acl": ["alice"], "uri": "https://example.test/q633"})
    assert r.status_code == 200, r.text

    live = client.post("/chat", headers=h,
                       json={"conv_id": "q633-conv", "question": "how much leave?"})
    assert live.status_code == 200, live.text
    live_cites = live.json()["citations"]
    if not live_cites:
        print("  SKIP  retrieval cited nothing - nothing to quote")
        return
    assert live_cites[0].get("quote"), (
        f"a live citation carries no quote: {live_cites[0]}")
    assert live_cites[0].get("quote_kind") in ("pointed", "retrieved"), live_cites[0]

    t = client.get("/conversations/q633-conv/transcript", headers=h)
    assert t.status_code == 200, t.text
    reopened = t.json()["turns"][0]["citations"]
    assert reopened[0]["quote"] == live_cites[0]["quote"], (
        "the reopened quote differs from the one shown live - it was recomputed, not stored")
    assert reopened[0]["quote_kind"] == live_cites[0]["quote_kind"], reopened[0]
    print("  PASS  a reopened turn quotes exactly what the live answer quoted")


def test_a_pre_633_turn_falls_back_instead_of_inventing():
    """Turns written before the citations column exist on every deployment with history.
    They must degrade to the #620 rows - named, unquoted - never to a quote derived now."""
    from dbsearch.server.app import _turn_citation_rows

    class OldTurn:
        cited_docs = ["hr.txt"]
        citations = []          # what an unmigrated row reads back as

    rows = _turn_citation_rows(OldTurn(), {"hr.txt": ("HR policy", "https://x.test/hr")})
    assert rows == [{"doc": "hr.txt", "title": "HR policy", "uri": "https://x.test/hr"}], rows
    assert "quote" not in rows[0], "a pre-#633 turn was given a quote it never had"
    print("  PASS  a pre-#633 turn degrades to a named, unquoted row")


def test_the_graphql_seam_does_not_splat_the_citation_dict():
    """FOUND BY THIS CARD, the hard way: `Citation(**c)` made the PUBLISHED GraphQL schema
    depend on the exact key set of an internal dict, so adding a presentational `quote` to a
    citation turned every GraphQL search into a 500 (selftest_server + selftest_integrations
    both went red). A developer API changes when somebody decides to change it, never as a
    side effect of a UI card. Pinned structurally so the splat cannot come back."""
    import ast

    src = (Path(__file__).resolve().parents[1]
           / "src/dbsearch/api/graphql_app.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Citation"):
            assert not any(k.arg is None for k in node.keywords), (
                "graphql_app builds Citation(**dict) again - the published schema is once "
                "more coupled to an internal dict's key set")
    print("  PASS  the GraphQL Citation is built field by field, not splatted")


def test_stored_title_is_never_trusted_over_the_readers_scope():
    """Titles are re-resolved per read. A stored one would survive a rename, and worse would
    survive the document leaving this reader's scope - naming something they cannot see."""
    from dbsearch.server.app import _turn_citation_rows

    class T:
        cited_docs = ["hr.txt"]
        citations = [{"doc": "hr.txt", "quote": "26 days", "quote_kind": "pointed",
                      "title": "STALE TITLE", "uri": "https://stale.test"}]

    rows = _turn_citation_rows(T(), {})          # reader's scope resolves nothing
    assert rows[0]["title"] == "hr.txt", rows[0]
    assert rows[0]["uri"] is None, rows[0]
    assert rows[0]["quote"] == "26 days", rows[0]
    print("  PASS  title and uri come from the reader's scope, never from the stored row")


if __name__ == "__main__":
    test_pointed_slice_comes_from_the_models_own_range()
    test_a_multi_line_range_is_joined()
    test_an_unmarked_source_falls_back_to_retrieved_and_says_so()
    test_an_out_of_range_marker_degrades_rather_than_crashing()
    test_a_marker_beyond_the_citation_list_is_ignored()
    test_an_empty_block_gets_no_quote_key_at_all()
    test_cap_cuts_at_a_sentence_boundary()
    test_turn_roundtrips_its_citations_through_the_store()
    test_a_quote_survives_the_reopen_and_is_the_same_one()
    test_a_pre_633_turn_falls_back_instead_of_inventing()
    test_the_graphql_seam_does_not_splat_the_citation_dict()
    test_stored_title_is_never_trusted_over_the_readers_scope()
    print("\nCITATION QUOTES SELF-TEST PASSED.")
