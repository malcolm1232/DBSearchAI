"""Heterogeneous synthesis — merged Evidence → ONE cited answer (Phase E E3, card #100).

Gate #3 (trim-before-synthesis): everything entering here is already per-store trimmed
(LAW 2); merging is subtractive/re-ordering ONLY. The merge signal is per-store RANK,
never raw score — native/analytical scores aren't comparable across families (ADR 0008).
Citations pass provenance through polymorphically (chunk → doc/uri/locator; row →
sql/table/row_ids). Partial coverage is DISCLOSED, never silently backfilled (§8).
"""
from __future__ import annotations

import os
import re

from dataclasses import dataclass, field

from dbsearch.ports.base import LlmPort
from dbsearch.router.decision import RoutingDecision, qualify
from dbsearch.router.evidence import CHUNK, RECORD, Evidence
from dbsearch.router.executor import (
    BUDGET, DECLINED, EMPTY, ERROR, OK, TIMEOUT, DispatchReport, StoreOutcome,
)
from dbsearch.router.provenance import ProvenanceError, normalize_proof

from dbsearch.core.copy import NO_EVIDENCE_ANSWER      # noqa: F401  (re-exported below)
NOT_COMPOSED_ANSWER = ("No data sources are connected yet, so there is nothing to search. "
                       "Connect a source and press Compose up, then ask again.")
DECLINED_ANSWER = ("None of the sources you can see hold that kind of data, so I have not "
                   "guessed an answer from something else.")
FAILED_ANSWER = ("I could not complete that query - the source it routed to did not respond "
                 "successfully, so I have no result to show you rather than a wrong one.")
EMPTY_RESULT_ANSWER = ("That query ran against your data and matched no records. The source is "
                       "there and readable - it simply holds nothing that fits.")
# #940: the same empty result, from a source we have not finished reading. EMPTY_RESULT_ANSWER
# above is three positive claims, and the last one - "it simply holds nothing that fits" - was
# FALSE on prod every time a deploy recreated the container: a connector store's index lives in
# the process, so it comes back empty and re-crawls, and during that window the question that
# had just answered with three citations was told the folder holds nothing. This says the one
# thing that is true, claims nothing about the contents, and tells the user it is temporary.
WARMING_ANSWER = ("I am still reading that source, so I cannot yet tell you what it does or "
                  "does not contain. It is syncing now - ask again in a moment.")


def no_evidence_answer(decision, outcomes: list) -> str:
    """#218: say WHY there is no answer, instead of always blaming permissions.

    Every empty result - nothing composed, an honest #211 decline, a store error, a query that
    matched no rows, AND a genuine LAW-2 denial - produced the same sentence: "I couldn't find
    anything you have access to about that." That is the PERMISSION-DENIAL phrasing, so a user
    whose real problem was "you haven't pressed Compose up yet" went hunting for an access
    problem. (It cost exactly that, in this repo, on the first question of a session.)

    In a product whose entire claim is honest, permission-faithful answers, a decline that
    misattributes its OWN reason is not cosmetic - it is the same class of dishonesty as
    inventing a column, pointed at the user instead of the data.

    LAW 2 still binds: when stores EXIST but none are visible to this caller, we say exactly what
    we always said and nothing more - an invisible store must remain indistinguishable from a
    nonexistent one. The router only reports `no store is composed yet` when the catalog is
    genuinely empty, where there is nothing whose existence could leak.
    """
    if not decision.stores:
        reason = (decision.reason or "").lower()
        if "composed" in reason:                 # catalog genuinely empty - safe to say so
            return NOT_COMPOSED_ANSWER
        return NO_EVIDENCE_ANSWER                # invisible-or-nonexistent: stay generic (LAW 2)
    if outcomes and all(o.status == DECLINED for o in outcomes):
        return DECLINED_ANSWER                   # #211: healthy store, honestly holds no such data
    if outcomes and all(o.status in (ERROR, TIMEOUT) for o in outcomes):
        return FAILED_ANSWER                     # the disclosure names which store and why
    if outcomes and any(o.status in (OK, EMPTY) for o in outcomes):
        # #940: ANY store still reading is enough to withhold the claim. "Your data matched no
        # records" describes a completed search; with one source mid-crawl the search is not
        # complete, and saying otherwise is a statement about content nobody has read yet.
        # Deliberately BELOW the DECLINED branch: #211's decline is about the KIND of data a
        # store holds, which finishing the crawl will not change - turning that into "wait a
        # moment" would be advice to wait for an answer that is never coming.
        if any(getattr(o, "warming", False) for o in outcomes):
            return WARMING_ANSWER
        return EMPTY_RESULT_ANSWER               # it ran, it just matched nothing
    return NO_EVIDENCE_ANSWER


