"""Query service: question -> permission-trimmed, cited answer.

The order here is the whole ballgame (LAW 2, PERMISSIONS.md):

    1. expand the user's transitive group membership (IdentityPort)
    2. embed the question
    3. retrieve WITH a mandatory principals filter (IndexPort.search trims server-side)
    4. fetch text for the surviving hits and hand ONLY those to the LLM
    4c. drop FILLER hits below the relevance floor (#53) so off-topic docs aren't cited —
        subtractive only (removes post-trim chunks), so LAW 2 is unaffected
    5. assemble citations from the surviving (trimmed) hits — never from anything else

`retrieve()` is the trimmed core; `answer()` adds the LLM + citations on top. Anything
that wants the retrieved context (a LangChain retriever, an API layer) MUST go through
`retrieve()` so the trim can never be bypassed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from dbsearch.ports.base import (EmbeddingPort, IdentityPort, IndexPort, LlmPort,
                                 ObjectStorePort, as_read_scope, expand_principals)


@dataclass
class RetrievedChunk:
    doc_external_id: str
    title: str
    uri: str
    text: str            # post-trim context — safe to hand to a model
    score: float
    locator: dict = field(default_factory=dict)
    owner_oid: "str | None" = None   # ADR 0012 attribution, carried through for #576


#: The model's own citation convention, with its line range. `answerNodes` in the browser
#: renders the same spelling as a [n] footnote; here it is the only record of WHICH lines of
#: the context block the model was pointing at.
_MARK_RANGE = re.compile(r"【\s*(\d+)\s*†\s*L(\d+)(?:\s*-\s*L?(\d+))?\s*】")

#: EVERY marker spelling a reader can see, in either convention: the plain [n] footnote and
#: the model's 【n】 / 【n†L4-L6】 form (the locator is optional - #555 caught the bare one
#: live). `_MARK_RANGE` above is deliberately narrower: it needs the line range, because it
#: exists to slice a quote. This one only asks WHICH source was pointed at, so it must match
#: the shapes that carry no range - a marker the reader can resolve is a reference whether or
#: not it names lines.
#: The bracket form accepts a GROUP - "[1, 2]", "[1,2]", "[1-2]" - not just a lone digit run.
#: #724 review: the first cut matched `\[(\d+)\]` only, and the prompt says merely "Cite
#: passages by their [n] markers" without forbidding grouping. A model that grouped therefore
#: produced `referenced == []` while `_drop_dangling_markers` (whose regex had the same shape)
#: left the markers ON SCREEN - so the reader saw a cited answer and the canvas, trusting the
#: empty set, REPLACED it with "none of them answered this". That is worse than the bug #724
#: set out to fix: the old code at least showed the answer.
#:
#: Only digits and separators are allowed inside the brackets, so "[see notes]" is not a
#: citation. Every integer that literally appears is taken; a range's interior is deliberately
#: NOT expanded ("[1-3]" yields 1 and 3, never 2), because inventing a reference to a source
#: the model did not name is the exact failure `_drop_dangling_markers` exists to prevent.
_MARK_ANY = re.compile(r"\[(\d+(?:\s*[,;\-–]\s*\d+)*)\]|【\s*(\d+)\s*(?:†[^】]*)?】")
_INTS = re.compile(r"\d+")

#: #893: the citation shapes a model emits NATIVELY that this product cannot render.
#:
#: Everything above is a whitelist of OUR format, and a whitelist silently passes anything
#: that is not it. gpt-oss cites the OpenAI way - 【<source>†<lines>】, or a private-use
#: sentinel run - and exactly ONE of those shapes is understood downstream: the NUMERIC
#: 【n†Lx-Ly】, which `answerNodes` turns into the same [n] control as the plain form. Every
#: other shape reaches the reader as literal text naming a filename or a chunk index, which
#: is unresolvable (no [n] to click) AND internal vocabulary on a user-facing page, which
#: DESIGN_SYSTEM.md s8 forbids outright.
#:
#: The negative lookahead is load-bearing: it is what keeps this from eating the recognised
#: numeric marker that the rule above has just decided to KEEP. Measured shapes:
#:   【9†L1-L4】               kept   - renders as [9]
#:   【handbook.pdf†L1-L4】    dropped - a filename the reader cannot click
#:   【employment_terms】      dropped - a chunk name
#:   citeturn0file0  dropped - the sentinel run
#:
#: NOT a raw string for the sentinel halves: those codepoints are invisible in an editor
#: and a literal paste of them is the kind of source nobody can review, so they are spelled
#: as escapes - which a raw string would compile as six literal characters instead.
_FOREIGN_MARK = re.compile(
    r"\s?\u3010(?!\s*\d+\s*(?:\u2020[^\u3011]*)?\u3011)[^\u3011]*\u3011"        # a \u3010...\u3011 that is NOT our numeric shape
    "|\\s?\ue200[^\ue201]*\ue201"                       # the OpenAI private-use citation run
    "|[\ue200-\ue206]"                                  # a stray sentinel from a truncated run
)

_QUOTE_CAP = 400


def _cap_quote(text: str, cap: int = _QUOTE_CAP) -> str:
    """One line, capped, cut at a sentence boundary when there is one in the back half.

    A quote exists to be READ next to the claim it supports, so a passage that runs past a
    screenful stops doing that job. Cutting mid-sentence is worse than cutting short: a
    truncated sentence can reverse the meaning of the clause it came from ("employees are
    not entitled to..."), and a reader checking a claim against it would be checking against
    something the document does not say. So the cut prefers a full stop, and falls back to an
    ellipsis that is visibly a cut rather than a silent one."""
    text = " ".join((text or "").split())
    if len(text) <= cap:
        return text
    cut = text[:cap]
    dot = cut.rfind(". ")
    return cut[:dot + 1] if dot > cap // 2 else cut.rstrip() + "…"


def _distinct_docs(hits) -> list[str]:
    """The DISTINCT documents these hits came from, best hit first.

    Measured on the live site: a single uploaded PDF matched five chunks, and the answer
    footer read "Answered from 5 of the 2 documents you can access" - a count of chunks
    rendered into a sentence about documents, and a number that cannot be true, since a
    reader can never draw on more documents than they are entitled to. `retrieved_owners`
    one line below was already de-duplicated; this was the same idea missing the same set.

    Order is retrieval order rather than sorted, because rank is meaning here: the first
    entry is the document that best answered the question."""
    seen, out = set(), []
    for h in hits:
        if h.doc_external_id not in seen:
            seen.add(h.doc_external_id)
            out.append(h.doc_external_id)
    return out


@dataclass
class QueryResult:
    answer: str
    citations: list[dict]       # [{doc, title, uri}] - from trimmed hits only
    retrieved_docs: list[str]   # DISTINCT doc ids this question retrieved (post-trim top-k)
    retrieved_owners: "list[str]" = field(default_factory=list)
    referenced: "list[int]" = field(default_factory=list)
    # #724: the 1-based citation indices the ANSWER POINTS AT - the referenced set, where
    # `citations` above is the SHOWN set ("from trimmed hits only"). They are different
    # questions and the canvas was reading the first as the second: a question the documents
    # could not answer still returned every retrieved document as a numbered Sources list
    # under the words "I do not have that information", so an unrelated HR policy and a
    # directorship PDF were displayed as the evidence for a revenue answer. A caller can now
    # ask "did the documents CONTRIBUTE?" (`referenced` non-empty) instead of "was anything
    # retrieved?" (`citations` non-empty), which is the question every one of them meant.
    #
    # Structural on purpose: derived from the markers in the final answer, never from
    # matching the decline's WORDING. #233 is the standing lesson there - phrasing moves
    # with the model and every matcher built on it has broken.
    # #576 (retention sweep, review Finding 2): the DISTINCT owner oids of the documents
    # actually returned - "actually returned" meaning after rerank/relevance-floor, the
    # same post-trim top-k `retrieved_docs` reflects, not the wider pre-rerank candidate
    # pool. This is attribution metadata riding along for free (`owner_oid` is already on
    # every Chunk, ADR 0012) - never a second authorization signal, never content, and
    # costs no extra round trip: the caller (server/app.py) touches each oid's activity
    # clock so "a colleague reading a document shared with them" counts as access for the
    # document's OWNER, not only for an explicit Grant holder.

    # #393: this field was called `authorized_docs`, and the name is what caused the bug.
    # It holds the post-trim top-k for ONE question, but every surface read it as "documents
    # you can access" and printed that as an entitlement claim - a number that moved when the
    # question moved, and that read "0 documents you can access" (i.e. a permissions refusal)
    # when the truth was an empty index. Retrieval and entitlement are different questions:
    # this is retrieval, and `Edition.corpus_status()` is entitlement. The rename is the fix
    # that a comment could not be, because the next reader never sees the comment.
    #
    # No `authorized_docs` alias is kept HERE on purpose. External clients speak HTTP, where
    # the old key is still emitted for compatibility; in-process callers are all ours, so an
    # alias would only preserve the misreading it was renamed to prevent. Anything still
    # reaching for the old attribute should fail loudly rather than quietly get retrieval.


#: How many keyword candidates may JOIN the vector pool (#498). Measured live: an
#: uncapped union (candidate_k=20 keyword hits) FLOODED the rerank and pushed
#: semantically-correct documents to MRR 0.0 on four previously-passing items. The
#: keyword source exists to sneak a NEEDLE into the pool, not to replace the pool -
#: three seats is a needle plus margin, few enough that semantic winners keep theirs.
_LEXICAL_ADDITIONS = 3

#: Words that carry no lexical signal. Deliberately tiny: over-stripping would delete a
#: real search term ("will", "may" are risky enough to keep out of the list).
_STOPWORDS = frozenset(
    "the a an and or of to in for is are was were be been what which who whose how "
    "many much does do did on at by with from as it its this that these those".split())


def _question_terms(question: str) -> list:
    """The question's greppable terms (#498): casefolded alphanumeric-ish tokens, 3+
    chars, stopwords out, order-stable dedupe. Mechanical on purpose - this feeds a
    keyword search whose whole value is being independent of any model."""
    import re
    tokens = re.findall(r"[A-Za-z0-9][\w.\-]{2,}", question or "")
    seen: set = set()
    out: list = []
    for t in (t.casefold().strip(".-") for t in tokens):
        if len(t) >= 3 and t not in _STOPWORDS and t not in seen:
            seen.add(t)
            out.append(t)
    return out


class QueryService:
    def __init__(
        self,
        index: IndexPort,
        identity: IdentityPort,
        embedder: EmbeddingPort,
        llm: LlmPort,
        store: ObjectStorePort,
        top_k: int = 5,
        rerank: bool = True,
        rerank_factor: int = 4,
        relevance_floor: bool = True,
        floor_rel_lexical: float = 0.5,
        floor_vector_rescue: float = 0.0,
        *,
        tenant_id: str,
    ) -> None:
        # ADR 0012: a QueryService cannot exist un-partitioned. `tenant_id` is the
        # deployment/store constant; per-request callers (the hosted multi-tenant path)
        # override it per call with the session's verified tid.
        self._tenant_id = tenant_id
        self._index = index
        self._identity = identity
        self._embedder = embedder
        self._llm = llm
        self._store = store
        self._top_k = top_k
        self._rerank = rerank              # #44 hybrid vector+lexical reranking
        self._rerank_factor = max(1, rerank_factor)
        self._relevance_floor = relevance_floor    # #53 drop filler so it can't be cited; False disables
        self._floor_rel_lexical = floor_rel_lexical    # #54 keep if shares >= this fraction of best_shared
        self._floor_vector_rescue = floor_vector_rescue  # #54 vector near-tie rescue; 0=off (noisy embedders)

    def content_titles(self, limit: int = 40, tenant_id: "str | None" = None) -> list[str]:
        """#306: distinct ingested doc titles for a document store's routing profile (a content
        signal so the router ranks the store on WHAT IT HOLDS). Best-effort: an index without the
        capability returns []. Capped so a huge library doesn't bloat the profile vector.

        #439: takes the same per-call `tenant_id` override as retrieve/has_visible_content. A
        shared QueryService is constructed with the DEPLOYMENT constant, so without this a
        foreign owner's store profiled the HOME tenant's titles - both wrong for routing and a
        cross-tenant metadata read (ADR 0012).

        #849: NORMALIZED through `as_read_scope`, exactly as its two siblings are, then handed
        the bare partition `distinct_titles` actually compares against. It used to forward
        `tenant_id` raw, so a caller passing a `ReadScope` - which is what every server-layer
        read path passes - hit `chunk.tenant_id != <ReadScope>`, matched nothing, and got an
        EMPTY title list back. Not an error, not a log line: a document store simply lost its
        entire content routing signal and scored ~0 against every question, which reads from
        outside as the router declining a question its own index can answer. Latent until
        #689's ask overlay became the first caller to pass a scope here.

        The DOORWAY is deliberately not consulted. A profile is a routing signal derived from
        what this partition holds; folding in the titles of individually-shared documents from
        other partitions would put another tenant's document names into a store profile, which
        is the cross-tenant metadata read the #439 note above exists to prevent."""
        fn = getattr(self._index, "distinct_titles", None)
        scope = as_read_scope(tenant_id, self._tenant_id)
        return fn(scope.partition)[:limit] if callable(fn) else []

    def document_inventory(self, user_oid: str,
                           tenant_id: "str | None" = None) -> list[dict]:
        """#939: WHICH documents this caller can see in this store, one row per DOCUMENT.

        The launch gate (#895) asks a connected node to show "synced + doc count", and the
        owner asked to see the filenames - "did DBSNotes.txt land?". Neither was answerable
        anywhere in the product, and neither needed new storage: every index adapter already
        implements `list_doc_acls`, and both the local and pgvector ones return the title and
        uri alongside the acl. Nothing was missing; nothing was reading it.

        THE TRIM IS THE POINT, AND IT IS NOT OPTIONAL. `list_doc_acls` is the ADMIN
        permission-tester surface: it returns every document in the partition, deliberately
        untrimmed, because its job is to answer "who can see what". Forwarding that to a
        user-facing list would publish the NAMES of documents the caller cannot read - a
        disclosure with no query attached to it, and one no retrieval test would catch because
        retrieval never goes near this call. So the caller's own expanded principals are
        intersected with each row here, through `expand_principals`, the same chokepoint
        `retrieve` and `corpus_status` use (LAW 2), under the same ADR 0012 partition.

        Titles and uris only - never content, never a chunk (LAW 1). Best-effort: an index
        without the capability returns [], which reads downstream as "we cannot say", not as
        "this store is empty" (#392).
        """
        scope = as_read_scope(tenant_id, self._tenant_id)
        principals = set(expand_principals(self._identity, user_oid, scope))
        lister = getattr(self._index, "list_doc_acls", None)
        if not callable(lister):
            return []
        rows: list[dict] = []
        for row in lister(scope):
            allowed = set(getattr(row, "allowed_principals", None) or [])
            if not (principals & allowed):
                continue                      # LAW 2 - see the docstring above
            rows.append({"doc": row.doc_external_id,
                         "title": getattr(row, "title", "") or "",
                         "uri": getattr(row, "uri", "") or ""})
        return rows

    def has_visible_content(self, user_oid: str, tenant_id: "str | None" = None) -> bool:
        """#304: does this identity have ANY retrievable content in the index? An EXISTENCE check
        for health/exercise. It deliberately bypasses the #53 relevance floor (which drops content
        that doesn't match the QUERY — a health probe query like a store's id matches nothing) so a
        fully-indexed, authorized source is never reported 'empty / not indexed yet'. LAW 2 holds:
        the check only ever sees the caller's own authorized docs."""
        scope = as_read_scope(tenant_id, self._tenant_id)
        principals = expand_principals(self._identity, user_oid, scope)  # (1) same trim as retrieve
        exists = getattr(self._index, "has_authorized", None)
        if callable(exists):
            return bool(exists(principals, scope))
        # fallback for indexes without the score-free check: a broad lexical probe (floor OFF).
        query_vec = self._embedder.embed(["the and of to a in is for it"])[0]
        return bool(self._index.search(query_vec, principals, 1, scope))

    def retrieve(self, user_oid: str, question: str,
                 tenant_id: "str | None" = None) -> list[RetrievedChunk]:
        """The permission-trimmed retrieval core. Returns ONLY chunks this user may see.

        With hybrid reranking on (#44), the index returns a LARGER trimmed candidate pool which
        is then re-ordered by RRF of vector + lexical scores and cut to top_k. The principals
        filter inside index.search still does the trim, so reranking only ever reorders
        already-authorized chunks — LAW 2 is unaffected.

        ADR 0012: the index call ALWAYS carries an explicit tenant — the per-call override
        (the caller's verified tid on the hosted path) or this service's constructor tenant."""
        scope = as_read_scope(tenant_id, self._tenant_id)
        # (1) #601 / ADR 0020: expansion is conversation-aware, so a conv-scoped grant
        # authorizes only inside its own conversation - `scope.active_conv_id` carries which.
        principals = expand_principals(self._identity, user_oid, scope)
        query_vec = self._embedder.embed([question])[0]            # (2)
        candidate_k = self._top_k * self._rerank_factor if self._rerank else self._top_k
        hits = self._index.search(query_vec, principals, candidate_k, scope)  # (3) both trims
        # (3b) #498: the KEYWORD candidate source. The #44 hybrid rerank could only ever
        # REORDER the vector-selected pool, so a chunk with no meaning-shape - one line
        # of raw JSON 200KB into a directory dump - never entered the pool at all, and
        # "What is Loo Say Hoo's job title?" declined with the fact sitting in the index.
        # Terms are matched inside the index under the SAME trims; union is by chunk_id,
        # so the rerank now chooses across both modalities instead of one.
        lexical = getattr(self._index, "lexical_search", None)
        if callable(lexical):
            seen = {h["chunk_id"] for h in hits}
            hits = hits + [h for h in lexical(_question_terms(question), principals,
                                              _LEXICAL_ADDITIONS, scope)
                           if h["chunk_id"] not in seen]
        out: list[RetrievedChunk] = []
        for h in hits:                                             # (4) post-trim only
            out.append(RetrievedChunk(
                doc_external_id=h["doc_external_id"],
                title=h.get("title", ""),
                uri=h.get("uri", ""),
                text=self._store.get(h["text_ref"]).decode(),
                score=h.get("score", 0.0),
                locator=h.get("locator", {}),
                owner_oid=h.get("owner_oid"),
            ))
        if self._rerank:                                          # (4b) hybrid rerank, post-trim
            from dbsearch.query.rerank import hybrid_rerank
            out = hybrid_rerank(question, out, len(out))          # reorder the FULL pool; cut below
        if self._relevance_floor:                                 # (4c) #53/#54 drop filler before top_k
            from dbsearch.query.rerank import relevance_floor     # so a dropped slot frees a real hit
            out = relevance_floor(question, out, rel_lexical=self._floor_rel_lexical,
                                  rel_score=self._floor_vector_rescue, enabled=True)
        return out[:self._top_k]

    @staticmethod
    def _context_and_citations(hits: list) -> "tuple[list[str], list[dict]]":
        """One numbered context block per DOCUMENT, and the citation list in the SAME order (#257).

        The adapter numbers every block it is handed 1..N, and the reader resolves a [n] against
        the citation list — so the two must be built from the same units. They were not: context
        was one block per CHUNK while citations were deduplicated per DOCUMENT, so a document
        retrieved as 6 chunks offered the model [1]..[6] while only [1] existed to click. Seen
        live: "what is holiday days" cited [4] against a single citation.

        This is #233's rule ("evidence numbering stays 1..N in lockstep with the footnotes built
        from that same evidence") applied to the /search path, which had its own copy of the
        numbering and drifted from it. Grouping is by document and preserves first-appearance
        order, so relevance ranking still decides what comes first; chunks of one document are
        joined rather than merged with any other document's, because collapsing two documents
        into one footnote would misattribute the claim."""
        blocks: "dict[str, list[str]]" = {}
        order: list[str] = []
        meta: dict = {}
        for h in hits:
            if h.doc_external_id not in blocks:
                blocks[h.doc_external_id] = []
                order.append(h.doc_external_id)
                meta[h.doc_external_id] = {"doc": h.doc_external_id, "title": h.title,
                                           "uri": h.uri, "locator": h.locator}
            blocks[h.doc_external_id].append(h.text)
        return (["\n\n".join(blocks[d]) for d in order], [meta[d] for d in order])

    @staticmethod
    def _drop_dangling_markers(answer: str, n_citations: int) -> str:
        """Remove any [n] the reader could not resolve (#257).

        Lockstep numbering (_context_and_citations) is necessary but NOT sufficient: the model
        also picks numbers up out of the CONTENT. Observed live against a single source — a
        policy document with numbered headings ("1. ENTITLEMENT", "2. CARRYOVER",
        "4. PUBLIC HOLIDAYS") produced "holiday days refer to public holidays [4]" and
        "the carryover rule states [2]" when exactly ONE citation existed. The model was
        echoing the document's section numbers as if they were footnotes.

        A marker that resolves to nothing is worse than no marker: it reads as corroboration —
        "this claim is sourced, see 4" — and there is no 4. So anything outside 1..N is dropped
        (the prose survives, the false promise does not). Deliberately DROP rather than
        renumber: renumbering would attach the claim to a source the model never pointed at,
        inventing provenance instead of removing it.

        BOTH SPELLINGS, since #861. This matched `[n]` only, and the model's OWN convention is
        `【n†Lx-Ly】` (or a bare `【n】`) — which `components.js` renders to the reader as a
        `[n]` footnote exactly like the plain form, and which `_MARK_ANY` has always counted.
        So the docstring above claimed a guarantee this function did not provide for the
        marker form the model actually emits most: measured on prod 260820, four consecutive
        routed answers came back carrying `【1】` and `【1†L1-L2】` and nothing checked them.
        The two spellings are one rule and must not have two behaviours.

        NOT the grouped form (`[1, 9]`) — `_MARK_ANY` counts it and this still does not touch
        it, because a group needs REWRITING to its in-range members rather than deleting, and
        deleting "[1, 9]" would take a real source with it. Carded as #866, pinned by a test.

        ORDERING: must run AFTER `_citation_quotes`, which slices the model's `【n†Lx-Ly】`
        ranges and is the only moment those ranges exist."""
        if not answer:
            return answer
        answer = re.sub(r"\s?\[(\d+)\]|\s?【\s*(\d+)\s*(?:†[^】]*)?】",
                        lambda m: m.group(0)
                        if 1 <= int(m.group(1) or m.group(2)) <= n_citations else "",
                        answer)
        # #893, and it is the SAME rule as the one above rather than a second one: a marker
        # the reader cannot resolve does not reach the reader. The rule above enforces that
        # for OUR two spellings by number; this enforces it for the shapes that are not our
        # format at all, which the number test could never reach because it never matched
        # them. Found on prod: "...has been confirmed【9†L1-L4】" - and the same generation
        # family also cites by FILENAME, which no numbering makes resolvable.
        #
        # Runs SECOND, on the survivors, so the numeric marker this function has just decided
        # to keep is still there to be protected by the lookahead.
        return _FOREIGN_MARK.sub("", answer)

    @staticmethod
    def _referenced(answer: str, n_citations: int) -> list[int]:
        """The citation numbers the FINAL answer actually points at (#724).

        Read off the answer the READER sees, after `_drop_dangling_markers`, so it can only
        ever name a marker that is really on screen. Out-of-range numbers are ignored for
        exactly the reason that function drops them: they index a source the model never
        pointed at, and counting one as a reference would manufacture the contribution this
        is here to measure.

        An empty list is the honest signal for "this surface contributed nothing" - the model
        was handed context and referred to none of it. That is a weaker claim than "the model
        used nothing" (it may have read a block and cited nothing), and it is deliberately the
        weaker one: what a model was SHOWN is knowable, what it USED is not (#622/#633). What
        we can say is whether the answer OFFERS the reader anything to check, and a paragraph
        with no resolvable marker offers nothing - so displaying a Sources list beneath it
        claims a provenance the answer never asserted."""
        seen: set[int] = set()
        for m in _MARK_ANY.finditer(answer or ""):
            for raw in _INTS.findall(m.group(1) or m.group(2) or ""):
                n = int(raw)
                if 1 <= n <= n_citations:
                    seen.add(n)
        return sorted(seen)

    @staticmethod
    def _citation_quotes(raw_answer: str, context: list[str], citations: list[dict]) -> None:
        """#633: attach to each citation the passage a reader can check the claim against.

        TWO KINDS, LABELLED, because only one of them is a claim about the MODEL:

          pointed   - sliced from the model's own 【n†Lx-Ly】 range over context block n.
                      This is what the answer points at, and it exists only when the model
                      said so.
          retrieved - the head of that document's context block. Chunks arrive relevance-
                      ordered, so the head is its best-ranked passage. This is evidence the
                      model was HANDED.

        The distinction is load-bearing and is the same rule the Sources rail keeps (#622):
        what a model was SHOWN is knowable, what it USED is not. A quote whose provenance is
        "we put this in the prompt" must never wear the words "this is where the answer came
        from" - that would dress evidence up as attribution, and a reader who trusts the
        label has no way to discover it was wrong.

        RUNS OVER THE RAW ANSWER, before `_drop_dangling_markers`, because that function
        deletes exactly the markers this reads and it is the only moment the ranges exist.
        A marker outside 1..N is ignored here for the same reason it is dropped there: it
        would index into a block belonging to a different document, which is how you invent
        provenance rather than record it.

        Mutates `citations` in place. A block with no text gets NO quote key at all - an
        absent key reads as "no quote", where an empty string renders as a broken blockquote.
        """
        pointed: dict = {}
        for m in _MARK_RANGE.finditer(raw_answer or ""):
            n = int(m.group(1))
            lo = int(m.group(2))
            hi = int(m.group(3) or m.group(2))
            # First marker for a source wins: later ones are usually the same passage cited
            # again, and picking the last would silently change the quote as an answer grows.
            if 1 <= n <= len(context) and n not in pointed:
                pointed[n] = (lo, hi)
        for i, c in enumerate(citations):
            block = context[i] if i < len(context) else ""
            if not block:
                continue
            rng = pointed.get(i + 1)
            if rng:
                lines = block.splitlines()
                lo = max(rng[0] - 1, 0)
                hi = min(rng[1], len(lines))
                text = " ".join(ln.strip() for ln in lines[lo:hi] if ln.strip()).strip()
                if text:
                    c["quote"], c["quote_kind"] = _cap_quote(text), "pointed"
                    continue
                # The range named lines that are not there. The model does emit these (see
                # `_drop_dangling_markers` on where its numbers come from), and inventing a
                # slice to satisfy it would be worse than admitting we only have the head.
            c["quote"], c["quote_kind"] = _cap_quote(block), "retrieved"

    def answer(self, user_oid: str, question: str, llm: "LlmPort | None" = None,
               tenant_id: "str | None" = None) -> QueryResult:
        # `llm` lets the caller pick a generation model (#43 pluggable models) WITHOUT touching
        # retrieval: the permission-trimmed retrieve() below is byte-identical regardless (LAW 2).
        hits = self.retrieve(user_oid, question, tenant_id=tenant_id)
        context, citations = self._context_and_citations(hits)      # (5) lockstep — #257
        generated = (llm or self._llm).answer(question, context)
        # #633: BEFORE the marker sweep below, which deletes the very ranges this reads.
        self._citation_quotes(generated["answer"], context, citations)

        final = self._drop_dangling_markers(generated["answer"], len(citations))
        return QueryResult(
            answer=final,
            citations=citations,
            retrieved_docs=_distinct_docs(hits),
            retrieved_owners=sorted({h.owner_oid for h in hits if h.owner_oid}),
            referenced=self._referenced(final, len(citations)),   # #724
        )

    def answer_stream(self, user_oid: str, question: str, llm: "LlmPort | None" = None,
                      tenant_id: "str | None" = None):
        """Streaming twin of answer() (#50). Retrieval/trim is the SAME byte-identical
        permission-trimmed retrieve() (LAW 2 unaffected); only generation is streamed.
        Yields {'type':'token','text':..} events, then one {'type':'done', answer, citations,
        retrieved_docs} event (plus the deprecated `authorized_docs` alias - see
        QueryResult)."""
        hits = self.retrieve(user_oid, question, tenant_id=tenant_id)
        # #257: same lockstep as answer(). This path had its own copy of the citation build and
        # its own context expression, so the two could drift apart independently.
        context, citations = self._context_and_citations(hits)
        parts: list[str] = []
        for tok in (llm or self._llm).answer_stream(question, context):
            parts.append(tok)
            yield {"type": "token", "text": tok}
        # streamed tokens are already gone, so the final answer is the sanitised one — a
        # client that renders the stream should re-render from 'done' (#257)
        retrieved = _distinct_docs(hits)
        # #633: same order as answer() - quotes are read off the RAW stream, before the
        # marker sweep. The two paths had drifted apart once already (#257), so this sits
        # in the same place in both.
        self._citation_quotes("".join(parts), context, citations)
        final = self._drop_dangling_markers("".join(parts), len(citations))
        yield {"type": "done",
               "answer": final,
               "citations": citations,
               "referenced": self._referenced(final, len(citations)),   # #724
               "retrieved_docs": retrieved,
               "retrieved_owners": sorted({h.owner_oid for h in hits if h.owner_oid}),
               "authorized_docs": retrieved}      # deprecated alias (#393)
