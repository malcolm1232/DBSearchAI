"""Conversational layer over the stateless QueryService (Phase 2.5).

ConversationService adds two things and NOTHING else: (a) per-(conversation, user)
history read through a durable store, and (b) a condense step that rewrites a follow-up
into a standalone question BEFORE retrieval. It then delegates to QueryService.answer -
the unchanged, permission-trimmed core (LAW 2). There is exactly one retrieval path, so
the trim can never be bypassed by the conversational route.

History is keyed by (conv_id, user_oid): a guessed conv_id under another identity simply
misses and starts fresh, so histories never bleed across users.

#596: conversations survive a restart. The store is the read path here - unlike
grant_store.py, which keeps memory as the only read surface because principal expansion
is authorization and runs on every request, conversation history is one SELECT per
question against a latency budget already dominated by retrieval and LLM generation.
Reading through the store means a restarted process simply continues the thread, with no
hydration step and no in-process cache to go stale. See
dbsearch.server.conversation_store for the store implementations and the two failure
stances (`history()` unguarded, the ask path guarded) documented on the methods below.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dbsearch.query.service import QueryResult, QueryService
from dbsearch.ports.base import LlmPort


@dataclass
class Turn:
    question: str        # what the user typed
    standalone: str      # the (possibly rewritten) question actually retrieved on
    answer: str          # synthesized answer (post-trim content)
    cited_docs: list = field(default_factory=list)
    # #600: the turn's QueryResult.retrieved_docs. Stored because sharing a conversation
    # is DEFINED over it - the share grants exactly the documents this thread cited.
    citations: list = field(default_factory=list)
    # #633: [{doc, quote, quote_kind, locator}] - the passages the Sources panel quotes when
    # this turn is REOPENED. Deliberately a second field rather than an enrichment of
    # cited_docs: that list is what the share machinery is defined over (`_owned_thread_
    # citations`, `_readable_prefix`, ADR 0020's per-turn re-check), and a shape change there
    # is a change to who can read what. This one is presentation, and nothing authorises off
    # it. Recomputing quotes at read time was the alternative and is worse: retrieval is not
    # stable, so a reopened thread would show a reader a passage the answer above it was
    # never built from.


#: What a stored citation keeps (#633). NOT `title` and NOT `uri`: both are resolved at read
#: time through the READER's own scope, and a stored copy would go stale the moment a document
#: is renamed, moved or deleted - showing a reader a title the index no longer agrees with.
#: `doc` is the join key, and the transcript re-resolves the rest (`_citation_rows`).
_CITATION_KEYS = ("doc", "quote", "quote_kind", "locator")

#: What a stored PROOF citation keeps (#689, ADR 0025's "transcripts must carry router
#: provenance"). A routed turn's evidence is a query against a store, not a document in an
#: index, so it joins on nothing the reader's scope can re-resolve.
#:
#: THIS ONE STORES ITS OWN DISPLAY TEXT, and that is the opposite of the rule above rather
#: than an oversight. A document title is re-resolved because the index is still there to
#: ask and a stale copy would contradict it. A store can be recomposed under new credentials,
#: renamed, or deleted outright, and when it is, there is nothing left to ask - so the proof
#: has to be the record of what the answer was built from, the same reasoning that made #633
#: store quotes rather than recompute them.
#:
#: `rerun_token` is NEVER here. It is signed per (store, sql, USER) and a stored one would
#: either be a token minted for somebody else or a token outliving the identity it was bound
#: to. The transcript re-signs for its reader at read time (#689 slice 2b).
_PROOF_KEYS = ("kind", "store_id", "sql", "origin", "snippet")


def _slim_citations(cites) -> list:
    """Trim a citation list down to what is worth persisting.

    TWO SHAPES, one list: document rows keyed on `doc` (#633) and router proof rows keyed on
    `store_id` (#689). They travel together because they are what ONE answer was built from,
    and splitting them into two columns would let a reopened turn show half its evidence.

    Absent keys are dropped rather than stored as None, so a row that never had a quote does
    not round-trip as one that has an empty one.

    LENGTH AND ORDER ARE THE CONTRACT. This trims what is INSIDE each row and never how many
    there are, because the answer's `[n]` markers index into this list POSITIONALLY - the
    rule `_citation_rows` states for the document plane and #855 proved this function was
    breaking on the proof plane: "removing row n silently renumbers every later marker... A
    row that says less is honest; a row that has moved is a lie."

    So no dedupe, and a row this cannot classify HOLDS ITS SLOT as an empty one rather than
    disappearing. An earlier version deduped by the whole slimmed row, to stop a transcript
    rendering three identical Verify data buttons for one SELECT that returned three result
    rows. Two things were wrong with that. The rows were only identical because
    `pair_proof_snippets` was joining every result row onto every citation (fixed with it, in
    the same commit) - and even for rows that ARE identical, whether the rail shows one entry
    or three is a RENDER question, answered on the live surface, which shows three. A
    persisted list that shows fewer than the live one is not tidier; it is a different
    answer, numbered differently, under the same words."""
    out = []

    def _keep(row: dict) -> None:
        out.append(row)

    for c in cites or []:
        if c.get("doc"):
            _keep({k: c[k] for k in _CITATION_KEYS if c.get(k) is not None})
            continue
        if not c.get("store_id"):
            _keep({})            # neither shape: hold the slot, say nothing
            continue
        row = {k: c[k] for k in _PROOF_KEYS if c.get(k) is not None}
        proof = c.get("proof") or {}
        # The router puts `sql` under `proof`, the flattened footnote shape puts it at the
        # top level. Read both, because a proof row that lost its query is a claim with
        # nothing behind it, and which shape arrives depends on which producer built it.
        if not row.get("sql") and proof.get("sql"):
            row["sql"] = proof["sql"]
        # `kind` IS THE PROOF VOCABULARY HERE - sql | document | record - and never the
        # Evidence one (chunk | row | record), which is what an un-normalized citation
        # carries. The two overlap on `record` and disagree everywhere else, so a stored
        # row whose kind meant "row" would be read by every renderer as "not a SQL proof"
        # and quietly lose its Verify data action. One field, one vocabulary, chosen where
        # the two shapes are still distinguishable.
        if proof.get("kind"):
            row["kind"] = proof["kind"]
        elif row.get("kind") in ("chunk", "row"):
            row.pop("kind")          # unclassifiable: say nothing rather than the wrong thing
        _keep(row)
    return out


class ConversationService:
    def __init__(self, query_service: QueryService, llm: LlmPort,
                 max_history_turns: int = 8, store=None) -> None:
        from dbsearch.server.conversation_store import InMemoryConversationStore
        self._qs = query_service
        self._llm = llm
        self._max = max_history_turns
        self._store = store if store is not None else InMemoryConversationStore()

    def history(self, user_oid: str, conv_id: str) -> "list[Turn]":
        """The stored thread, oldest first. Deliberately NOT guarded: a caller deciding
        what a conversation contains (the #600 share operation, the transcript view) must
        see a store outage as an error, never as an empty conversation - minting a share
        from a silently-empty history would grant nothing and report success."""
        return self._store.history(conv_id, user_oid)

    def _history_or_empty(self, conv_id: str, user_oid: str) -> "list[Turn]":
        """The ask path's read. A store outage here degrades to answering WITHOUT
        conversational context (condense skipped, logged) rather than refusing the
        question - the answer itself never depended on the store."""
        try:
            return self._store.history(conv_id, user_oid)
        except Exception:
            import logging
            logging.getLogger("dbsearch").error(
                "conversation store unreadable - answering without history")
            return []

    def ask(self, user_oid: str, conv_id: str, question: str,
            llm: "LlmPort | None" = None, tenant_id: "str | None" = None,
            retrieval_oid: "str | None" = None) -> QueryResult:
        """`retrieval_oid` splits WHOSE THREAD THIS IS from WHOSE PERMISSIONS ANSWER IT, and
        defaults to not splitting them at all (#605, ADR 0021).

        Every signed-in caller passes nothing and gets the pre-#605 behaviour exactly: one oid
        keys the history and expands the principals, because for a person those are the same
        fact. An anonymous link visitor is the one caller for whom they are not. Their turns
        must land on a PRIVATE fork key, `link:<share_id>:<visitor_id>`, so that no stranger
        can ever append to the owner's thread and no visitor can read another's; their
        RETRIEVAL must expand the share's sentinel principal, `link:<share_id>`, because that
        is the grantee the share's conv-scoped grants actually name. Keying retrieval on the
        fork instead would expand nothing (a principal no grant names - every visitor
        unauthorized), and keying the store on the sentinel instead would pour every visitor's
        questions into one shared thread they all then read.

        NOTHING A CLIENT SENDS REACHES THIS. `link_access.py` derives both values from a share
        record it resolved by token hash plus a server-minted cookie; there is no request field
        anywhere that names either one. The parameter can only ever be as narrow as the caller
        makes it, and the default is the narrow one - omitting it can never widen a read."""
        gen = llm or self._llm        # #43: optional per-request generation model; trim unaffected
        history = self._history_or_empty(conv_id, user_oid)
        if history:
            window = history[-self._max:]
            hist_dicts = [{"question": t.question, "answer": t.answer} for t in window]
            standalone = gen.condense_question(question, hist_dicts)
        else:
            standalone = question
        result = self._qs.answer(retrieval_oid or user_oid, standalone, llm=gen,
                                 tenant_id=tenant_id)  # UNCHANGED trim core (LAW 2 + ADR 0012)
        turn = Turn(question=question, standalone=standalone, answer=result.answer,
                    cited_docs=list(result.retrieved_docs),
                    citations=_slim_citations(result.citations))
        try:
            self._store.append(conv_id, user_oid, turn)
        except Exception:
            import logging
            logging.getLogger("dbsearch").error(
                "conversation store unreachable - this turn was not recorded")
        return result

    def ask_stream(self, user_oid: str, conv_id: str, question: str,
                   llm: "LlmPort | None" = None, tenant_id: "str | None" = None,
                   retrieval_oid: "str | None" = None,
                   answer_producer=None):
        """Streaming twin of ask() (#50): condense (if history) then stream the answer; record
        the turn once the stream completes. Same trim core (LAW 2) and history keying - and the
        same `retrieval_oid` split, for the same reason and with the same default; see `ask`.
        Both routes of the anonymous doorway exist, so a rule held by only one of them is a
        rule a client picks its way around by choosing the other endpoint.

        `answer_producer` (#689, ADR 0025) replaces WHERE THE ANSWER COMES FROM and nothing
        else. Given one, this method still reads the history, still condenses the follow-up
        into a standalone question, still records the turn from the `done` event, and still
        degrades the same way when the store is unreachable - it simply asks the producer for
        the answer instead of the document plane. That is the whole of the ADR's server-side
        change: Ask is conversational, the router is per-workspace, and the boundary crossing
        is one callable rather than a second conversational path with its own copy of turn
        recording and sharing.

        It takes the STANDALONE question, not the raw one. A router that received "and by
        region?" would route on a fragment whose subject lives in the previous turn, so
        follow-ups would land on whatever store the stray words resembled. Condense is the
        conversational surface's contribution to the routed answer and the producer must not
        have to reimplement it.

        The producer is a SERVER-BOUND callable (`router_api.ask_delegate`), never anything a
        client names: `retrieval_oid`'s docstring on `ask` explains why nothing a client sends
        may choose whose permissions answer a question, and the same rule applies to which
        catalog does."""
        gen = llm or self._llm
        history = self._history_or_empty(conv_id, user_oid)
        if history:
            window = history[-self._max:]
            hist_dicts = [{"question": t.question, "answer": t.answer} for t in window]
            standalone = gen.condense_question(question, hist_dicts)
        else:
            standalone = question
        final = None
        source = (answer_producer(standalone) if answer_producer is not None else
                  self._qs.answer_stream(retrieval_oid or user_oid, standalone, llm=gen,
                                         tenant_id=tenant_id))
        for ev in source:
            if ev["type"] == "done":
                final = ev
            yield ev
        turn = Turn(question=question, standalone=standalone,
                    answer=(final or {}).get("answer", ""),
                    cited_docs=list((final or {}).get("retrieved_docs", [])),
                    citations=_slim_citations((final or {}).get("citations", [])))
        try:
            self._store.append(conv_id, user_oid, turn)
        except Exception:
            import logging
            logging.getLogger("dbsearch").error(
                "conversation store unreachable - this turn was not recorded")

    def drop_user(self, user_oid: str) -> int:
        """#576: the retention sweep's conversation-history cleanup for a swept account.
        Delegates to the store: with a PgConversationStore (PGVECTOR_DSN set) the delete
        reaches Postgres, so a swept account's history stays gone across a restart too;
        with the default InMemoryConversationStore (unconfigured) this still only clears
        this process's memory, same as before #596. Returns the number of conversations
        dropped."""
        return self._store.drop_user(user_oid)