def merge_evidence(per_store: list[list[Evidence]], cap: int = 12) -> list[Evidence]:
    """Round-robin by per-store rank: every store's #1 outranks any store's #2."""
    merged: list[Evidence] = []
    rank = 0
    while len(merged) < cap:
        row = [lst[rank] for lst in per_store if rank < len(lst)]
        if not row:
            break
        merged.extend(row)
        rank += 1
    return merged[:cap]


def citations_from(evidence: list[Evidence]) -> list[dict]:
    """Polymorphic citations, ONE PER EVIDENCE ROW: provenance pass-through + store_id/kind.
    #165: each citation ALSO carries a typed `proof` (contract in provenance.py);
    an unclassifiable citation degrades to the legacy flat shape — a bad citation
    must never kill an answer (contract test catches real connectors).

    #861: THIS LIST IS INDEXED POSITIONALLY BY THE ANSWER'S [n] MARKERS, so it must not be
    deduped. It used to collapse on (store_id, kind, doc, table, row_ids), which meant two
    chunks of ONE document were two footnotes and one citation — and since the live rail
    renders footnotes while a reopened transcript renders these, a marker that resolved live
    pointed at nothing the moment the thread was reopened. Measured on prod before the fix:

        answer   "- Singapore: 137 [1] - London: 92 [3] - Berlin: 78 [5] - Austin: 65 [7]"
        stored citations 6   ->  [7] resolved to nothing on reopen

    #855 had already settled this one layer down, when `_slim_citations` deduped and broke
    the same numbering: "removing row n silently renumbers every later marker... A row that
    says less is honest; a row that has moved is a lie." That rule simply had a third home
    nobody had counted — this function, upstream of everything #855 touched.

    Whether a rail shows ONE entry for two rows of the same document is a RENDER question,
    and #859 already answers it on the live surface by rendering only what the answer points
    at. Collapsing the data to answer a display question is what cost the numbering."""
    out: list[dict] = []
    for ev in evidence:
        prov = ev.provenance or {}
        cite = {"store_id": ev.store_id, "kind": ev.kind}
        cite.update(prov)
        try:
            cite["proof"] = normalize_proof(ev).to_dict()
        except ProvenanceError:
            pass
        out.append(cite)
    return out


