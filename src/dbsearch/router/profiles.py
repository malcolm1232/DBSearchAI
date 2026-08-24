"""Profile vectors + scoring — the router's cheap prefilter (Phase E E2, card #99).

Each store is embedded ONCE (its profile text) into the canonical embedder's space and the
vector cached on the StoreProfile; the query is embedded per call; cosine ranks them. The
fan-out set is chosen by a relative floor — keep stores scoring within `frac` of the best —
the same shape as the #53/#54 relevance floor, here applied to store scores instead of chunks.
"""
from __future__ import annotations

import math
import re

from dbsearch.ports.base import EmbeddingPort
from dbsearch.router.catalog import CatalogNode
from dbsearch.router.decision import RoutedStore
from dbsearch.router.store import StoreProfile


def _schema_terms(schema: list | None) -> list[str]:
    """#296: the routing signal for a store used to be title/description/business_unit/topics
    only, so a store connected without a description (the UI seeds it empty) had an ~empty
    profile and scored 0 against every question — unroutable, then mislabeled "you don't have
    access". Its SCHEMA is the strongest evidence of what it holds and is already retrieved and
    attached (E4), so fold the table + column names into the profile. Compound identifiers
    (dbo.sales, total_revenue) are split into word tokens because the demo embedder tokenizes on
    whitespace — 'sales'/'revenue' must be matchable, not buried inside 'dbo.sales'.

    Columns arrive as either {"name": ...} dicts (structured engines) or bare strings (fixture
    engines), so both shapes are handled. Doc/native stores have schema=None -> no change."""
    terms: list[str] = []
    for table in schema or []:
        if not isinstance(table, dict):
            continue
        names = [table.get("table")]
        for col in table.get("columns") or []:
            names.append(col.get("name") if isinstance(col, dict) else col)
        for name in names:
            if name:
                terms.extend(re.findall(r"[a-z0-9]+", str(name).lower()))
    return terms


def profile_text(profile: StoreProfile) -> str:
    parts = [profile.title, profile.description, profile.business_unit, *profile.topics]
    parts.extend(_schema_terms(profile.schema))          # #296: structure routes an undescribed store
    return " ".join(p for p in parts if p)


def ensure_profile_vector(profile: StoreProfile, embedder: EmbeddingPort) -> list[float]:
    if profile.profile_vector is None:
        profile.profile_vector = embedder.embed([profile_text(profile)])[0]
    return profile.profile_vector


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


_STOP = {"the", "a", "an", "is", "are", "was", "our", "what", "which", "how", "of",
         "for", "to", "in", "on", "by", "and", "or", "vs", "versus"}


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _terms(text: str) -> set:
    return {_stem(t) for t in text.lower().split() if t not in _STOP and len(t) > 2}


def distinctive_narrow(question: str, selected: list[RoutedStore],
                       nodes: list[CatalogNode]) -> list[RoutedStore]:
    """#288: scope a fan-out when the question DISTINCTIVELY names one of the tied stores.

    The embedding prefilter fans out to every store that scores near the top - correct for a
    genuine cross-store question ("total amount by region"), but wrong when the user named a
    specific measure that only one store holds ("total DEAL amount by region"): the shared
    tokens ("amount", "region") dominate the vector score, so unrelated "amount" stores tie in
    and their rows get blended into one wrong total.

    So, purely as a post-filter on an existing fan-out: if the question contains a term that is
    in exactly ONE fanned-out store's profile and ABSENT from the others' ("deal"->deals store,
    "order"->orders store), scope to that store. If no term is distinctive (a true cross-store
    ask) or several stores are distinctively named, the fan-out is kept unchanged. Never widens
    the set and never reaches a store outside it (LAW 2 / gate #1 already applied upstream)."""
    if len(selected) < 2:
        return selected
    prof = {n.id: _terms(profile_text(n.profile)) for n in nodes if n.profile is not None}
    ids = [s.store_id for s in selected if s.store_id in prof]
    if len(ids) < 2:
        return selected
    q = _terms(question)
    hits = {}
    for sid in ids:
        others = set().union(*(prof[o] for o in ids if o != sid))
        hits[sid] = len(q & (prof[sid] - others))       # question terms unique to THIS store
    winners = [s for s in selected if hits.get(s.store_id, 0) > 0]
    return winners if len(winners) == 1 else selected


def _why(question: str, profile: StoreProfile, score: float) -> str:
    """E7: per-store advisor explanation — the match score plus the terms the question
    actually shares with the store's profile (honest lexical transparency; an LLM
    explainer can replace this behind the same field)."""
    q_terms = {t for t in question.lower().split() if t not in _STOP}
    p_terms = set(profile_text(profile).lower().split())
    shared = sorted(q_terms & p_terms)[:4]
    caps = "/".join(sorted(profile.capabilities)) or "semantic"
    bits = [f"match {score:.2f}", caps]
    if shared:
        bits.append("shares: " + ", ".join(shared))
    return " · ".join(bits)


def score_stores(query_vec: list[float], nodes: list[CatalogNode],
                 embedder: EmbeddingPort, question: str = "") -> list[RoutedStore]:
    scored: list[RoutedStore] = []
    for n in nodes:
        if n.profile is None:
            continue
        v = ensure_profile_vector(n.profile, embedder)
        score = cosine(query_vec, v)
        scored.append(RoutedStore(n.id, n.profile.business_unit, score,
                                  why=_why(question, n.profile, score)))
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored


def coarse_prune(query_vec: list[float], nodes: list[CatalogNode],
                 embedder: EmbeddingPort, floor_frac: float = 0.5) -> list[CatalogNode]:
    """E8 coarse→fine (deferred from E2): score each BU by its BEST store, keep only the
    stores of business units within `floor_frac` of the best BU. At 10-BU scale this
    prunes most of the catalog before per-store scoring. Falls back to the full set
    when there's <2 BUs or no signal — pruning must never invent a zero-candidate
    route that the fine pass wouldn't have produced.

    #321: this scored a BU by the CENTROID (mean) of its store vectors, which dilutes a
    single strong match with unrelated siblings. The demo's `sales` BU (deals + storefront
    + warehouse) lost its one revenue+product store to the gate, so a 'revenue per product'
    question fell to a finance DOCUMENT holding only a single total. The coarse gate's
    question is 'could ANY store in this BU answer?' — a MAX over stores, not a mean. Max
    also prunes strictly less, which is the safe direction (a BU with any relevant store
    survives; a BU where nothing matches is still dropped)."""
    by_bu: dict[str, list[CatalogNode]] = {}
    for n in nodes:
        if n.profile is not None:
            by_bu.setdefault(n.profile.business_unit, []).append(n)
    if len(by_bu) < 2:
        return nodes
    bu_scores: list[tuple[str, float]] = []
    for bu, ns in by_bu.items():
        best_store = max(cosine(query_vec, ensure_profile_vector(n.profile, embedder))
                         for n in ns)
        bu_scores.append((bu, best_store))
    bu_scores.sort(key=lambda t: t[1], reverse=True)
    best = bu_scores[0][1]
    if best <= 0.0:
        return nodes
    keep = {bu for bu, s in bu_scores if s >= floor_frac * best}
    return [n for bu in keep for n in by_bu[bu]]


def relative_floor(scored: list[RoutedStore], frac: float) -> list[RoutedStore]:
    if not scored:
        return []
    best = scored[0].score
    if best <= 0.0:
        return []
    cut = frac * best
    return [s for s in scored if s.score >= cut]
