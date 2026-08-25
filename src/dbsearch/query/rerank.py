"""Hybrid retrieval reranking (#44) — fuse vector similarity with lexical keyword overlap so
the right snippet surfaces from messy unstructured docs.

Pure functions, adapter-agnostic. Applied by QueryService AFTER the permission trim, so it only
ever reorders chunks the user is already authorized to see (LAW 2 is untouched — reranking can
neither add nor reveal a chunk the trim excluded).

Method: Reciprocal Rank Fusion (RRF). Each candidate gets a rank from the vector ordering and a
rank from the lexical ordering; the fused score is sum(1/(k+rank)). RRF is scale-free, so it
combines a dense cosine score and a sparse keyword score without brittle normalization — the
standard hybrid-search technique.
"""
from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def lexical_score(query: str, text: str) -> float:
    """Fraction of distinct query terms that appear in the text (0..1). Cheap, deterministic,
    and a strong signal for exact-keyword matches that dense vectors can rank too low."""
    q = set(_terms(query))
    if not q:
        return 0.0
    body = set(_terms(text))
    return len(q & body) / len(q)


# Minimal English stopword set — used ONLY by the relevance floor's content-term overlap,
# never by RRF ranking. Keeps the floor from being fooled by shared filler words ("what is
# our …") so an off-topic doc that shares only stopwords scores 0 and can be dropped.
_STOP = frozenset(
    "a an and are as at be been but by can could do does for from had has have he her his how "
    "i in is it its me my of on or our she that the their them they this to was we were what "
    "when where which who why will with would you your".split()
)


def _content_terms(text: str) -> set:
    """Distinct CONTENT terms (stopwords removed) — the unit the relevance floor reasons over."""
    return set(_terms(text)) - _STOP


def content_overlap(query: str, text: str) -> float:
    """Fraction of the query's CONTENT terms (stopwords removed) that appear in `text` (0..1).

    Stricter than `lexical_score`: 'what/is/our' don't count, so a doc that only shares filler
    words with the question scores 0. Kept for callers/tests; the floor uses shared COUNTS."""
    q = _content_terms(query)
    if not q:
        return 0.0
    return len(q & set(_terms(text))) / len(q)


def shared_content_count(query: str, text: str) -> int:
    """How many DISTINCT query content-terms appear in `text`. Count, not fraction, so a doc
    that matches a sub-topic of a long multi-topic query isn't penalised for the words it
    doesn't address (the fraction metric wrongly dropped a relevant 'confidential acquisition'
    doc against a 7-word brief)."""
    return len(_content_terms(query) & set(_terms(text)))


def _position(hit) -> "tuple[str, int] | None":
    """(document, ordinal) for a chunk, or None when it is unpositioned.

    `chunk_id` is "{doc_external_id}#{n}" with n sequential per document
    (pipeline/runner.py:_stage_chunk_embed), so adjacency needs no extra index round trip -
    the id a hit already carries says where in its document it sits. Callers whose hits have
    no chunk_id (a hand-built test hit, an adapter that does not supply one) get None and are
    simply never treated as anyone's neighbour."""
    cid = getattr(hit, "chunk_id", "") or ""
    doc, sep, ordinal = cid.rpartition("#")
    if not sep:
        return None
    try:
        return (getattr(hit, "doc_external_id", doc), int(ordinal))
    except ValueError:
        return None