def _once(fragments) -> list:
    """Collapse IDENTICAL rendered fragments, preserving first-occurrence order.

    #799: every line below is built by joining over OUTCOMES, and a compound ask produces one
    outcome per store PER SUB-QUESTION. A store that declined both halves of a question was
    therefore named twice, and prod rendered exactly that:

        Asked but holds no data of this kind - not used: bigquery-1, bigquery-1.

    Deduping by `store_id` would be the obvious fix and would be WRONG: `Truncated` names
    "showing 5 of 295 rows" and `Cross-source alignment` carries `o.note`, so two outcomes for
    one store can be two genuinely different facts. Collapsing those would trade a cosmetic
    duplicate for real information loss. Two outcomes that render the SAME fragment are saying
    the same thing twice; two that render different fragments are different facts and both
    belong on screen. Same rule, and the same shape, as the canvas outcome-row label
    (#753/#761) - which is where this class of duplicate was last fixed, on a different surface.
    """
    seen, out = set(), []
    for f in fragments:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def disclosure_from(outcomes: list[StoreOutcome]) -> str:
    """§8: a dropped store is disclosed, never silently backfilled. '' = full coverage."""
    parts: list[str] = []
    dropped = [o for o in outcomes if o.status in (ERROR, TIMEOUT)]
    if dropped:
        # #680: say WHY when the why is something the reader can act on. This printed only
        # `status`, so "you have not linked Amazon" and "the warehouse timed out" arrived as
        # the same word - and the one the user could have fixed in thirty seconds read as a
        # dead end. `remedy` is set by the executor only for drops carrying `.idp`; a plain
        # fault keeps its bare status, because a diagnostic string is not instructions and
        # dressing one up as advice is worse than staying quiet.
        #
        # The instruction gets its OWN sentence rather than going inside the parentheses:
        # a remedy is a full sentence with its own punctuation, and nesting it in a
        # comma-separated list produced something no one could parse once a second store
        # failed alongside it.
        # #727: "not connected" is the UNLINKED-CLOUD phrasing (#680) and now applies only
        # there - a schema fault also carries a remedy, and calling it "not connected" sent
        # the user to re-link a cloud that was already linked.
        named = ", ".join(_once(
            qualify(o.store_id, o.business_unit,
                    "not connected" if o.unlinked else o.status)
            for o in dropped))
        parts.append(f"Partial coverage — unavailable and omitted: {named}.")
        # #799: its own loop, so it duplicates independently of the joined line above -
        # two identical "To use X: ..." sentences in a row on a compound ask.
        parts.extend(_once(f"To use {o.store_id}: {o.remedy.rstrip('.')}."
                           for o in dropped if o.remedy))
    capped = [o for o in outcomes if o.status == BUDGET]
    if capped:                       # E8: cost ceilings are DISCLOSED, never silent
        named = ", ".join(_once(qualify(o.store_id, o.business_unit) for o in capped))
        parts.append(f"Capped by query budget — not dispatched: {named}.")
    # #211: a store that DECLINED is healthy — it just doesn't hold this kind of data, and said
    # so instead of inventing a column. Say it out loud: a silent decline is what let a
    # fabricated answer look like a complete one, and the reader deserves to know which sources
    # were asked and had nothing to offer.
    declined = [o for o in outcomes if o.status == DECLINED]
    if declined:
        named = ", ".join(_once(qualify(o.store_id, o.business_unit) for o in declined))
        parts.append(f"Asked but holds no data of this kind — not used: {named}.")
    # #206: a ROW cap gets the same promise as a cost cap. The answer is written from `count`
    # rows; if the query really produced `total`, the prose is a sample of the truth and will
    # not know it ("here is the total revenue for EACH product SKU" over 5 of 295). Name the
    # real number so the reader can tell an answer from a sample.
    trimmed = [o for o in outcomes if o.truncated]
    if trimmed:
        named = ", ".join(_once(qualify(o.store_id, o.business_unit,
                                        f"showing {o.count} of {o.total} rows")
                                for o in trimmed))
        parts.append(f"Truncated — the answer below is built from a sample, not the full "
                     f"result: {named}.")
    # #219: a cross-source semi-join that could not be aligned (or had to drop unsafe key values)
    # is DISCLOSED, never silent - an answer whose two halves were NOT constrained to the same
    # keys must not read as though they were.
    noted = [o for o in outcomes if getattr(o, "note", "")]
    if noted:
        named = ", ".join(_once(qualify(o.store_id, o.business_unit, o.note) for o in noted))
        parts.append(f"Cross-source alignment - {named}.")
    return " ".join(parts)


def compound_disclosure(decision: RoutingDecision, outcomes: list[StoreOutcome]) -> str:
    """E6 / scenario C: a sub-query no accessible source answered is DISCLOSED in the
    user's own words — never silently dropped, never backfilled from stores the caller
    can't see. Covered = at least one OK dispatch served that sub-question."""
    if not decision.sub_queries:
        return ""
    answered = {o.sub_question for o in outcomes if o.status == "ok"}
    uncovered = [sq.question for sq in decision.sub_queries if sq.question not in answered]
    if not uncovered:
        return ""
    quoted = ", ".join(f"'{q}'" for q in uncovered)
    return f"Not covered: {quoted} — no source you can access returned results."


@dataclass
class RouterResult:
    """QueryResult's federated sibling: one answer over many stores, explainable."""
    answer: str
    citations: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)        # merged, post-trim (UI/audit)
    routing: dict = field(default_factory=dict)               # RoutingDecision.to_dict()
    outcomes: list[dict] = field(default_factory=list)        # StoreOutcome.to_dict()s
    disclosure: str = ""                                      # "" = full coverage

    def to_dict(self) -> dict:
        return {"answer": self.answer, "citations": self.citations,
                "evidence": self.evidence, "routing": self.routing,
                "outcomes": self.outcomes, "disclosure": self.disclosure}


# Every label that heads an INSTRUCTION line folded into the model's context. Adding a new
# one below (#206 [coverage], #227 [query], #449 [style]) without adding it HERE is how #570
# happened: the style directive shipped as a third instruction chunk and nothing stripped it.
_INSTRUCTION_MARKERS = ("coverage", "query", "style")


