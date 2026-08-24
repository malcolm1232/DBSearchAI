"""Fan-out executor — turns a RoutingDecision into Evidence (Phase E E3, card #100).

Design §6 steps 6-8: authorize each SELECTED store (the gate-#2 seam), retrieve in
parallel under a per-dispatch deadline, record a StoreOutcome per store. A failing or
slow store is DROPPED with its outcome — it never fails the whole query (§10-E3).
Evidence arrives already permission-trimmed by each store (LAW 2); the executor only
collects. Timed-out worker threads are abandoned (cancel_futures + non-blocking
shutdown), so one hung source can't stall the answer.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from dataclasses import dataclass, field

from dbsearch.router.catalog import StoreCatalog
from dbsearch.router.decision import RoutingDecision
from dbsearch.router.evidence import Evidence
from dbsearch.router.structured import CannotAnswerFromSchema, SchemaUnavailable
from dbsearch.router.store import StorePort

OK = "ok"
EMPTY = "empty"
ERROR = "error"
TIMEOUT = "timeout"
BUDGET = "budget"    # E8: not dispatched — the query's dispatch ceiling was reached
DECLINED = "declined"   # #211: the store was asked, and it does NOT hold this kind of data.
# NOT the same as EMPTY. EMPTY means "I looked and found no matching rows"; DECLINED means "this
# question is not about anything I hold." Conflating them is how the fabrication happened: asked
# for support tickets, a sales database counted order lines and CALLED them support tickets.


@dataclass
class StoreOutcome:
    """Per-store dispatch result — the audit/disclosure record (§8 partial coverage)."""
    store_id: str
    business_unit: str
    status: str                  # OK | EMPTY | ERROR | TIMEOUT
    count: int = 0               # evidence rows the answer was actually built from
    error: str = ""
    sub_question: str = ""       # E6: which decomposed sub-query this dispatch served
    total: int = 0               # rows the query REALLY produced (#206); 0 = unknown/N/A
    note: str = ""               # #219: cross-source alignment note (metadata, disclosed)
    # #680: the part of a drop the READER can act on, kept apart from `error`.
    # `error` is a diagnostic - a stringified exception, class name and all. A missing cloud
    # link is different in kind: it is the one failure the user can fix themselves, and it
    # rendered identically to a timeout because the disclosure only ever printed `status`.
    # Recorded HERE rather than sniffed out of `error` downstream, because the exception
    # object (and its `.idp`) only exists at the point of the drop.
    remedy: str = ""
    # #940: this store had not FINISHED READING its source when it answered. Distinct from
    # every other field here, which describe what a completed read found: this one says the
    # read was not complete, so "your data matched no records" is a claim about a search that
    # had not finished. Defaults False so a store with no crawl - every federated SQL store -
    # keeps exactly the answer it had before, rather than telling users to wait for a sync
    # that does not exist.
    warming: bool = False
    # #727: True ONLY for the #680 unlinked-cloud drop. The disclosure renders that one as
    # "not connected" - correct there, and previously applied to EVERY remedied drop, which
    # dressed a schema fault as a missing cloud link and sent the user to re-link a cloud
    # that was already linked.
    unlinked: bool = False

    @property
    def truncated(self) -> bool:
        """`count` rows out of `total` — the answer is a sample, and must say so."""
        return self.total > self.count > 0

    def to_dict(self) -> dict:
        return {"store_id": self.store_id, "business_unit": self.business_unit,
                "status": self.status, "count": self.count, "error": self.error,
                "sub_question": self.sub_question, "total": self.total, "note": self.note}


@dataclass
class DispatchReport:
    evidence_by_store: dict[str, list[Evidence]] = field(default_factory=dict)
    outcomes: list[StoreOutcome] = field(default_factory=list)


def _one(store: StorePort, user_oid: str, question: str, top_k: int,
         bind_values: "list | None" = None, rank_source: bool = False,
         bind_column: "str | None" = None) -> list[Evidence]:
    access = store.authorize(user_oid)           # gate #2 seam (per-store AccessContext)
    # #219: a compound half after the first carries the previous half's join-key values. A store
    # that supports the semi-join binds to them; one that does not (Cosmos, indexed) runs unbound
    # and the OK branch discloses that the halves could not be aligned - honest degradation.
    if bind_values and hasattr(store, "retrieve_bound"):
        # #474 gate 3: the carried COLUMN name travels too, so a scalar half can push the
        # IN list into its own WHERE when it has no group-by column to wrap.
        return store.retrieve_bound(access, question, bind_values, top_k=top_k,
                                    column=bind_column)
    # #232: a compound half whose keys will be carried into a LATER half is a semi-join carry
    # source; order its breakdown by the MEASURE so the top_k it shows (and hands on) are the top
    # rows, not an alphabetical slice. A store without retrieve_ranked (doc/indexed) just runs
    # retrieve - it has no measure to rank by.
    if rank_source and hasattr(store, "retrieve_ranked"):
        return store.retrieve_ranked(access, question, top_k=top_k)
    return store.retrieve(access, question, top_k=top_k)


def execute(catalog: StoreCatalog, decision: RoutingDecision, user_oid: str, question: str,
            *, top_k: int = 5, timeout_s: float = 8.0,
            bind_values: "list | None" = None, rank_source: bool = False,
            bind_column: "str | None" = None) -> DispatchReport:
    targets = [(r, catalog.get(r.store_id).store) for r in decision.stores]
    report = DispatchReport()
    if not targets:
        return report
    pool = ThreadPoolExecutor(max_workers=len(targets))
    try:
        futures = [(routed, store,
                    pool.submit(_one, store, user_oid, question, top_k, bind_values, rank_source,
                                bind_column))
                   for routed, store in targets]
        deadline = time.monotonic() + timeout_s
        for routed, store, fut in futures:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                evidence = fut.result(timeout=remaining)
            except _FutureTimeout:
                # #719: the engine may know WHY this timed out - a serverless warehouse
                # waking from idle finishes at ~12s against this 8s budget, and the first
                # cold ask read as "broken" when it was just cold. Duck-typed like #680's
                # `.idp`: only SQL engines carry the hint, and an engine that has none (or
                # a store with no engine at all) answers with the plain timeout unchanged.
                eng = getattr(store, "_engine", None)
                hint = eng.cold_start_hint() if hasattr(eng, "cold_start_hint") else ""
                report.outcomes.append(StoreOutcome(routed.store_id, routed.business_unit,
                                                    TIMEOUT, remedy=hint))
                continue
            except SchemaUnavailable as exc:
                # #727: introspection returned ZERO tables - a source-side fault (privileges,
                # allowlist, a swallowed empty read), never evidence about the data. Must sit
                # ABOVE CannotAnswerFromSchema's handler conceptually and ahead of the generic
                # drop so the remedy survives: the message IS the user's instructions.
                report.outcomes.append(StoreOutcome(routed.store_id, routed.business_unit,
                                                    ERROR,
                                                    error=f"SchemaUnavailable: {exc}",
                                                    remedy=str(exc)))
                continue
            except CannotAnswerFromSchema as exc:
                # #211: NOT an error — the store is healthy and did the honest thing. It simply
                # does not hold this kind of data, and said so instead of inventing a column.
                report.outcomes.append(StoreOutcome(routed.store_id, routed.business_unit,
                                                    DECLINED, error=str(exc)))
                continue
            except Exception as exc:  # noqa: BLE001 — any store fault is a drop, never fatal
                # #680: `.idp` is the structural marker for "this caller has not linked that
                # cloud" — the same hasattr check health.py has used since #656, deliberately
                # not an import across the layer boundary. A drop carrying it is ACTIONABLE,
                # and the message is the user's instructions, not a diagnostic; anything else
                # is a fault they can do nothing about and gets no remedy (see the control in
                # selftest_680).
                report.outcomes.append(StoreOutcome(
                    routed.store_id, routed.business_unit, ERROR,
                    error=f"{type(exc).__name__}: {exc}",
                    remedy=str(exc) if hasattr(exc, "idp") else "",
                    unlinked=hasattr(exc, "idp")))
                continue
            if evidence:
                report.evidence_by_store[routed.store_id] = evidence
                # a store that knows how many rows its query really produced says so (#206)
                total = max((e.provenance.get("total_rows", 0) or 0) for e in evidence)
                outcome = StoreOutcome(routed.store_id, routed.business_unit,
                                       OK, count=len(evidence), total=total)
                # #219: if we asked this half to bind to half A's keys, say how it went. A store
                # that could not align (no retrieve_bound, or it fell open) is disclosed; a store
                # that aligned but had to drop unsafe key values discloses the drop.
                if bind_values:
                    bind = (evidence[0].provenance or {}).get("bind")
                    if not (bind and bind.get("aligned")):
                        outcome.note = "could not be aligned to the other half on a shared key"
                    elif bind.get("dropped"):
                        outcome.note = (f"{bind['dropped']} key value(s) dropped by the safety "
                                        f"allowlist before alignment")
                # #482: this store's query was NOT the model's - generation failed and it
                # degraded to the deterministic keyword query. The rows are real, but they
                # answer a naive question rather than the one that was asked, and the
                # disclosure is built from these outcomes, so it has to say so here.
                degraded = (evidence[0].provenance or {}).get("degraded")
                if degraded:
                    outcome.note = ((outcome.note + "; ") if outcome.note else "") + (
                        f"answered with the fallback query - SQL generation degraded "
                        f"({degraded})")
                report.outcomes.append(outcome)
            else:
                # #940: a store still reading its source is not a store that holds nothing.
                # Duck-typed exactly like `_engine` (#719) and `.idp` (#680) on this same
                # path - only a connector-backed store can report freshness, and one that
                # cannot is settled by definition.
                fresh = store.freshness() if hasattr(store, "freshness") else ""
                report.outcomes.append(StoreOutcome(routed.store_id, routed.business_unit,
                                                    EMPTY,
                                                    warming=str(fresh).startswith("syncing")))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # abandon hung workers (py>=3.9)
    return report
