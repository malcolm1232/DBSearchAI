"""Hybrid selector — turn scored candidates into a pick (Phase E E2, card #99).

Cheap-first, LLM-only-when-ambiguous (design §11.5). A dominant winner (clear margin over
#2) is chosen outright — no LLM cost. When several stores tie within the margin, the
relative-floor pool is the fan-out set; an optional injected `tiebreak` callable (an LLM in
prod, a stub in tests) may narrow/order it. A bad tiebreak result is ignored — the selector
never picks a store that wasn't in the scored, visible pool (LAW 2 / gate #1 upheld upstream).
"""
from __future__ import annotations

from typing import Callable

from dbsearch.router.decision import RoutedStore
from dbsearch.router.profiles import relative_floor


def select_stores(
    scored: list[RoutedStore],
    *,
    margin: float = 0.15,
    fanout_cap: int = 3,
    floor_frac: float = 0.6,
    tiebreak: Callable[[list[str]], list[str]] | None = None,
) -> tuple[list[RoutedStore], str, float]:
    if not scored:
        return [], "fallback", 0.0

    pool = relative_floor(scored, floor_frac)
    if not pool:
        return [], "fallback", 0.0

    conf = pool[0].score

    # dominant single winner — no LLM needed
    if len(pool) == 1 or (pool[0].score - pool[1].score) >= margin:
        return [pool[0]], "prefilter", conf

    capped = pool[:fanout_cap]

    if tiebreak is not None:
        by_id = {s.store_id: s for s in capped}
        picked_ids = [sid for sid in tiebreak([s.store_id for s in capped]) if sid in by_id]
        if picked_ids:
            return [by_id[sid] for sid in picked_ids], "llm", conf

    return capped, "prefilter", conf