def strip_instruction_markers(answer: str) -> str:
    """Remove `[coverage]` / `[query]` if the model echoes them into its prose (#272).

    Those labels head INSTRUCTION lines we fold into the context (#206's sample warning, #227's
    "here is what produced this evidence"). #233 already stopped them consuming a citable
    NUMBER, so a footnote can never point at one — but nothing stopped the model reproducing
    the literal token. Caught by an independent audit: the prose carried markers
    [1][2][3][4][5][coverage] against sources [1]-[5], so `[coverage]` sat in the text
    resolving to nothing, on two consecutive runs.

    Same rule and same reason as #257's numeric fix on the /search path: a marker that resolves
    to nothing reads as corroboration the answer has not got. That fix was applied to the
    document path only, and this is the identical bug surviving in the analytical path — worth
    saying plainly, because "fixed it over there" is exactly how a bug hides.

    Only these two known labels are stripped: an unrecognised bracketed word is likely the
    author's own prose ("[sic]", "[see below]") and removing it would edit their meaning."""
    if not answer:
        return answer
    pattern = r"\s?\[(?:%s)\]" % "|".join(_INSTRUCTION_MARKERS)
    return re.sub(pattern, "", answer, flags=re.I)


#: The ANSWER_SYSTEM prompt tells the model to "say plainly that you do not have that
#: information", so live declines are LLM-AUTHORED and drift ("I don't have that
#: information." was measured alongside the canonical sentence). The #493 trigger has to
#: catch the refusal FAMILY, not one string - but only the family: anything longer than a
#: bare refusal is a delivered answer and is never touched.
_REFUSAL = re.compile(
    r"^\W*i\s+(?:do\s+not|don'?t)\s+have\s+(?:that|the|enough)\s+information\b[.!]?\s*$",
    re.I)


def _is_refusal(answer: str) -> bool:
    a = (answer or "").strip()
    return (a in (NO_EVIDENCE_ANSWER, DECLINED_ANSWER, EMPTY_RESULT_ANSWER, WARMING_ANSWER)
            or bool(_REFUSAL.match(a)))


def _strip_question_echo(answer: str, question: str) -> str:
    """B-013/B-007 measured live: the model sometimes prefixes its answer with the
    question verbatim - and sometimes the echo is ALL there is. Strip the echoed prefix
    mechanically; an echo with a real answer behind it becomes that answer, and a pure
    echo becomes empty, which `_is_refusal`-adjacent handling turns into the condensed
    pass. Whitespace-normalized, case-folded prefix match - nothing fuzzier, so a real
    answer that merely mentions the question's words is never touched."""
    a = (answer or "").strip()
    squash = lambda s: " ".join(s.split()).casefold()   # noqa: E731
    q = squash(question or "")
    if not q or not squash(a).startswith(q):
        return a
    # walk the raw answer far enough to consume the question's characters
    consumed, qi = 0, 0
    q_flat = "".join(q.split())
    a_fold = a.casefold()
    while consumed < len(a) and qi < len(q_flat):
        if not a_fold[consumed].isspace():
            qi += 1
        consumed += 1
    return a[consumed:].strip()


def _verbatim_in(extract: str, chunk: str) -> bool:
    """Whitespace-normalized, case-folded containment - the mechanical trust gate. An
    extract the source chunk does not contain is a model invention and is discarded,
    the same verbatim rule as #479's resolver and #474's planner."""
    squash = lambda s: " ".join(s.split()).casefold()   # noqa: E731
    return squash(extract) in squash(chunk)


def _condensed_answer(question: str, merged: list, llm: LlmPort) -> "str | None":
    """#493: ONE second synthesis over verbatim-verified extracts of chunk evidence.

    Findings s15: with the fact IN context, the 8B synthesizer answers from the single
    fact-bearing chunk and drowns at five. Each chunk is asked for verbatim spans
    relevant to the question; spans that fail `_verbatim_in` are discarded; the
    surviving spans alone feed one more `llm.answer`. None whenever anything falls
    short - no extract capability, fewer than two chunks (nothing to condense), no
    verified span, a second answer that is itself a refusal - and the caller keeps the
    original decline. Chunk-kind only: ROW evidence is the SQL rail, byte-identical."""
    chunks = [ev for ev in merged if ev.kind == CHUNK]
    if len(chunks) < 2 or not hasattr(llm, "extract_relevant"):
        return None
    verified: list = []
    for ev in chunks:
        try:
            raw = llm.extract_relevant(question, ev.content) or ""
        except Exception:                      # noqa: BLE001 - a failed extract is no extract
            continue
        for line in raw.splitlines():
            line = line.strip().lstrip("-• ").strip().strip('"').strip()
            if not line or line.upper() == "NONE":
                continue
            if _verbatim_in(line, ev.content):
                verified.append(f"[{ev.store_id} · {ev.business_unit}] {line}")
    if not verified:
        return None
    try:
        second = llm.answer(question, verified)["answer"]
    except Exception:                          # noqa: BLE001 - a failed pass is no answer
        return None
    second = strip_instruction_markers(second or "")
    if not second.strip() or _is_refusal(second):
        return None
    return second


