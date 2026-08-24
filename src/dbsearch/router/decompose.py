"""Compound-query decomposition (Phase E E6, card #103).

Deterministic, honest default: split on explicit comparison joints (versus / vs /
compared to|with) first, else one top-level " and ". The classifier (E2) over-triggers
on purpose — this is the check: if no clean split exists, the question is returned
whole and routed as a plain query. Real deployments swap an LLM decomposer behind the
same signature, injected into RouterQueryService exactly like the E2 tiebreak seam.
"""
from __future__ import annotations

import re

_VS = re.compile(r"\s+(?:versus|vs\.?|compared\s+(?:to|with))\s+", re.I)
_AND = re.compile(r"\s+and\s+", re.I)
# question-y lead-ins that survive a split but aren't retrieval signal
_LEAD = re.compile(r"^(?:compare|contrast|what about|how about|show me|tell me about)\s+", re.I)

MAX_SUBQUERIES = 3   # fan-out discipline: same spirit as the selector's fanout_cap


def decompose_query(question: str) -> list[str]:
    """Split a compound question into sub-queries; [question] when there's no clean split."""
    q = question.strip()
    parts = _VS.split(q)
    if len(parts) < 2:
        parts = _AND.split(q)
    cleaned = []
    for p in parts:
        p = _LEAD.sub("", p.strip(" ?.,;:")).strip()
        if p:
            cleaned.append(p)
    if len(cleaned) < 2:
        return [q]
    return cleaned[:MAX_SUBQUERIES]


def _store_columns(stores: list, store_id: str) -> set:
    """Every column name the caller-visible metadata lists for one store. Names only -
    the planner is never shown a value (LAW 1), and neither is this."""
    for store in stores:
        if store.get("id") == store_id:
            return {c for t in (store.get("tables") or [])
                    for c in (t.get("columns") or []) if c}
    return set()


def _names_a_shared_key(parsed: dict, filter_q: str, stores: list,
                        filter_store: str, measure_store: str) -> bool:
    """#524: the filter half must project a key the MEASURE store also has, or the plan
    cannot possibly join and the rescue burns two model calls and two round-trips to
    reject itself.

    Measured live (findings s26): D-002's plan carried `product_category_name` into
    `olist-orders`, which has no such column - the shared key is `product_id`, two hops
    inside the catalog store. The system prompt already said "find a column name that
    appears in BOTH stores"; nothing checked that it had.

    DERIVED, not declared. The metadata already carries both stores' column names, so the
    intersection is computable here - no new field in the JSON contract, which matters
    because F-005 answers correctly through this parser today and a contract change would
    risk a working item to repair broken ones. A `key` the model volunteers is honoured,
    but only after the same check: a declaration is not evidence.

    Matching is word-wise. A substring test would let `xproduct_idx`, or any chatty
    sentence that happens to contain a short column name, satisfy the guard by accident."""
    shared = _store_columns(stores, filter_store) & _store_columns(stores, measure_store)
    if not shared:
        return False
    declared = str(parsed.get("key") or parsed.get("key_column") or "").strip()
    if declared:
        return declared in shared
    return any(re.search(rf"\b{re.escape(col)}\b", filter_q) for col in shared)


def _repaired_measure_store(parsed: dict, filter_q: str, stores: list,
                            filter_store: str) -> "str | None":
    """#524: the measure store the plan SHOULD have named, when the schema leaves exactly
    one possibility - else None, and the plan is refused.

    Measured live (findings s26): F-005's plan is right about everything that is hard -
    the filter half is `customer_id` from `olist-catalog`, which is the gold join key -
    and wrong about the one thing the SCHEMA already settles, naming `movielens` as the
    measure store (3/3, deterministic). Prompting did not fix this: the measure phrasing
    for that question names no column at all ("how much money ... through the
    marketplace"), so there is nothing in the wording for the model to ground on, and
    every prompt variant that taught the two-hop filter for D-002/E-004 broke F-005's
    store choice instead. That trade is not worth taking, and it does not have to be:
    which stores can receive this key is a fact about the metadata, not a judgment.

    So the model keeps the part that needs language - WHICH key, and how to phrase each
    half - and the schema decides the part that is mechanical. Fail closed on ambiguity:
    zero candidates is a plan that cannot join, and two or more is a genuine routing
    choice this function has no business making silently.

    LAW 2 holds because `stores` is already only what the caller can see, so a repair can
    never name a store the plan could not have named itself."""
    candidates = [s.get("id") for s in stores
                  if s.get("id") not in (filter_store, None)
                  and _names_a_shared_key({}, filter_q, stores, filter_store, s.get("id"))]
    return candidates[0] if len(candidates) == 1 else None


