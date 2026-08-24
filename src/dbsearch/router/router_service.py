"""RouterQueryService — the 'which DB is suitable' brains (Phase E E2, card #99).

route() classifies the query, scores the caller's VISIBLE stores by profile similarity,
and picks one store or a fan-out set via the hybrid selector — returning an explainable
RoutingDecision. It does NOT retrieve or synthesize (that is E3): the decision is the
product. Gate #1 is the routing boundary — only catalog.visible_stores() are ever scored,
selected, or named, so a routing explanation can never reveal a store the user can't see.

E6 (card #103): a compound question is decomposed into sub-queries, each routed
INDEPENDENTLY over the same visible catalog (gate #1 per sub) and executed with its own
question text. The decomposer is injectable (LLM in prod, deterministic default).
E7 (card #104): store_override manually pins a VISIBLE store (method 'manual').
E8 (card #105): scale & governance —
  · route cache keyed by (principal set, question, override, catalog.revision): a
    recompose or late registration invalidates naturally; LRU + TTL bounded.
  · coarse→fine BU prune (deferred from E2): at >= coarse_min_bus business units the
    BU centroids are scored first and only surviving BUs' stores are fine-scored.
  · dispatch budget: one query (including all compound sub-queries) never dispatches
    more than max_dispatches stores — the remainder become BUDGET outcomes and are
    DISCLOSED (no silent caps).
  · store profile vectors are warmed at construction, not on the first query.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import replace
from typing import Callable

from dbsearch.ports.base import EmbeddingPort, IdentityPort, LlmPort
from dbsearch.router.catalog import StoreCatalog
from dbsearch.router.classify import classify_query
from dbsearch.router.decompose import decompose_query
from dbsearch.router.decision import RoutedStore, RoutingDecision, SubQuery, qualify
from dbsearch.router.executor import BUDGET, OK, DispatchReport, StoreOutcome, execute
from dbsearch.router.profiles import (
    coarse_prune, distinctive_narrow, ensure_profile_vector, score_stores,
)
from dbsearch.router.selector import select_stores
from dbsearch.router.structured import KEY_CARRY_CAP, bind_values_from_evidence
from dbsearch.router.synthesizer import (  # noqa: F401 - _is_refusal shared on purpose
    RouterResult, _is_refusal, synthesize,
)


class RouterQueryService:
    def __init__(self, catalog: StoreCatalog, identity: IdentityPort, embedder: EmbeddingPort,
                 *, tiebreak: Callable[[list[str]], list[str]] | None = None,
                 decomposer: Callable[[str], list[str]] | None = None,
                 cross_store_planner: "Callable[[str, list], list | None] | None" = None,
                 margin: float = 0.15, fanout_cap: int = 3, floor_frac: float = 0.6,
                 max_dispatches: int = 8, coarse_min_bus: int = 6,
                 coarse_floor_frac: float = 0.5,
                 cache_size: int = 256, cache_ttl_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._catalog = catalog
        self._identity = identity
        self._embedder = embedder
        self._tiebreak = tiebreak
        self._decomposer = decomposer or decompose_query
        # #474 (ADR 0014-B): the schema-aware cross-store planner. None = no rescue.
        self._cross_store_planner = cross_store_planner
        self._margin = margin
        self._fanout_cap = fanout_cap
        self._floor_frac = floor_frac
        self._max_dispatches = max_dispatches
        self._coarse_min_bus = coarse_min_bus
        self._coarse_floor_frac = coarse_floor_frac
        self._cache: "OrderedDict[tuple, tuple[float, RoutingDecision]]" = OrderedDict()
        self._cache_size = cache_size
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._hits = 0
        self._misses = 0
        # E8: warm every store's profile vector now — user-independent, and the first
        # real query shouldn't pay the whole catalog's embedding cost.
        for n in catalog.stores():
            if n.profile is not None:
                ensure_profile_vector(n.profile, embedder)

    def cache_stats(self) -> dict:
        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}

    def route(self, user_oid: str, question: str,
              store_override: str | None = None) -> RoutingDecision:
        principals = self._identity.expand_groups(user_oid)
        # E8 cache: keyed by the PRINCIPAL SET (not the oid — same visibility, same
        # decision) + catalog revision, so recompose/registration invalidates.
        key = (tuple(sorted(principals)), question, store_override or "",
               self._catalog.revision)
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            self._hits += 1
            self._cache.move_to_end(key)
            return hit[1]
        self._misses += 1
        decision = self._route_fresh(principals, question, store_override)
        self._cache[key] = (now + self._cache_ttl_s, decision)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return decision

    def _route_fresh(self, principals: list[str], question: str,
                     store_override: str | None) -> RoutingDecision:
        query_type = classify_query(question)
        if store_override:
            # E7 manual pin: the user's choice beats routing AND decomposition — but
            # never gate #1 (an invisible/nonexistent override gets one generic fallback).
            return self._route_manual(principals, question, query_type, store_override)
        if query_type == "compound":
            subs = self._decomposer(question)
            if len(subs) >= 2:
                # #503: a split is only real when the halves belong to DIFFERENT stores -
                # the same test the #134 rescue below has always applied, which this
                # branch was missing. Without it, " and " inside one stored VALUE was
                # taken as a clause joint: "products in the health and beauty category"
                # reached the stores as "...in the health" + "beauty category", failing
                # both halves (D-002/E-004, findings s20).
                # NOTE (#519, findings s25): this branch no longer carries the
                # rescue-eligibility argument it was written with. It cannot - measured
                # live, those torn fragments route to DIFFERENT stores, so the same-store
                # test correctly declines and never fires on the very items it was built
                # for. Eligibility now comes from ask() instead, which no longer requires
                # an un-decomposed question. What survives here is the narrower claim:
                # when the halves DO share a store, the split was not a real split.
                # Checking the halves' STORES decides it on data instead of on
                # grammar, and it repairs any bad split, not just this one - including
                # the case measured live, where the LLM decomposer correctly answered
                # "not compound" and llm_decomposer's dropped-half guard overrode it with
                # the regex's torn halves.
                compound = self._route_compound(principals, subs)
                routed_ids = {sq.decision.stores[0].store_id
                              for sq in compound.sub_queries if sq.decision.stores}
                # An UNROUTED half keeps the compound alive even though it contributes no
                # store id. Collapsing there would be a silent honesty loss, not a repair:
                # a half no visible source can answer is exactly what compound_disclosure
                # reports ("Not covered: '...'"), and under LAW 2 that half may be unrouted
                # precisely BECAUSE the store holding it is invisible to this caller - so
                # the same question would collapse for one user and decompose for another,
                # and the less-privileged user would silently lose the disclosure that
                # tells them half their question went unanswered. (Caught by
                # selftest_router_e6's partial-visibility case, not by reasoning.)
                uncovered = any(not sq.decision.stores for sq in compound.sub_queries)
                if len(routed_ids) >= 2 or uncovered:
                    return compound
            query_type = "semantic"       # classifier over-triggered; no clean split
        else:
            # #134 under-trigger rescue: keyword dominance (e.g. "total ... amount")
            # can mislabel an "and"-joined question, silently dropping the other
            # half. If the decomposer finds a clean split AND the halves route to
            # DIFFERENT stores, the question genuinely spans stores — treat it as
            # compound. Same-store splits (idioms like "terms and conditions") fall
            # through to plain single routing.
            subs = self._decomposer(question)
            if len(subs) >= 2:
                compound = self._route_compound(principals, subs)
                routed_ids = {sq.decision.stores[0].store_id
                              for sq in compound.sub_queries if sq.decision.stores}
                if len(routed_ids) >= 2:
                    return compound

        return self._route_single(principals, question, query_type)

    def _route_single(self, principals: list[str], question: str,
                      query_type: str) -> RoutingDecision:
        visible = self._catalog.visible_stores(principals)          # gate #1
        if not visible:
            # #218: distinguish "nothing is composed" from "nothing is visible TO YOU". They are
            # completely different facts and they were collapsing into one permissions-flavoured
            # sentence ("I couldn't find anything you have access to"), which sent the user
            # hunting for a permissions problem when the real answer was "you haven't pressed
            # Compose up yet". LAW 2 is why the distinction has to be drawn HERE and drawn
            # carefully: saying "no sources are connected" is safe ONLY when the catalog is
            # genuinely empty, because then there is nothing whose existence could leak. The
            # moment ANY store exists, we say nothing about it - an invisible store must stay
            # indistinguishable from a nonexistent one (design §8, scenario G).
            empty_catalog = not self._catalog.stores()
            return RoutingDecision(
                query_type=query_type, method="fallback",
                reason=("no store is composed yet" if empty_catalog
                        else "no accessible store for this user"))

        qv = self._embedder.embed([question])[0]                # the question becomes ONE vector,
                                                                # same embedder the profiles used
        nodes = visible                                         # pool = visibility-trimmed catalog
                                                                # (LAW 2: unseen stores never enter)
        bus = {n.profile.business_unit for n in visible if n.profile is not None}
        if len(bus) >= self._coarse_min_bus:                    # E8 coarse→fine: at scale (6+ BUs),
            nodes = coarse_prune(qv, visible, self._embedder, self._coarse_floor_frac)
                                                                # drop whole BUs by their BEST store
        candidates = score_stores(qv, nodes, self._embedder, question)
                                                                # fine pass: one cosine per survivor
        selected, method, confidence = select_stores(
            candidates, margin=self._margin, fanout_cap=self._fanout_cap,
            floor_frac=self._floor_frac, tiebreak=self._tiebreak,
        )
        # #288: a prefilter fan-out whose question DISTINCTIVELY names one tied store is scoped
        # to it (embedding ties on shared tokens like "amount"/"region"; the distinctive token,
        # e.g. "deal"/"order", disambiguates). A true cross-store ask keeps its fan-out.
        if method == "prefilter" and len(selected) > 1:
            narrowed = distinctive_narrow(question, selected, nodes)
            if len(narrowed) < len(selected):
                selected = narrowed
        return RoutingDecision(
            query_type=query_type, stores=selected, candidates=candidates,
            confidence=confidence, method=method, reason=_reason(selected, method),
        )

    def _route_manual(self, principals: list[str], question: str, query_type: str,
                      store_id: str) -> RoutingDecision:
        """E7: pin one store by id. Only a VISIBLE store can be pinned; an invisible or
        nonexistent id gets the SAME generic fallback (scenario G: the response must
        never confirm the store exists)."""
        visible = self._catalog.visible_stores(principals)           # gate #1
        target = next((n for n in visible if n.id == store_id), None)
        if target is None:
            return RoutingDecision(query_type=query_type, method="fallback",
                                   reason="no accessible store matches the requested override")
        qv = self._embedder.embed([question])[0]
        candidates = score_stores(qv, visible, self._embedder, question)
        routed = next((c for c in candidates if c.store_id == store_id),
                      RoutedStore(store_id, target.profile.business_unit if target.profile
                                  else "", 0.0))
        return RoutingDecision(
            query_type=query_type, stores=[routed], candidates=candidates,
            confidence=routed.score, method="manual",
            reason=f"manually pinned to {qualify(store_id, routed.business_unit)}",
        )

    def _route_compound(self, principals: list[str], subs: list[str]) -> RoutingDecision:
        """E6: route each sub-query independently; parent = union of the sub-selections.
        Confidence is the WEAKEST sub (honest: a compound answer is only as routable as
        its least-routable half)."""
        sub_queries = [
            SubQuery(q, self._route_single(principals, q, classify_query(q))) for q in subs
        ]
        stores: list[RoutedStore] = []
        candidates: list[RoutedStore] = []
        seen_s: set[str] = set()
        seen_c: set[str] = set()
        for sq in sub_queries:
            for s in sq.decision.stores:
                if s.store_id not in seen_s:
                    seen_s.add(s.store_id)
                    stores.append(s)
            for c in sq.decision.candidates:
                if c.store_id not in seen_c:
                    seen_c.add(c.store_id)
                    candidates.append(c)
        routed = [sq for sq in sub_queries if sq.decision.stores]
        confidence = min((sq.decision.confidence for sq in routed), default=0.0)
        # #753: the reason used to enumerate every sub-question and its target store — which is
        # exactly what `sub_queries` carries, structurally, on the same response. Both consumers
        # render BOTH: the canvas prints the reason inside the trace and then its own "↳ question
        # → store" lines, and cli.py does the identical pair. So each sub-question appeared twice
        # on one screen (three times counting the per-outcome rows, which quote it again).
        #
        # The structured field wins and the prose stops competing with it: it is the one a client
        # can render properly — naming the query_type, and distinguishing "no accessible source"
        # from a store — while a semicolon-joined string can only be printed.
        #
        # CORRECTION (#753 round 2). The commit that made this change also claimed a safety
        # rationale — "selftest_478 requires that a decision reason NOT name a store" — and that
        # is false. selftest_478 forbids naming a store the caller CANNOT SEE (its assertion is
        # `"baseball-payroll" not in decision.reason`, a LAW 2 leak check), and
        # selftest_router_route.py:47 asserts the OPPOSITE for the semantic path: `"hr-wiki" in
        # d.reason`. Naming an authorised store in a reason is required elsewhere in this repo.
        # I generalised a leak test into a repo-wide rule to justify a change I had already made.
        # The duplication argument above is the whole of the case for it, and it stands on its own.
        return RoutingDecision(
            query_type="compound", stores=stores, candidates=candidates,
            confidence=confidence, method="decompose",
            reason=f"decomposed into {len(sub_queries)} sub-queries",
            sub_queries=sub_queries,
        )

    def ask(self, user_oid: str, question: str, llm: LlmPort,
            *, top_k: int = 5, timeout_s: float = 8.0,
            store_override: str | None = None) -> RouterResult:
        return self._ask(user_oid, question, llm, top_k=top_k, timeout_s=timeout_s,
                         store_override=store_override)

    def _ask(self, user_oid: str, question: str, llm: LlmPort,
             *, top_k: int = 5, timeout_s: float = 8.0,
             store_override: str | None = None, on_token=None) -> RouterResult:
        """E3: the full federated path — route → fan-out execute → synthesize.
        route() stays pure/explainable; this composes it. Gates: #1 inside route(),
        #2 at each store's authorize(), #3 in synthesize() (post-trim evidence only).
        E6: each sub-query dispatches with its OWN question text; reports merge, and
        every outcome is stamped with the sub-question it served (audit + disclosure).
        E7: store_override pins the dispatch to one visible store (method 'manual').
        E8: at most max_dispatches stores dispatch per query (across ALL sub-queries);
        the remainder are BUDGET outcomes, surfaced in the disclosure.

        #689: `on_token` streams the FIRST synthesis for `ask_stream`. Only that one, and
        never the rescue's - a rescued answer replaces the first wholesale, so streaming both
        would put two answers on screen for one question."""
        decision = self.route(user_oid, question, store_override=store_override)
        budget = self._max_dispatches
        if decision.sub_queries:
            report = self._dispatch_sub_queries(decision, user_oid, top_k, timeout_s, budget)
        else:
            allowed = decision.stores[:max(0, budget)]
            capped = decision.stores[len(allowed):]
            report = execute(self._catalog, replace(decision, stores=allowed),
                             user_oid, question, top_k=top_k, timeout_s=timeout_s)
            report.outcomes.extend(
                StoreOutcome(s.store_id, s.business_unit, BUDGET) for s in capped)
        decision = self._also_consult(user_oid, question, decision, report, top_k, timeout_s)
        result = synthesize(question, report, decision, llm, on_token=on_token)
        # #474: the rescue keys off the product's OWN final judgment, not off empty
        # evidence - and the judgment is the REFUSAL FAMILY, not the canonical constants
        # alone. THE bug that kept D at 0/5 live while the offline proof reached gold:
        # the ANSWER prompt tells the model to "say plainly you do not have that
        # information", so live declines are LLM-AUTHORED text that never equals any
        # constant, and a constants-only trigger meant the rescue never fired on the
        # served path at all. A delivered answer - even a wrong one - is never
        # overridden (that would be a different, dangerous feature).
        # #519: NOT gated on `not decision.sub_queries`. A decomposition that FAILED is
        # the strongest reason to try the rescue, not a reason to skip it. #503 above
        # collapses a bad split only when the halves share a store; measured live
        # (findings s25) the torn fragments route to DIFFERENT stores - "beauty category"
        # goes to the catalog because that is what the words say - so that repair cannot
        # reach D-002/E-004, and the gate here then locked out the one mechanism that
        # answers this shape correctly. Asked the SAME question, the planner returns the
        # gold join. The rescue plans on the WHOLE question, so a torn fragment never
        # reaches the planner, and the trigger is still a REFUSAL - a decomposed question
        # that answered is never overridden.
        if _is_refusal(result.answer) and store_override is None:
            rescued = self._cross_store_rescue(user_oid, question, top_k, timeout_s)
            if isinstance(rescued, tuple):
                report, decision = rescued
                return synthesize(question, report, decision, llm)
            if rescued:
                # LAW 8: a rejected rescue says WHY. From outside, "never fired" and
                # "fired and rejected" looked identical - which cost a day of
                # live-vs-offline confusion chasing the wrong links in the chain.
                note = f"Cross-store rescue attempted and rejected: {rescued}."
                result.disclosure = f"{result.disclosure} {note}".strip()
        return result

    def _also_consult(self, user_oid: str, question: str, decision: RoutingDecision,
                      report, top_k: int, timeout_s: float) -> RoutingDecision:
        """#856: ask the stores that are not CANDIDATES but PLANES, and merge what they say.

        A catalog may declare that some node is asked on every turn rather than ranked against
        the others. Today exactly one does: the ask surface's overlay, whose node is the
        caller's own uploaded documents (`DocsOverlayCatalog.always_consulted`). A plain
        StoreCatalog has no such method, so `/router/ask`, the canvas and every composed
        manifest route exactly as before - this is an ASK-surface rule, not a router-wide one.

        WHY A PLANE AND NOT A BETTER SCORE. Measured on prod: "How many days of annual leave do
        I get?" scored a composed HR folder at 0.1333 ("analytical/exact") and the documents
        node at 0.0556, so the folder took the whole ask - and answered from its ABBREVIATED
        copy of the leave policy, losing the "30 days after five years of service" clause that
        the caller's own upload carries. Boosting the documents score was the obvious
        alternative and `ask_router.py`'s header refuses it in advance: that description is
        deliberately generic, and narrowing it makes the router prefer a database for the
        questions the documents actually answer. The asymmetry is real - a database is one of
        many sources competing to be relevant, while "what did this person's own files say" is
        worth asking every time - so it belongs in WHETHER we ask, not in the scoring.

        This is also what the CANVAS has always done. `canvas.js` fires the #255 document
        search alongside the router on every ask, says "also searched your documents ... none
        answered this" when it comes back empty, and retracts the router's abstention when it
        answers. /ask leaving documents to compete was the same two-surfaces-one-question
        divergence #689 is about.

        ADDITIVE TO THE BUDGET, deliberately. `_max_dispatches` caps how many of N competing
        databases a question may fan out to; the caller's own corpus is not one of the N and
        must not displace a store that won on merit.

        NOT ON A MANUAL PIN. E7 exists so a user can say "answer from THIS store", and
        widening that would overrule an explicit choice - the one place where always must not
        mean always."""
        always = getattr(self._catalog, "always_consulted", None)
        if always is None or decision.method == "manual":
            return decision
        already = {s.store_id for s in decision.stores}
        extra = [RoutedStore(n.id,
                             n.profile.business_unit if n.profile is not None else "",
                             next((c.score for c in decision.candidates
                                   if c.store_id == n.id), 0.0))
                 for n in always(self._identity.expand_groups(user_oid))
                 if n.id not in already]
        if not extra:
            return decision
        side = execute(self._catalog, replace(decision, stores=extra, sub_queries=[]),
                       user_oid, question, top_k=top_k, timeout_s=timeout_s)
        # The WHOLE question, even for a compound: this plane is a semantic index, and it is
        # the same question `/search` would be handed on the same surface.
        report.evidence_by_store.update(side.evidence_by_store)
        report.outcomes.extend(side.outcomes)
        # The decision has to say so too, or the trace and the disclosure would describe an
        # ask that did not happen - LAW 8, and the line a reader checks the answer against.
        return replace(decision, stores=list(decision.stores) + extra)

    def ask_stream(self, user_oid: str, question: str, llm: LlmPort,
                   *, top_k: int = 5, timeout_s: float = 8.0,
                   store_override: str | None = None):
        """#689 / ADR 0025: `ask()` as an event stream, for the conversational surface.

        Yields `{"type":"token","text":...}` while the first synthesis generates, then one
        `{"type":"done", **RouterResult.to_dict()}`. The contract is deliberately the SAME
        shape `QueryService.answer_stream` yields, because `ConversationService.ask_stream`
        consumes either one and must not learn which plane answered.

        `ask()` IS NOT REIMPLEMENTED HERE. Routing, the budget, the compound dispatch and the
        rescue are one body with one set of rules, and a second copy is where the canvas and
        Ask would drift into answering the same question differently - which is the defect
        #689 reports. `ask()` and this share `_ask` and differ only in what they do with the
        tokens.

        The rescue's second synthesis is NOT streamed, and the `done` answer is what the
        client renders: a rescued answer replaces the streamed draft wholesale (see
        `synthesize`'s `on_token` docstring)."""
        import queue
        import threading

        # A BOUNDED hand-off, not a buffer. `ask()` is a plain call and must stay one body,
        # so the generation runs on a worker and its `on_token` callback publishes here while
        # this generator drains. Collecting the tokens and yielding them afterwards would be
        # simpler and would silently make the routed surface non-streaming - the user waits
        # for the whole answer where the document path drips it, which is a regression dressed
        # as an implementation detail.
        #
        # maxsize is what makes it a hand-off: a fast model and a slow client cannot grow an
        # unbounded list in memory, the worker simply blocks until the client catches up.
        chan: "queue.Queue" = queue.Queue(maxsize=64)
        _DONE = object()
        box: dict = {}

        def _run():
            try:
                box["result"] = self._ask(user_oid, question, llm, top_k=top_k,
                                          timeout_s=timeout_s, store_override=store_override,
                                          on_token=chan.put)
            except BaseException as exc:      # noqa: BLE001 - re-raised on the caller's thread
                box["error"] = exc
            finally:
                chan.put(_DONE)

        # Daemon: a client that disconnects mid-answer abandons this generator, and a
        # non-daemon worker blocked on a full queue would then hold the process at shutdown.
        worker = threading.Thread(target=_run, name="router-ask-stream", daemon=True)
        worker.start()
        while True:
            tok = chan.get()
            if tok is _DONE:
                break
            yield {"type": "token", "text": tok}
        worker.join()
        if "error" in box:
            # The failure surfaces on the CALLER's thread, so /chat/stream's error handling
            # and the client's catch see it exactly as they would from a one-shot ask.
            raise box["error"]
        yield {"type": "done", **box["result"].to_dict()}

    def _dispatch_sub_queries(self, decision: RoutingDecision, user_oid: str,
                              top_k: int, timeout_s: float, budget: int) -> DispatchReport:
        """The sequential compound dispatch (#219): after each half, carry the
        (join_column, values) it SHOWED into the next half's bind (WHERE key IN ...). The
        first half runs unbound; a half that carries nothing leaves the next unbound too.
        The bind itself is mechanical and sanitized in the store - no customer value ever
        reaches an LLM. Shared verbatim by the compound route and the #474 rescue."""
        report = DispatchReport()
        carry: "tuple | None" = None            # (column, values) - #474: the name travels
        last = len(decision.sub_queries) - 1
        for i, sq in enumerate(decision.sub_queries):
            allowed = sq.decision.stores[:max(0, budget)]
            capped = sq.decision.stores[len(allowed):]
            budget -= len(allowed)
            # #232: a half BEFORE the last is a semi-join carry source - order its breakdown by
            # the measure so the keys it hands on are its true top-N, not an alphabetical slice.
            sub_report = execute(self._catalog, replace(sq.decision, stores=allowed),
                                 user_oid, sq.question, top_k=top_k, timeout_s=timeout_s,
                                 bind_values=carry[1] if carry else None,
                                 bind_column=carry[0] if carry else None,
                                 rank_source=(i < last))
            for o in sub_report.outcomes:
                o.sub_question = sq.question
            for sid, ev in sub_report.evidence_by_store.items():
                report.evidence_by_store.setdefault(sid, []).extend(ev)
            report.outcomes.extend(sub_report.outcomes)
            report.outcomes.extend(
                StoreOutcome(s.store_id, s.business_unit, BUDGET,
                             sub_question=sq.question) for s in capped)
            carry = _carried_join_values(sub_report)
        return report

    def _cross_store_rescue(self, user_oid: str, question: str, top_k: int,
                            timeout_s: float) -> "tuple | None":
        """#474 (ADR 0014, option B): when the plain path produced NO evidence, try the
        one question shape fan-out structurally cannot answer - filter in one store,
        measure in another - as a planned key-carry compound.

        Three properties make this safe to bolt on:
        - it fires only on a decline, so an answering question can never regress here;
        - the planner sees the CALLER-visible stores' metadata only - ids, table and
          column names, never a value (LAW 1) and never an invisible store (LAW 2);
        - the rescue's report is trusted ONLY when some half's evidence is genuinely
          BOUND to carried keys (aligned) - an unaligned rescue is an ordinary unbound
          answer to a question the plain path already declined, which is exactly the
          resurrection class the #495/G-001 lesson pinned. Reject it; decline stands."""
        if self._cross_store_planner is None:
            return None                       # feature off: no disclosure noise
        principals = self._identity.expand_groups(user_oid)
        stores_meta = self._visible_sql_schemas(principals)
        if len(stores_meta) < 2:
            return "fewer than two SQL stores are visible, nothing to join across"
        halves = self._cross_store_planner(question, stores_meta)
        if not halves or len(halves) != 2:
            return "the planner judged this a single-store question (or its plan was invalid)"
        # The planner NAMES each half's store (validated against the caller-visible
        # metadata by the factory), so dispatch is deterministic - measured live, a
        # correct filter half re-entering embedding routing landed on the wrong store,
        # because routing scores profile PROSE while the plan was made on the SCHEMA.
        sub_queries = []
        for store_id, half_q in halves:
            node = self._catalog.get(store_id)
            bu = node.profile.business_unit if node.profile else ""
            routed = [RoutedStore(store_id, bu, 1.0, why="named by the cross-store planner")]
            sub_queries.append(SubQuery(half_q, RoutingDecision(
                query_type="analytical", stores=routed, candidates=routed,
                confidence=1.0, method="cross-store-rescue",
                reason=f"the planner placed this half on {store_id}")))
        compound = RoutingDecision(
            query_type="compound", stores=[sq.decision.stores[0] for sq in sub_queries],
            candidates=[sq.decision.stores[0] for sq in sub_queries],
            confidence=1.0, method="cross-store-rescue",
            reason="cross-store key-carry rescue (#474, ADR 0014-B)",
            sub_queries=sub_queries)
        # The filter half must SHOW its whole key set or the projection-carry cliff
        # refuses (D-001's filter matches 296 customers; a top_k of 5 could never carry
        # them). The carry budget is the same disclosed cap the bind enforces; the
        # synthesizer's evidence merge stays capped regardless.
        # Each rescue half needs a FRESH LLM generation, and the plain path's snappy 8s
        # per-store budget reproducibly kills it: offline (timeout_s=120) D-001 reached
        # its gold; live (timeout_s=8) half A timed out, nothing carried, and the
        # aligned-trust rule rejected every rescue - D stayed 0/5. The trade here is
        # different from the plain path's: this code only runs on a question that has
        # ALREADY declined, so the user's alternative is no answer at all. Latency for
        # an answer is the right trade; the plain path's budget is untouched.
        report = self._dispatch_sub_queries(compound, user_oid, max(top_k, KEY_CARRY_CAP),
                                            max(timeout_s, 90.0), self._max_dispatches)
        aligned = any(((ev.provenance or {}).get("bind") or {}).get("aligned")
                      for evs in report.evidence_by_store.values() for ev in evs)
        if not aligned:
            carried = any(((ev.provenance or {}).get("bind") or {}) != {}
                          for evs in report.evidence_by_store.values() for ev in evs)
            return ("the measure half could not be aligned to the carried keys"
                    if carried else "the filter half carried no key values")
        # Measured live (cap_sql_a/b/c + cap_sql2_a/b/c 260805, D 0/5 WITH the rescue
        # dispatching, findings s18/s19): both halves succeeded, SUM(price)=39180.04 sat
        # in the evidence, and the synthesizer declined anyway. The decisive variants:
        # 11 carry-key rows beside the fact row -> decline; TWO -> still a decline; the
        # fact row + a readable proof alone -> the exact gold in prose. The filter
        # half's key rows are MECHANICS - the bind consumed them, and the measure SQL
        # shown as proof names every carried key - so they are marked mechanics (the
        # synthesizer's prompt skips them; evidence/citations keep them for the reader),
        # trimmed to 2 samples, and the condensation is disclosed (LAW 8).
        filter_sq = compound.sub_queries[0]
        fstore = filter_sq.decision.stores[0].store_id
        evs = report.evidence_by_store.get(fstore) or []
        kept = evs[:2]
        for ev in kept:
            if ev.provenance is None:
                ev.provenance = {}
            ev.provenance["mechanics"] = True
        if len(evs) > 2:
            report.evidence_by_store[fstore] = kept
            for o in report.outcomes:
                if o.store_id == fstore and o.sub_question == filter_sq.question and o.status == OK:
                    o.note = ((o.note + "; ") if o.note else "") + (
                        f"filter half matched {len(evs)} key rows, condensed to 2 samples "
                        f"for the answer - every key was carried into the measure query")
        return report, compound

    def _visible_sql_schemas(self, principals: list[str]) -> list:
        """The caller-visible SQL stores' schema METADATA for the planner: id, title,
        table and column names. Built from visible_stores() so it can never name a store
        the caller isn't cleared to see (LAW 2), and it carries no values (LAW 1)."""
        out = []
        for node in self._catalog.visible_stores(principals):
            store = node.store
            described = getattr(store, "described_schema", None)
            if described is None:
                continue
            try:
                tables = [{"table": t.get("table", ""),
                           "columns": [c.get("name", "") for c in t.get("columns", [])]}
                          for t in described()]
            except Exception:                  # noqa: BLE001 - an unreadable store plans nothing
                continue
            if tables:
                out.append({"id": node.id,
                            "title": node.profile.title if node.profile else node.id,
                            "tables": tables})
        return out


def _carried_join_values(sub_report: DispatchReport) -> "tuple | None":
    """#219/#474: the (join_column, values) THIS half showed, to bind the NEXT half. Takes
    the first store's evidence that groups on a key or projects a single column; returns
    None when nothing carries (the next half runs unbound). The column NAME rides along so
    a scalar next half can bind without a group-by column of its own (#474 gate 3)."""
    for ev in sub_report.evidence_by_store.values():
        col, values = bind_values_from_evidence(ev)
        if values:
            return col, values
    return None


def _reason(selected: list[RoutedStore], method: str) -> str:
    if not selected:
        return "no store cleared the relevance floor"
    # #729: "unassigned" is the placeholder a store carries when nobody has given it a business
    # unit. It names nothing, so printing "azure_sql-1 (unassigned)" spends the reader's
    # attention on a word that cannot help them. The two canvas surfaces that show a business
    # unit already drop it; this is the third and last, and leaving one of the three disagreeing
    # with the others reads as the product being unsure whether the word means anything.
    named = ", ".join(qualify(s.store_id, s.business_unit) for s in selected)
    lead = "routed to" if len(selected) == 1 else "fanned out to"
    tail = " — chosen by tiebreak" if method == "llm" else ""
    return f"{lead} {named}{tail}"