#: A bound IN list beyond this many characters is collapsed in the PROMPT proof only.
#: Findings s19 (D-001, 260805): the rescue's measure SQL carried 296 keys - a
#: 10,789-char [query] line - and llama3.1:8b declined on every variant that contained
#: it (full, capped, beside the fact row); with the list collapsed to a count, the same
#: model answered with the exact gold. Footnotes and provenance keep the full
#: re-runnable SQL - the reader loses nothing.
_IN_LIST_COLLAPSE_CHARS = 160
_IN_LIST = re.compile(r"IN \(([^()]{%d,})\)" % _IN_LIST_COLLAPSE_CHARS, re.I)


def _collapse_in_lists(sql: str) -> str:
    return _IN_LIST.sub(
        lambda m: f"IN (<{m.group(1).count(',') + 1} carried values - the full list is "
                  f"in the shown query>)", sql)


def _semi_join_proof(merged: list) -> "tuple | None":
    """#526: render a key-carry answer's proof as the SEMI-JOIN it actually is, or None.

    Measured (findings s28) on D-002, whose chain completes and whose answer was still a
    decline over `COUNT(*)=213` - the gold - sitting in its own evidence. The cause is the
    shape of the proof, not the evidence. The model was shown two statements:

        SELECT DISTINCT product_id FROM products WHERE product_category_name = 'beleza_saude'
        SELECT COUNT(*) FROM order_items WHERE product_id IN (<124 carried values - the
                                                              full list is in the shown query>)

    Nothing connects them, and the placeholder actively says the list is somewhere the
    model cannot see - so it reports missing information, which is the honest reading of
    what it was given. Rewording the placeholder was tried and rejected: near-identical
    phrasings flip the outcome, which is a sign of prompt overfitting, not of a fix.

    A semi-join is what the two halves COMPUTED, so stating it is not a hint - it is the
    accurate, self-contained proof, and it needs no placeholder at all:

        SELECT COUNT(*) FROM order_items WHERE product_id IN (
            SELECT product_id FROM products WHERE product_category_name = 'beleza_saude')

    The filter half's standalone statement is then DROPPED, because it is now nested
    inside. That part is load-bearing and measured: keeping both fails 3/3 while the
    single combined statement answers 3/3 - the s18/s19 rule again, findings not workings.

    Prompt-only. Evidence, citations and footnotes keep every executed statement verbatim
    with its re-runnable token, so the reader's trail is unchanged."""
    filter_sql = next((str((ev.provenance or {}).get("sql")
                           or (ev.provenance or {}).get("query") or "")
                       for ev in merged if (ev.provenance or {}).get("mechanics")
                       and ((ev.provenance or {}).get("sql")
                            or (ev.provenance or {}).get("query"))), "")
    if not filter_sql:
        return None
    for ev in merged:
        prov = ev.provenance or {}
        bind = prov.get("bind") or {}
        column = bind.get("column")
        sql = str(prov.get("sql") or prov.get("query") or "")
        if not (bind.get("aligned") and column and sql):
            continue
        pattern = re.compile(rf"({re.escape(str(column))}\s+IN\s+)\([^()]*\)", re.I)
        rewritten, hits = pattern.subn(rf"\1({filter_sql})", sql, count=1)
        if hits:
            return rewritten, sql, filter_sql
    return None