def llm_cross_store_planner(llm):
    """#474 (ADR 0014, option B): split ONE question whose filter column and measure
    column live in DIFFERENT stores into a filter half and a measure half.

    "What is the total item revenue from customers located in RJ?" contains no
    conjunction, so `decompose_query` can never split it - the split is visible only in
    the SCHEMA (customer_state in one store, price in another). The model is shown the
    caller-visible stores' metadata - ids, table and column NAMES, never a value (LAW 1)
    - and replies with strict JSON {"filter": ..., "measure": ...} or SINGLE.

    The model only PROPOSES the plan. Any failure - refusal, SINGLE, junk, a missing or
    empty half, a store id that is not in the caller-visible metadata, an exception -
    returns None, and the caller keeps today's behaviour (the honest decline). Same
    guard-the-raw-reply division of labour as llm_sql_generator.

    Each half NAMES its target store, and the ids are validated against the metadata the
    model was shown: the planner already knows which store holds which column, so the
    halves dispatch deterministically instead of re-entering the embedding routing
    lottery (measured: a correct filter half routed to the WRONG store on profile prose).

    Ordering is the contract: [(filter_store, filter_q), (measure_store, measure_q)].
    The filter half runs first so its shown key values carry into the measure bind."""
    def plan(question: str, stores: list) -> "list | None":
        try:
            raw = (llm.plan_cross_store(question, stores) or "").strip()
            raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw).strip()
            if not raw.startswith("{"):
                return None
            import json
            parsed = json.loads(raw)
            filter_q = (parsed.get("filter") or "").strip()
            measure_q = (parsed.get("measure") or "").strip()
            filter_store = (parsed.get("filter_store") or "").strip()
            measure_store = (parsed.get("measure_store") or "").strip()
            visible = {s.get("id") for s in stores}
            if not filter_q or not measure_q:
                return None
            if filter_store not in visible or measure_store not in visible:
                return None                    # a named store must be one the CALLER sees
            if filter_store == measure_store:
                return None                    # one store = not a cross-store question
            if not _names_a_shared_key(parsed, filter_q, stores,
                                       filter_store, measure_store):
                # #524: no key crosses to the named measure store, so this plan cannot
                # join. The schema may still settle it outright - see the repair.
                measure_store = _repaired_measure_store(parsed, filter_q, stores,
                                                        filter_store)
                if measure_store is None:
                    return None
            return [(filter_store, filter_q), (measure_store, measure_q)]
        except Exception:                      # noqa: BLE001 - a bad plan is no plan
            return None
    return plan


def llm_decomposer(llm, fallback=decompose_query):
    """#215: split a compound question so that EVERY half still carries the JOIN KEY.

    The regex split above is honest but lossy. "Which products generate the most support
    tickets, and how much revenue do they bring?" becomes ["...support tickets", "how much
    revenue do they bring"] — and that second half is a fragment whose subject is the pronoun
    "they". Handed to Azure SQL it produced `SELECT SUM(TotalDue)`: TOTAL company revenue, not
    revenue BY PRODUCT. The halves then cannot be joined, which defeats the entire point of a
    federated answer.

    So each sub-question must be STANDALONE: pronouns resolved, and the shared entity/grain
    ("per product SKU", "by region") carried into every half, because that grain IS the join
    key the synthesizer needs to line the results up.

    The model only PROPOSES the split. Any failure — refusal, empty, wrong shape, a lone half,
    an exception — falls back to the deterministic split, so a bad generation degrades to the
    honest answer rather than to nothing. Same discipline as `llm_sql_generator`.
    """
    def decompose(question: str) -> list[str]:
        try:
            parts = llm.decompose_question(question)
            if not isinstance(parts, list) or not parts:
                raise ValueError("empty or non-list decomposition")
            out = []
            for p in parts:
                if not isinstance(p, str) or not p.strip():
                    raise ValueError(f"bad sub-question: {p!r}")
                out.append(p.strip())
            # One part = "this isn't compound", which is a legitimate answer — but only when
            # the deterministic splitter agrees there is nothing to split. If the model
            # collapses a genuinely compound question to one half, that is a LOST half.
            if len(out) == 1 and len(fallback(question)) > 1:
                raise ValueError("model dropped a half of a compound question")
            return out[:MAX_SUBQUERIES]
        except Exception:
            return fallback(question)
    return decompose
