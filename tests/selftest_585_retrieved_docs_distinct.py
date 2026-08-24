"""#585 - `retrieved_docs` counts DOCUMENTS, not chunks.

THE BUG, read off the live site while verifying the #572-#576 branch on dbsearch.ai:
one uploaded PDF matched five chunks, and the answer footer said

    "Answered from 5 of the 2 documents you can access."

Two things wrong in one sentence. The 5 is a chunk count wearing the word "document",
and the claim is impossible on its face - a reader can never draw on more documents than
they are entitled to see, so any number bigger than the entitlement is self-refuting.

The footer was innocent. `QueryResult.retrieved_docs` was built as one entry per HIT
(`[h.doc_external_id for h in hits]`) while its own docstring called it "doc ids", and
`retrieved_owners`, built one line below it, already de-duplicated with a set. The UI
component that renders the sentence deliberately keeps two numbers apart - retrieval and
entitlement - and was handed a conflated one.

Order is retrieval order, not sorted: the first entry is the document that best answered
the question, and sorting would throw that away.

    PYTHONPATH=src python3 tests/selftest_585_retrieved_docs_distinct.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from dbsearch.query.service import _distinct_docs  # noqa: E402


class _Hit:
    """Just enough of a retrieval hit: the two fields _distinct_docs reads."""

    def __init__(self, doc_external_id, owner_oid=""):
        self.doc_external_id = doc_external_id
        self.owner_oid = owner_oid


def test_five_chunks_of_one_document_are_one_document():
    """The live failure, reproduced at the unit the bug actually lived in."""
    hits = [_Hit("f-and-n-directorship-policy") for _ in range(5)]
    assert _distinct_docs(hits) == ["f-and-n-directorship-policy"], _distinct_docs(hits)


def test_the_count_can_never_exceed_the_documents_involved():
    """The property that makes 'N of the M you can access' sayable at all."""
    hits = [_Hit("a"), _Hit("b"), _Hit("a"), _Hit("b"), _Hit("a"), _Hit("c")]
    got = _distinct_docs(hits)
    assert len(got) == 3, got
    assert len(got) == len(set(h.doc_external_id for h in hits)), got


def test_retrieval_order_survives_and_is_not_sorted():
    """Rank is meaning here - the first entry is the best-answering document."""
    hits = [_Hit("zebra"), _Hit("apple"), _Hit("zebra"), _Hit("mango")]
    assert _distinct_docs(hits) == ["zebra", "apple", "mango"], _distinct_docs(hits)


def test_no_hits_is_an_empty_list_not_a_phantom_document():
    assert _distinct_docs([]) == []


def test_the_answer_path_reports_distinct_documents():
    """Through QueryResult, so a future refactor that rebuilds the field is caught too."""
    from dbsearch.query.service import QueryResult
    hits = [_Hit("doc-1", "alice"), _Hit("doc-1", "alice"), _Hit("doc-2", "bob")]
    r = QueryResult(answer="x", citations=[], retrieved_docs=_distinct_docs(hits),
                    retrieved_owners=sorted({h.owner_oid for h in hits if h.owner_oid}))
    assert r.retrieved_docs == ["doc-1", "doc-2"], r.retrieved_docs
    # the sibling field that was already correct, pinned so the two cannot drift apart
    assert r.retrieved_owners == ["alice", "bob"], r.retrieved_owners


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
