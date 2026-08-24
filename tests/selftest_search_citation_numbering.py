"""#257 — a [n] in a /search answer must resolve to citation n. It did not.

Live on the canvas, "what is holiday days" produced markers [4] and [1] against exactly ONE
citation. [4] pointed at nothing: a footnote the reader cannot follow, attached to a real
sentence, which reads as corroboration that does not exist.

Root cause is the #233 invariant broken in a different caller. QueryService.answer passed
`[h.text for h in hits]` — one context block per CHUNK, which the adapter numbers 1..N — while
building `citations` DEDUPLICATED BY DOCUMENT. A doc retrieved as 6 chunks therefore offered
the model [1]..[6] to cite while only [1] existed in the citation list.

#233's own comment states the rule this violates: "evidence numbering stays 1..N in lockstep
with the footnotes built from that same evidence."

Run: python3 tests/selftest_search_citation_numbering.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIdentity, InMemoryIndex, InMemoryObjectStore, InMemoryQueue,
    PlainTextExtractor,
)
from dbsearch.connectors.upload import UploadConnector  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.query.service import QueryService  # noqa: E402


class SpyLlm:
    """Records the context it was handed, and cites the LAST block it was given — which is
    exactly what a model does when the context offers more numbered blocks than there are
    sources."""

    def __init__(self):
        self.blocks = None

    def answer(self, question, context_chunks):
        self.blocks = list(context_chunks)
        n = len(context_chunks)
        return {"answer": f"Some claim [{n}] and another [1].", "citations": []}

    def answer_stream(self, question, context_chunks):
        yield self.answer(question, context_chunks)["answer"]


def _service(llm):
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    index = InMemoryIndex(store)
    embedder = HashingEmbedding()
    # one document, deliberately long enough to be chunked into several pieces
    body = ("holiday leave entitlement carryover policy staff annual days " * 60).encode()
    conn = UploadConnector("t", "holiday-policy", "Holiday and Annual Leave Policy",
                           body, "text/plain", ["all-staff"], "")
    run_ingestion(conn, queue, store, PlainTextExtractor(), embedder, index)
    identity = InMemoryIdentity({"alice": ["all-staff"]})
    return QueryService(index, identity, embedder, llm, store, tenant_id="t"), index


def test_context_blocks_line_up_one_per_citation():
    """The invariant: however many numbered blocks the model is shown, there must be exactly
    that many citations for a [n] to land on."""
    llm = SpyLlm()
    qs, _ = _service(llm)
    r = qs.answer("alice", "holiday leave carryover")

    assert llm.blocks, "the model was handed no context"
    assert len(llm.blocks) == len(r.citations), (
        f"model was shown {len(llm.blocks)} numbered blocks but only {len(r.citations)} "
        f"citations exist — every [n] above {len(r.citations)} dangles")


def test_no_marker_in_the_answer_dangles():
    llm = SpyLlm()
    qs, _ = _service(llm)
    r = qs.answer("alice", "holiday leave carryover")

    markers = {int(m) for m in re.findall(r"\[(\d+)\]", r.answer)}
    n = len(r.citations)
    dangling = sorted(m for m in markers if m < 1 or m > n)
    assert not dangling, (
        f"answer cites {sorted(markers)} but only {n} citation(s) exist — {dangling} resolve "
        "to nothing a reader can open")


def test_multiple_documents_still_get_one_block_each():
    """Grouping per document must not collapse DIFFERENT documents together — that would make
    two sources share one footnote and misattribute a claim."""
    llm = SpyLlm()
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    index = InMemoryIndex(store)
    embedder = HashingEmbedding()
    for i, title in enumerate(["Holiday Policy", "Expenses Policy"]):
        body = (f"{title} staff policy entitlement rules section " * 40).encode()
        conn = UploadConnector("t", f"doc-{i}", title, body, "text/plain", ["all-staff"], "")
        run_ingestion(conn, queue, store, PlainTextExtractor(), embedder, index)
    qs = QueryService(index, InMemoryIdentity({"alice": ["all-staff"]}), embedder, llm, store, tenant_id="t")

    r = qs.answer("alice", "policy staff entitlement rules")
    assert len(r.citations) >= 2, f"expected both documents to be cited, got {r.citations}"
    assert len(llm.blocks) == len(r.citations), (llm.blocks, r.citations)
    titles = [c["title"] for c in r.citations]
    assert len(set(titles)) == len(titles), f"documents collapsed into one citation: {titles}"


def test_the_streaming_path_numbers_identically():
    """answer_stream builds its own citations list — it must not drift from answer()."""
    llm = SpyLlm()
    qs, _ = _service(llm)
    done = [e for e in qs.answer_stream("alice", "holiday leave carryover")
            if e.get("type") == "done"][-1]
    assert len(llm.blocks) == len(done["citations"]), (
        f"stream showed {len(llm.blocks)} blocks for {len(done['citations'])} citations")


class SectionEchoLlm:
    """The failure lockstep numbering does NOT prevent, seen live: the model lifts numbers out
    of the CONTENT. A policy with headings "1. ENTITLEMENT / 2. CARRYOVER / 4. PUBLIC HOLIDAYS"
    produced "...public holidays [4]" and "...the carryover rule [2]" against ONE citation."""

    def answer(self, question, context_chunks):
        return {"answer": "Holiday days are public holidays [4], and carryover is capped [2]. "
                          "Entitlement is 25 days [1].", "citations": []}

    def answer_stream(self, question, context_chunks):
        yield self.answer(question, context_chunks)["answer"]


def test_a_marker_the_model_invented_from_document_content_is_dropped():
    llm = SectionEchoLlm()
    qs, _ = _service(llm)
    r = qs.answer("alice", "holiday leave carryover")

    n = len(r.citations)
    assert n == 1, f"fixture should yield exactly one citation, got {n}"
    markers = {int(m) for m in re.findall(r"\[(\d+)\]", r.answer)}
    assert markers == {1}, (
        f"answer still carries {sorted(markers)} against {n} citation(s) — [2] and [4] point at "
        "nothing and read as corroboration that does not exist")
    # the PROSE survives — we drop the false promise, not the sentence
    assert "public holidays" in r.answer and "carryover is capped" in r.answer, r.answer
    # and we do NOT renumber: [2]/[4] must vanish, never be silently reassigned to source 1
    assert r.answer.count("[1]") == 1, \
        f"a dropped marker was renumbered onto citation 1, inventing provenance: {r.answer!r}"


def test_the_streaming_done_event_is_sanitised_too():
    llm = SectionEchoLlm()
    qs, _ = _service(llm)
    done = [e for e in qs.answer_stream("alice", "holiday leave carryover")
            if e.get("type") == "done"][-1]
    markers = {int(m) for m in re.findall(r"\[(\d+)\]", done["answer"])}
    assert markers == {1}, f"streamed final answer still dangles: {sorted(markers)}"


def main():
    print("#257 /search citation numbering (a [n] must resolve to citation n):")
    test_context_blocks_line_up_one_per_citation()
    test_no_marker_in_the_answer_dangles()
    print("  PASS  numbered context blocks are in lockstep with the citation list")
    test_a_marker_the_model_invented_from_document_content_is_dropped()
    test_the_streaming_done_event_is_sanitised_too()
    print("  PASS  a marker the model lifted from document CONTENT is dropped, prose intact, "
          "never renumbered onto a real source")
    test_multiple_documents_still_get_one_block_each()
    test_the_streaming_path_numbers_identically()
    print("  PASS  distinct documents stay distinct, and the streaming path numbers identically")
    print("\nSEARCH-CITATION-NUMBERING SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
