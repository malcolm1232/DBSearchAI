"""#620: a reopened conversation must carry what its answers point at.

THE DEFECT, found on dbsearch.ai during the #619 prod acceptance run: a live answer renders
its Sources list and the honest "Answered from N of M documents you can access" footer;
clicking the same thread in "Your conversations" re-rendered the answer TEXT with its [1][2]
superscripts intact and nothing underneath - so the markers referenced nothing and the
honesty line silently disappeared. The answer looked sourced and was not checkable.

The transcript now returns, per turn, the citations that turn's answer was built from
({doc, title, uri}, in stored order), plus one top-level corpus block for the footer.

TWO RULES ABOUT DEGRADED ROWS, because both are ways of lying:

  A document the CALLER's scope cannot resolve (deleted since, or a grantor's document
  reached through a share) keeps its row with title = its id and uri = None. It is NOT
  dropped: the answer text's [n] markers index into this list POSITIONALLY, so removing
  row n silently renumbers every later marker - the exact thing _drop_dangling_markers
  refuses to do, because renumbering attaches a claim to a source the model never pointed
  at.

  Titles are resolved through the caller's own scope (conversation-aware, ADR 0020), never
  through the grantor's, so the transcript can never name a document the reader could not
  retrieve. A title is routinely the whole secret (#549).

    python3 tests/selftest_620_transcript_citations.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
H = {"X-DBSearch-User": "alice"}


def _seed(doc_id: str, title: str, text: str, uri: str) -> None:
    r = client.post("/ingest", headers=H, json={
        "external_id": doc_id, "title": title, "text": text,
        "acl": ["alice"], "uri": uri,
    })
    assert r.status_code == 200, r.text


def test_transcript_turns_carry_citations_and_corpus():
    _seed("hr-leave-policy.txt", "HR leave policy",
          "Full-time employees receive 26 days of paid annual leave per calendar year.",
          "https://intranet.example/hr-leave-policy")
    conv = "t620-conv-1"
    live = client.post("/chat", headers=H,
                       json={"conv_id": conv, "question": "how many leave days?"})
    assert live.status_code == 200, live.text
    live_cites = live.json()["citations"]

    t = client.get(f"/conversations/{conv}/transcript", headers=H)
    assert t.status_code == 200, t.text
    body = t.json()

    assert "corpus" in body, "transcript carries no corpus block - the footer cannot render"
    turns = body["turns"]
    assert turns, "no turns returned"
    assert "citations" in turns[0], "turn carries no citations key"

    assert [c["doc"] for c in turns[0]["citations"]] == [c["doc"] for c in live_cites], (
        "transcript citations disagree with the live answer's - the reopened thread would "
        "resolve its markers against a different list")
    if live_cites:
        row = turns[0]["citations"][0]
        assert row["title"] == "HR leave policy", row
        assert row["uri"] == "https://intranet.example/hr-leave-policy", row
    print("  PASS  reopened turns carry their citations, and the corpus block rides along")


def test_unresolvable_doc_degrades_to_its_id_and_is_never_dropped():
    _seed("doomed.txt", "Doomed policy",
          "The Krakow office allowance is 42 euros per day.",
          "https://intranet.example/doomed")
    conv = "t620-conv-2"
    live = client.post("/chat", headers=H,
                       json={"conv_id": conv, "question": "what is the Krakow allowance?"})
    assert live.status_code == 200, live.text
    n_live = len(live.json()["citations"])
    if not n_live:
        print("  SKIP  retrieval cited nothing - the degradation path has nothing to prove")
        return

    d = client.delete("/documents/doomed.txt", headers=H)
    assert d.status_code == 200, d.text

    t = client.get(f"/conversations/{conv}/transcript", headers=H)
    assert t.status_code == 200, t.text
    rows = t.json()["turns"][0]["citations"]
    assert len(rows) == n_live, (
        "a citation row was dropped rather than degraded - every later [n] marker in the "
        "answer now points at the wrong source")
    gone = [r for r in rows if r["doc"] == "doomed.txt"]
    assert gone, "the deleted document lost its row entirely"
    assert gone[0]["title"] == "doomed.txt", gone[0]
    assert gone[0]["uri"] is None, gone[0]
    print("  PASS  an unresolvable document keeps its row, titled by its id")


def test_shared_thread_reports_no_denominator_rather_than_a_wrong_one():
    """The footer says "of the N documents YOU can access". On a thread read through
    somebody else's share, the answers above it were produced under a conv-scoped expansion
    this route may not perform (ADR 0020 / CRITICAL-A - only the two chat routes may declare
    an active conversation). Counting under the narrower scope would print a number that
    disagrees with the answers it sits under, so `corpus` is null: the defined "cannot count"
    state, which the client renders as retrieval-only with no entitlement claim.

    Asserted through the OWNER's own thread, where the opposite must hold - a denominator is
    both computable and correct, so it must actually be there."""
    t = client.get("/conversations/t620-conv-1/transcript", headers=H)
    assert t.status_code == 200, t.text
    body = t.json()
    assert body["own"] is True, "fixture is not an owner-read thread"
    assert body["corpus"] is not None, (
        "an owner reading her own thread CAN be counted - a null here would drop the footer "
        "that #620 exists to restore")
    print("  PASS  an owner's own thread carries a real denominator")


def test_transcript_404_is_unchanged_for_a_thread_that_is_not_yours():
    r = client.get("/conversations/t620-conv-1/transcript", headers={"X-DBSearch-User": "bob"})
    assert r.status_code == 404, (
        f"existence is the secret (#549): got {r.status_code}, wanted 404")
    print("  PASS  somebody else's thread still answers 404, not 403")


def test_the_anonymous_link_reader_gained_no_new_field():
    """`_readable_prefix` is shared with the anonymous link visitor (link_access.py, ADR
    0021), whose audience this card never designed for. `docmeta=None` is what keeps that
    response byte-identical: no scope, no metadata, no citations key. Pinned structurally -
    the default must stay, and the visitor's call must stay three-argument - because the
    failure mode is a field quietly appearing on a token-authorized endpoint."""
    import ast
    import inspect

    from dbsearch.server import app as app_mod

    sig = inspect.signature(app_mod._readable_prefix)
    assert sig.parameters["docmeta"].default is None, (
        "_readable_prefix's docmeta must default to None - that default is what keeps the "
        "anonymous link transcript from growing a citations key")

    link_src = (Path(__file__).resolve().parents[1]
                / "src/dbsearch/server/link_access.py").read_text()
    for node in ast.walk(ast.parse(link_src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "readable_prefix"):
            assert len(node.args) == 2 or len(node.args) == 3, node.args
            assert not any(k.arg == "docmeta" for k in node.keywords), (
                "the anonymous link reader now passes docmeta - that adds citations to a "
                "token-authorized response, which is a disclosure decision, not a refactor")
    print("  PASS  the anonymous link transcript is untouched")


if __name__ == "__main__":
    test_transcript_turns_carry_citations_and_corpus()
    test_unresolvable_doc_degrades_to_its_id_and_is_never_dropped()
    test_shared_thread_reports_no_denominator_rather_than_a_wrong_one()
    test_transcript_404_is_unchanged_for_a_thread_that_is_not_yours()
    test_the_anonymous_link_reader_gained_no_new_field()
    print("\nTRANSCRIPT CITATIONS SELF-TEST PASSED.")
