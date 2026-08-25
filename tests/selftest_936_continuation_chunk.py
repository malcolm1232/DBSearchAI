"""#936 - the relevance floor must not evict the CONTINUATION of a chunk it kept.

Reported twice on prod by two different people, over two different connectors, on two
different documents - and both times /ask refused a question that its own citations
answered:

    store gdrive-1, file DBSNotes.txt
    q "what is Waves1 to 3 Aug 2026 ?"  ->  "I do not have that information in the
    provided context."  with 3 citations, all from that very file.

The mechanism, measured on the real functions rather than reasoned about. A heading and
the body under it land in ADJACENT chunks. The heading carries the question's words, so it
sets `best_shared` high; `relevance_floor` then raises its own bar to
`ceil(rel_lexical * best_shared)` and evicts the body, which is the half that actually
answers the question. The model is handed a heading with no body and declines.

This is #690 seen from the other side - same `best_shared` bar, opposite direction - so the
last test here is #690's own case, which must stay fixed.

WHY THE ASSERTIONS ARE SHAPED THIS WAY. The defect returned citations, so "N citations came
back" is green ON the defect and proves nothing. The load-bearing assertion is that the
sentence which answers the question reaches the model's context. And because the whole
mechanism depends on the heading and the body landing in DIFFERENT chunks, that split is
asserted FIRST, from the real chunker - otherwise a future chunk-size change would make
every test below pass while measuring nothing.

    python3 tests/selftest_936_continuation_chunk.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIdentity, InMemoryIndex, InMemoryObjectStore,
    InMemoryQueue, PlainTextExtractor,
)
from dbsearch.connectors.upload import UploadConnector  # noqa: E402
from dbsearch.pipeline.runner import _chunk_text, run_ingestion  # noqa: E402
from dbsearch.query import QueryService  # noqa: E402
from dbsearch.query.rerank import _content_terms, _terms  # noqa: E402

QUESTION = "what is Waves1 to 3 Aug 2026 ?"
HEADING = "Waves1 to 3 Aug 2026"
BODY = "state and error honesty"      # the Wave 2 substance, in the chunk AFTER the heading

_RULE = "_" * 60 + " "
_FILLER = ("Session notes from the working canvas. The manifest persisted and the store "
           "composed green. Routing telemetry printed above the answer and the proof pill "
           "expanded on click. ")
_WAVE1 = ("Wave 1 - pure canvas rendering fixes. The node chrome, the flyout width and the "
          "citation chips were repainted so a reader can tell which source a claim came from "
          "without opening the side panel, including the numbering that used to dangle when a "
          "conversation was reopened from the transcript rather than answered fresh. ")
_WAVE2 = ("Wave 2 - state and error honesty. A store whose crawl failed now says so on the "
          "node, the syncing pill resolves into a real document count, and a gesture that "
          "changed nothing no longer reports success to the person who made it, across all 3 "
          "surfaces. ")


def _notes(chunk: int = 1, offset: int = 850) -> str:
    """A document shaped like the one on prod: a heading deep inside the file, with its body
    running past the chunk boundary.

    `_chunk_text` normalizes whitespace before it windows, so the padding is measured on the
    normalized text. Window k covers [1050k, 1050k+1200); the heading has to END before
    1050(k+1) so the overlap does not carry it into the next chunk, and the body has to run
    past 1050k+1200 so it is cut into that next chunk instead."""
    pad = ""
    target = 1050 * chunk + offset
    while len(pad) < target:
        pad += _RULE if len(pad) % 400 < 80 else _FILLER
    return pad[:target - 1] + " " + HEADING + " " + _RULE + _WAVE1 + _RULE + _WAVE2 + _RULE + _FILLER * 4


class _Recorder:
    """Captures the context the model was actually handed. Not a mock of the thing under
    test - the assertion is on what REACHED it, which is the whole defect."""

    def __init__(self):
        self.context = []

    def answer(self, question, context_chunks):
        self.context = list(context_chunks)
        return {"answer": "", "citations": []}


def _shared(text):
    return _shared_q(QUESTION, text)


def _shared_q(question, text):
    return len(_content_terms(question) & set(_terms(text)))


def _ingest(text, ext="DBSNotes.txt"):
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    index = InMemoryIndex(store)
    embedder = HashingEmbedding()
    conn = UploadConnector("t", ext, ext, text.encode(), "text/plain", ["all-staff"], "")
    run_ingestion(conn, queue, store, PlainTextExtractor(), embedder, index)
    return store, index, embedder


def test_chunker_splits_the_heading_from_its_body():
    """PRECONDITION. Every test below is meaningless if these land in one chunk."""
    chunks = _chunk_text(_notes())
    head = [i for i, c in enumerate(chunks) if HEADING in c]
    body = [i for i, c in enumerate(chunks) if BODY in c]
    assert head and body, f"fixture lost its landmarks: heading={head} body={body}"
    assert not (set(head) & set(body)), (
        f"fixture does NOT reproduce the split - heading and body share chunk(s) "
        f"{sorted(set(head) & set(body))}; every assertion below would be vacuous")
    hs, bs = _shared(chunks[head[0]]), _shared(chunks[body[0]])
    assert hs > bs, f"precondition: the heading must out-match the body ({hs} vs {bs})"
    print(f"  PASS  chunker splits heading (chunk {head[0]}, shares {hs}) from body "
          f"(chunk {body[0]}, shares {bs}) - the prod trace was 4 and 1")


def test_floor_keeps_the_continuation_of_a_chunk_it_kept():
    """THE DEFECT. The body is retrieved, then thrown away by the floor."""
    store, index, embedder = _ingest(_notes())
    identity = InMemoryIdentity({"alice": ["all-staff"]})

    off = QueryService(index, identity, embedder, _Recorder(), store,
                       relevance_floor=False, tenant_id="t")
    pool = off.retrieve("alice", QUESTION)
    assert any(BODY in c.text for c in pool), (
        "precondition: with the floor OFF the body must be retrieved - if it is not, this is "
        "a retrieval miss and needs an index-side fix, not a floor fix")

    on = QueryService(index, identity, embedder, _Recorder(), store, tenant_id="t")
    kept = on.retrieve("alice", QUESTION)
    assert any(HEADING in c.text for c in kept), "the heading chunk should still be kept"
    assert any(BODY in c.text for c in kept), (
        "BUG #936: the floor evicted the continuation of a chunk it kept - the heading "
        "survived and the body that answers the question did not")
    print(f"  PASS  floor keeps the continuation ({len(kept)} of {len(pool)} chunks kept)")


def test_the_model_is_handed_the_sentence_that_answers_the_question():
    """THE USER-VISIBLE EFFECT. Citations came back in the defect too, so the assertion is on
    what reached the model, not on how many sources were listed."""
    store, index, embedder = _ingest(_notes())
    identity = InMemoryIdentity({"alice": ["all-staff"]})
    rec = _Recorder()
    QueryService(index, identity, embedder, rec, store, tenant_id="t").answer("alice", QUESTION)
    joined = " ".join(rec.context)
    assert HEADING in joined, "the heading reached the model (it always did)"
    assert BODY in joined, (
        "BUG #936: the model was handed the heading with no body, which is why it said "
        "'I do not have that information in the provided context' over its own citations")
    print("  PASS  the answering sentence reaches the model's context")


def test_floor_keeps_the_ANTECEDENT_of_a_chunk_it_kept():
    """THE SECOND SIGHTING, and the other direction of the same rule.

    Prod, 260824, sharepoint_link store, the owner's Letter of Employment. Asked for the
    notice period, the product answered "I do not have that information." over 5 citations
    from that very file - and citation [2] read "tion in a ccordance with Clause 14.1 above,
    you will be expected to work during the said period of notice". The chunk that REFERS to
    the value was kept; the chunk that CONTAINS it, earlier in the document, was not. Note the
    leading "tion": the kept chunk is itself a mid-word continuation.

    A fix that only walks FORWARD from a kept chunk leaves this sighting broken, which is why
    it is asserted separately rather than folded into the test above."""
    store, index, embedder = _ingest(_notes(), ext="letter.txt")
    identity = InMemoryIdentity({"alice": ["all-staff"]})
    # This question's words live in the LATER chunk, so the earlier one is the antecedent.
    q = "what changed across all 3 surfaces in 2026"
    off = QueryService(index, identity, embedder, _Recorder(), store,
                       relevance_floor=False, tenant_id="t")
    pool = off.retrieve("alice", q)
    later = [c for c in pool if BODY in c.text]
    earlier = [c for c in pool if HEADING in c.text]
    assert later and earlier, (
        f"precondition: both chunks must be retrieved with the floor off "
        f"(later={len(later)} earlier={len(earlier)})")
    assert _shared_q(q, later[0].text) > _shared_q(q, earlier[0].text), (
        "precondition: the LATER chunk must be the strong match here, or this is just the "
        "forward case again")

    kept = QueryService(index, identity, embedder, _Recorder(), store,
                        tenant_id="t").retrieve("alice", q)
    assert any(BODY in c.text for c in kept), "the strong later chunk should still be kept"
    assert any(HEADING in c.text for c in kept), (
        "BUG #936 (second sighting): the floor kept a continuation and evicted the chunk "
        "before it - the one that holds the value the kept chunk only refers to")
    print(f"  PASS  floor keeps the antecedent ({len(kept)} of {len(pool)} chunks kept)")


def test_offtopic_filler_is_still_dropped():
    """#690 CONTROL, the opposite direction. When nothing matches, best_shared collapses and
    the floor must still refuse to cite filler. A fix for #936 that resurrects this has traded
    one defect for the other."""
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    index = InMemoryIndex(store)
    embedder = HashingEmbedding()
    for ext, text in [
        ("staff-handbook", "all staff receive annual holiday leave and expenses are "
                           "reimbursed monthly against receipts"),
        ("jay-shetty-love", "eight rules of love by jay shetty about relationships "
                            "breakups and heart"),
    ]:
        run_ingestion(UploadConnector("t", ext, ext, text.encode(), "text/plain",
                                      ["all-staff"], ""),
                      queue, store, PlainTextExtractor(), embedder, index)
    identity = InMemoryIdentity({"alice": ["all-staff"]})
    cited = [c.doc_external_id for c in
             QueryService(index, identity, embedder, _Recorder(), store,
                          tenant_id="t").retrieve("alice", "what is our holiday and expenses policy")]
    assert "staff-handbook" in cited, "the on-topic handbook must still be retrieved"
    assert "jay-shetty-love" not in cited, (
        "REGRESSION of #690: off-topic filler is being cited again")
    print(f"  PASS  off-topic filler still dropped  ->  {cited}")


def main():
    print("#936 continuation-chunk self-test:")
    test_chunker_splits_the_heading_from_its_body()
    test_floor_keeps_the_continuation_of_a_chunk_it_kept()
    test_the_model_is_handed_the_sentence_that_answers_the_question()
    test_floor_keeps_the_ANTECEDENT_of_a_chunk_it_kept()
    test_offtopic_filler_is_still_dropped()
    print("\nALL #936 TESTS PASSED - a kept chunk brings its continuation, filler still dropped.")


if __name__ == "__main__":
    main()