#: #500: how many CHUNK-kind evidence items may enter the MODEL's context.
#: Findings s15 measured the mechanism (the 8B answers from the single fact-bearing
#: chunk and drowns at five; order irrelevant, volume decisive) and s21 measured its
#: cost after #499 tripled the pool: B-009 and B-011 decline 3/3 with the fact chunk
#: ranked FIRST. The merge cap (12) was never a context policy - it is an evidence
#: budget. 0 disables the cap (the pre-#500 context, byte-identical).
#:
#: DEFAULT 0 - OFF - and the reason is measured, not cautious (s24). The gate ran the
#: doc pack at cap 3 and a CONTROL at cap 0: both scored 27/33, differing on exactly two
#: items in opposite directions. The cap converts B-007 and breaks A-003. A one-for-one
#: trade is not an improvement, and #500's two actual targets (B-009, B-011) stayed red
#: at BOTH settings with doc-MRR 1.00 - which falsifies the volume hypothesis rather
#: than merely failing to confirm it. Left off by default it also cannot spring #512's
#: multi-store starvation on a fan-out the single-store pack cannot measure.
#:
#: The mechanism is deliberately NOT removed: DBSEARCH_SYNTH_CHUNK_CAP turns it on, and
#: stage 2 (extract-then-combine as the PRIMARY path) needs it to experiment against.
_CHUNK_CAP_DEFAULT = 0


def _resolve_chunk_cap(chunk_cap: "int | None") -> int:
    """Only a non-negative integer is a policy; anything else keeps the measured default.

    Review's mutation battery found the first cut clamped with `max(0, ...)`, which made
    `DBSEARCH_SYNTH_CHUNK_CAP=-1` mean UNLIMITED - it collided with the "0 disables"
    sentinel, so an operator tightening the cap would have removed it instead. A typo
    ("ten", "1e3") likewise has no defensible reading, so it keeps the default rather
    than inventing one: a misconfiguration must never silently change the policy in
    either direction."""
    if chunk_cap is not None:
        ok = isinstance(chunk_cap, int) and not isinstance(chunk_cap, bool) and chunk_cap >= 0
        return chunk_cap if ok else _CHUNK_CAP_DEFAULT
    raw = os.environ.get("DBSEARCH_SYNTH_CHUNK_CAP")
    if raw is None or not raw.strip():
        return _CHUNK_CAP_DEFAULT
    try:
        value = int(raw)
    except ValueError:                     # a typo must not silently change the policy
        return _CHUNK_CAP_DEFAULT
    return value if value >= 0 else _CHUNK_CAP_DEFAULT


#: The kinds the cap counts: PASSAGE evidence, whatever store produced it. Originally
#: this was CHUNK alone, which review caught as half a fix - `GraphSearchStore`, the
#: shipped SharePoint connector, emits RECORD (native_search.py), so on a SharePoint
#: tenant the cap never fired AND it biased the context toward the uncapped store while
#: the disclosure quoted numbers that counted only chunks. ROW is the SQL rail: compact
#: facts, never the volume s15 measured the model drowning in, and excluded on purpose.
_CAPPED_KINDS = (CHUNK, RECORD)


def _cap_chunks(prompt_evs: list, cap: int) -> "tuple[list, int]":
    """Keep the first `cap` passage-kind items in merged order; ROW evidence passes
    through untouched and never consumes a slot (the SQL rail stays byte-identical).
    Returns the capped list and how many passages were offered - the disclosure needs
    both, and a silent cap is exactly what LAW 8 forbids."""
    offered = sum(1 for ev in prompt_evs if ev.kind in _CAPPED_KINDS)
    if not cap or offered <= cap:
        return prompt_evs, offered
    kept, seen = [], 0
    for ev in prompt_evs:
        if ev.kind not in _CAPPED_KINDS:
            kept.append(ev)
            continue
        if seen < cap:
            kept.append(ev)
            seen += 1
    return kept, offered


def _generate(llm: LlmPort, question: str, context: list, on_token) -> dict:
    """One generation, streamed when the caller wants tokens AND the model can produce them.

    A model without `answer_stream` is not an error and must not be a missing answer: the
    capability-gated fallback runs the one-shot call and hands the whole answer to `on_token`
    once, so a caller's event contract holds whatever model is configured (the same
    capability shape `_build_service` uses for the decomposer and the planner)."""
    if on_token is None or not hasattr(llm, "answer_stream"):
        generated = llm.answer(question, context)
        if on_token is not None:
            on_token(generated["answer"])
        return generated
    parts: list[str] = []
    for tok in llm.answer_stream(question, context):
        parts.append(tok)
        on_token(tok)
    return {"answer": "".join(parts)}