def relevance_floor(query: str, hits: list, *, rel_lexical: float = 0.5, rel_score: float = 0.0,
                    enabled: bool = True):
    """Drop FILLER chunks that a fixed top-k would otherwise force into the citation list.

    A chunk is KEPT only if it clears a MEANINGFUL bar on at least one axis, RELATIVE to the
    best-matching hit:
      - lexical (primary): it shares at least `ceil(rel_lexical * best_shared)` of the query's
        content terms, where `best_shared` is the most any hit shares (and >= 1 term always).
        Relative + count-based, so it (a) kills incidental single-word coincidences — a food
        review containing "holiday" loses to the handbook that shares "holiday/expenses/policy",
        and (b) still keeps a doc that genuinely answers ONE part of a long brief (shares 2 of
        2 distinctive terms even if the brief has 7). This is the robust signal for a
        bag-of-words embedder and does NOT degrade as the corpus grows.
      - vector (opt-in rescue): score >= `rel_score` x the best score. OFF by default
        (`rel_score=0`) because HashingEmbedding cosine is noisy (an off-topic doc can sit at
        ~0.85x best); a real semantic embedder can enable it (e.g. 0.9) to recover pure-semantic
        matches that share no keywords.

    A kept chunk also brings its immediate NEIGHBOURS in the same document (#936), because
    the relative bar above is blind to document structure: a heading matches a question's
    words, raises `best_shared`, and evicts the body underneath that actually answers it.

    Strictly SUBTRACTIVE: like the permission trim it can only REMOVE chunks, never add or
    reorder authorization (LAW 2 untouched) - the neighbour rule re-admits only chunks already
    in this pool, which the trim has already authorized. Returning [] is correct for a truly
    unanswerable question — the caller then abstains instead of citing irrelevant filler.
    `enabled=False` turns the floor off (a reversible config knob, not a rewrite)."""
    if not hits or not enabled:
        return list(hits)
    qterms = _content_terms(query)
    shared = [len(qterms & set(_terms(h.text or ""))) for h in hits]
    best_shared = max(shared, default=0)
    import math
    lex_need = max(1, math.ceil(rel_lexical * best_shared))
    best_score = max((h.score for h in hits), default=0.0)
    score_cut = rel_score * best_score if rel_score > 0 else None
    keep = [sh >= lex_need or (score_cut is not None and h.score >= score_cut)
            for h, sh in zip(hits, shared)]
    # #936: a kept chunk brings its immediate neighbours with it. The bar above is RELATIVE to
    # the best hit, and a heading is exactly the shape that matches a question's words while
    # answering nothing - so it sets `best_shared` high and evicts the body underneath it,
    # which is the half that answers. Both prod sightings were that: heading kept / body
    # dropped, and continuation kept / antecedent dropped. One hop only, and only out of chunks
    # ALREADY in this authorized pool, so this stays strictly subtractive against the trim
    # (LAW 2) and cannot re-admit a whole document. It cannot resurrect #690's off-topic filler
    # either: a document with no kept chunk has nothing to be adjacent to.
    adjacent = {(doc, n + step)
                for pos in (_position(h) for h, k in zip(hits, keep) if k) if pos
                for doc, n in (pos,) for step in (-1, 1)}
    if adjacent:
        keep = [k or (_position(h) in adjacent) for h, k in zip(hits, keep)]
    return [h for h, k in zip(hits, keep) if k]


def _ranks(order: list[int]) -> dict[int, int]:
    # order = item indices best-first -> {index: rank(1-based)}
    return {idx: r for r, idx in enumerate(order, start=1)}


def hybrid_rerank(query: str, hits: list, top_k: int, *, k: int = 60):
    """Reorder `hits` by RRF of their vector score and their lexical score, return the top_k.

    `hits` is any list whose items expose `.score` (vector similarity) and `.text`. Returns the
    same item objects, reordered and truncated — never fabricates or drops authorization."""
    if not hits:
        return []
    idxs = list(range(len(hits)))
    vec_order = sorted(idxs, key=lambda i: hits[i].score, reverse=True)
    lex_order = sorted(idxs, key=lambda i: lexical_score(query, hits[i].text), reverse=True)
    vr, lr = _ranks(vec_order), _ranks(lex_order)
    fused = sorted(idxs, key=lambda i: (1.0 / (k + vr[i]) + 1.0 / (k + lr[i])), reverse=True)
    return [hits[i] for i in fused[:top_k]]