def synthesize(question: str, report: DispatchReport, decision: RoutingDecision,
               llm: LlmPort, *, cap: int = 12, chunk_cap: "int | None" = None,
               on_token=None) -> RouterResult:
    """`on_token` (#689, ADR 0025): a callback fed each token of the FIRST generation as it
    arrives, so the conversational ask surface can stream the way `/chat/stream` always has.

    ONLY the first generation streams, and that is a correctness rule rather than a
    simplification. Everything after it - the marker strip, the question-echo strip, the #493
    condensed pass, and the #474 rescue one layer up - can REPLACE the answer entirely, so
    what was streamed is a draft and `RouterResult.answer` is the record. A caller must render
    the final answer, not the tokens it accumulated; streaming the rewrites too would put two
    answers on screen for one question. `/search` has had exactly this contract since #257."""
    # Merge in decision order so rank interleave is deterministic and explainable.
    per_store = [report.evidence_by_store[r.store_id] for r in decision.stores
                 if r.store_id in report.evidence_by_store]
    merged = merge_evidence(per_store, cap=cap)
    condensed_note = ""
    cap_note = ""
    if merged:
        # Findings s19: rows marked provenance.mechanics are WORKINGS the machine already
        # consumed (the rescue's carry keys - the bind used them and the measure SQL shown
        # as proof names every one). Measured live: even two such rows beside the fact row
        # make the 8B synthesizer decline; excluded, it answers. They stay in `merged` -
        # so the reader's evidence, citations and footnotes keep the full trail - and out
        # of the prompt.
        prompt_evs = [ev for ev in merged if not (ev.provenance or {}).get("mechanics")]
        # #500: the same rule one layer out - the merge cap is an EVIDENCE budget, not a
        # context policy, and 12 chunks is four times what the 8B tolerates (s15). Only
        # the top-ranked chunks reach the model; `merged` stays whole, so evidence,
        # citations and footnotes keep every passage and the reader loses nothing.
        chunk_limit = _resolve_chunk_cap(chunk_cap)
        before = prompt_evs
        prompt_evs, offered = _cap_chunks(prompt_evs, chunk_limit)
        if chunk_limit and offered > chunk_limit:
            cap_note = (f"Retrieved {offered} passages; the answer was written from the "
                        f"{chunk_limit} most relevant - all {offered} remain cited in "
                        f"the evidence.")
            # A store the cap SILENCED must be named. `merge_evidence` is round-robin by
            # per-store rank, so with 4+ document stores the cap is reached before the
            # 4th store's best passage is ever offered - and that store still reports OK
            # with a row count and still shows its citations on screen, so the reader has
            # every reason to believe it was read. Naming it keeps "the 3 most relevant"
            # from quietly meaning "the first 3 stores" (both reviewers, independently).
            # This DISCLOSES the squeeze-out; whether to change the mechanism (per-store
            # cap vs global) is a measurement question, not a wording one - see #512.
            heard = {ev.store_id for ev in prompt_evs}
            silenced = [sid for sid in dict.fromkeys(ev.store_id for ev in before)
                        if sid not in heard]
            if silenced:
                cap_note += (" Nothing from " + ", ".join(silenced)
                             + " reached the answer, though its passages are cited below.")
        context = [f"[{ev.store_id} · {ev.business_unit}] {ev.content}" for ev in prompt_evs]
        # #206: if the evidence is a SAMPLE, the model must be told, or it will answer as
        # though it were the whole result — "here is the total revenue for EACH product SKU"
        # written over 5 of 295 rows. The disclosure line alone doesn't fix that: the prose
        # itself makes the false claim, and the prose is what people read.
        trimmed = [o for o in report.outcomes if o.truncated]
        if trimmed:
            context = [
                "[coverage] The rows below are a PARTIAL SAMPLE, not the full result: "
                + "; ".join(f"{o.store_id} returned {o.total} rows and only {o.count} are "
                            f"shown" for o in trimmed)
                + ". Answer only about the rows you were given, and say plainly that this is "
                  "a sample of a larger result — never imply it is complete or exhaustive.",
            ] + context
        # #227/#231: tell the model WHAT PRODUCED the evidence, not just the evidence. The
        # context was `ev.content` alone, so a number arrived stripped of its meaning:
        #   - `Touring Bikes=220655` from `SELECT TOP 1 ... ORDER BY revenue DESC` -> the model
        #     said "I don't have enough information to say which category is highest; I only see
        #     one" - to a query that had ALREADY ranked them and returned the winner.
        #   - `count=2` from `... WHERE rating = 5 AND STRINGEQUALS(category,'touring bikes')` ->
        #     "I don't have that information", because nothing told it what the 2 counted.
        # Both are the same bug: the database did the work and the answer was thrown away. The
        # query is our own generated, guard-validated statement and is already shown to the user
        # as proof, so passing it costs nothing.
        proofs: list[str] = []
        for ev in merged:
            prov = ev.provenance or {}
            q = prov.get("sql") or prov.get("query")
            if q and str(q) not in proofs:
                proofs.append(str(q))
        # #526: a key-carry answer's two statements become the one semi-join they
        # computed - complete, self-contained, and with the carried list nested rather
        # than elided. Falls through to the s19 collapse whenever the pair is not present.
        semi = _semi_join_proof(merged)
        if semi:
            rewritten, measure_sql, filter_sql = semi
            proofs = [rewritten if p == measure_sql else p
                      for p in proofs if p != filter_sql]
        # s19: collapse AFTER dedup so equality keys on the real SQL; prompt-only - the
        # evidence/footnote provenance keeps every carried value re-runnable.
        proofs = [_collapse_in_lists(p) for p in proofs]
        if proofs:
            context = context + [
                "[query] The evidence above is the RESULT of running this against the database: "
                + " | ".join(proofs)
                + ". The database ALREADY applied every filter, join, grouping, ordering and "
                  "aggregate in that query, over the full dataset. So: an aggregate "
                  "(COUNT/SUM/AVG) is computed across EVERY matching record, not just the rows "
                  "shown; and rows returned after ORDER BY with TOP/LIMIT ARE the top ones - the "
                  "single row of a TOP 1 ... ORDER BY x DESC IS the maximum. Answer the question "
                  "directly from this result. Do NOT claim you lack information that the query "
                  "already answered, and do not re-derive the ranking or the arithmetic yourself.",
            ]
        # #449: say the answer, don't show the working. A fan-out across two stores legitimately
        # has to combine numbers the database could not combine for us - but the prose came back
        # as a calculation transcript ("For apac: 205000 + 800 = 205800 For emea: ..."), which is
        # a worksheet, not an answer. The arithmetic is not lost by omitting it: every input row
        # is already on screen as a citation with a re-runnable proof, so the derivation stays
        # checkable while the prose stays readable.
        context = context + [
            "[style] Answer in plain prose, as briefly as the question allows. State the result. "
            "Do NOT show intermediate arithmetic, per-item working, or a step-by-step derivation "
            "- if you combined figures across sources, give the combined figure only. Do not "
            "restate the raw evidence as a list of field=value pairs; the reader can already see "
            "the source rows next to your answer.",
        ]
        generated = _generate(llm, question, context, on_token)   # post-trim only (gate #3)
        answer = strip_instruction_markers(generated["answer"])
        answer = _strip_question_echo(answer, question)
        # #493: the model answers correctly from the single fact-bearing chunk and
        # drowns at five (findings s15 - order irrelevant, volume decisive). A DECLINED
        # answer over chunk evidence - including a pure question-echo, which is a refusal
        # wearing a costume - gets ONE condensed second pass over verbatim-verified
        # extracts; a delivered answer - even a wrong one - is never touched.
        if not answer or _is_refusal(answer):
            condensed = _condensed_answer(question, merged, llm)
            if condensed is not None:
                answer = condensed
                # The condensed pass reads the FULL merged list, so the cap sentence is
                # not true of the answer that actually shipped - it would tell the reader
                # the answer came from 3 passages when it came from extracts of all of
                # them. Two disclosures that contradict each other are worse than one
                # missing: drop the cap note and let the condensed note stand alone.
                cap_note = ""
                condensed_note = ("The first read of the retrieved passages came back "
                                  "empty; this answer is from a second, condensed pass "
                                  "over verbatim extracts of the same passages.")
            elif not answer:
                answer = NO_EVIDENCE_ANSWER    # a pure echo, unrescued: an honest decline
    else:
        # never call the LLM without evidence - but SAY WHY there is none (#218)
        answer = no_evidence_answer(decision, report.outcomes)
    disclosure = " ".join(filter(None, [disclosure_from(report.outcomes),
                                        compound_disclosure(decision, report.outcomes),
                                        cap_note, condensed_note]))
    return RouterResult(
        answer=answer,
        citations=citations_from(merged),
        evidence=[ev.to_dict() for ev in merged],
        routing=decision.to_dict(),
        outcomes=[o.to_dict() for o in report.outcomes],
        disclosure=disclosure,
    )
