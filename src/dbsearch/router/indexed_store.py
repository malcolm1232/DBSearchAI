"""IndexedStore — a StorePort over a DBSearch-owned vector index (Phase E, card #98).

Wraps the existing QueryService.retrieve() — the permission-trimmed LAW-2 core — so the
router reuses the SAME trim byte-for-byte and can never bypass it. retrieve() maps each
RetrievedChunk into an Evidence(kind=chunk) with citation provenance. Federated (SQL/doc)
stores are separate adapters over the same StorePort (E4/E5).
"""
from __future__ import annotations

from dbsearch.ports.base import IdentityPort
from dbsearch.query import QueryService
from dbsearch.router.evidence import CHUNK, Evidence
from dbsearch.router.store import AccessContext, INDEXED, SEMANTIC, StorePort, StoreProfile

# #453 fix-round-1 (finding 2, arc review of tasks 10/10b/10c): unconditional, uncapped title
# folding let a haystack of many near-duplicate titles overwhelm a genuinely descriptive
# profile - breadth DILUTION, not a hash collision, since it reproduced on the golden pack
# even at #450's 4096-dim floor (capability D's retrieval-miss rose 3->13 and capability A
# picked up new distractor-cited failures once every doc store, including a 200-doc filler
# corpus, got its full title list folded in). Bound the folded titles to a cumulative WORD
# budget, added whole-title in ingested order, stopping once the NEXT title would exceed it
# (but always keeping at least one), so a store's profile text stays description-dominant in
# order of magnitude: a single-purpose store's few dozen title words still comfortably fit; a
# 200-doc haystack's title words do not all pile in. 30 is roughly the token count of a
# healthy one-to-two-sentence catalog description (see the golden pack's rewritten
# descriptions), which is the scale this is calibrating against, not a precisely tuned
# constant.
_TOPIC_WORD_BUDGET = 30


def _bounded_topics(titles: list, word_budget: int = _TOPIC_WORD_BUDGET) -> list:
    """Fold whole titles (never split mid-title) until the cumulative word count would
    exceed word_budget. Always includes at least the first title, even if it alone
    exceeds the budget, so a single verbose title is never silently dropped to zero."""
    out: list = []
    total = 0
    for title in titles:
        n = len(title.split())
        if out and total + n > word_budget:
            break
        out.append(title)
        total += n
    return out


class IndexedStore(StorePort):
    def __init__(self, store_id: str, business_unit: str, title: str, description: str,
                 query_service: QueryService, identity: IdentityPort,
                 tenant_id: "str | None" = None) -> None:
        self._store_id = store_id
        self._bu = business_unit
        self._title = title
        self._description = description
        self._qs = query_service
        self._identity = identity
        # #439: the ADR 0012 partition this store reads, when it differs from the QueryService's
        # own. A store wrapping the EDITION's shared index (#304/#306) inherits that service's
        # DEPLOYMENT CONSTANT, so a foreign owner's connected SharePoint store queried the home
        # partition and returned nothing through /router/ask - the doc plane's last mile after
        # #389 lifted the gate. None keeps the service's own tenant, which is right for a store
        # with its own dedicated index.
        self._tenant_id = tenant_id

    def _tenant(self) -> dict:
        """Per-call override kwargs, empty when this store has no tenant of its own - so a
        store built the old way calls exactly the old signature."""
        return {"tenant_id": self._tenant_id} if self._tenant_id else {}

    def profile(self) -> StoreProfile:
        # #306/#453: fold ingested doc TITLES into routing topics ALWAYS, additively with the
        # store's own description, never as an either/or. #306 originally gated this on an empty
        # description (a described store kept routing on the description alone), reasoning that
        # titles would shift calibration for stores the user already described. Golden-suite
        # evidence (task 10b) proved that gate wrong: a docs store WITH a real description
        # (hr-wiki: "Company HR policies, benefits, and onboarding documentation.") had ZERO
        # router-level signal for its actual content ("sixteen weeks", "parental leave") at ANY
        # embedding dimension, because the description text never mentions it and titles were
        # never folded in to cover the gap. Description and titles are complementary signals, not
        # alternatives: a description says what a store is FOR, titles say what it actually HOLDS,
        # and the router needs both to rank on real content instead of on prose that may not
        # mention it. LAW 2: titles are store-content metadata; in the self-serve model a store's
        # ACL == its docs' ACL, so this exposes nothing a store-visible user can't already see. A
        # per-doc-ACL multi-user deployment should principal-trim these (follow-up).
        # Bounded per _TOPIC_WORD_BUDGET above (fix-round-1, finding 2): folding is still
        # unconditional (always additive, never gated on description emptiness), just capped
        # in magnitude so a large title corpus cannot dilute a store's profile past the point
        # where its own description still dominates.
        #
        # Merge note (main <- feat/per-owner-workspaces): the branch being merged still had
        # #306's ORIGINAL either/or gate (titles only when the description is empty). #453
        # superseded that gate on golden-suite evidence, so the gate does not come back - but
        # #439's `**self._tenant()` argument is an ORTHOGONAL fix and must survive: a store
        # wrapping the edition's shared index has to read ITS OWN ADR 0012 partition, or a
        # foreign owner's connected store folds in zero titles and silently loses its content
        # signal. Both fixes are kept: unconditional bounded folding, tenant-correct source.
        topics = _bounded_topics(self._qs.content_titles(**self._tenant()))
        return StoreProfile(store_id=self._store_id, title=self._title,
                            description=self._description, kind=INDEXED,
                            capabilities={SEMANTIC}, business_unit=self._bu,
                            topics=topics, proof_kind="document")

    def authorize(self, user_oid: str) -> AccessContext:
        return AccessContext(user_oid=user_oid,
                             principals=self._identity.expand_groups(user_oid))

    def has_content(self, access: AccessContext) -> bool:
        """#304: existence check for health/exercise — does the caller have ANY retrievable doc
        in the index? Independent of a relevance query (a health probe derived from this store's
        id matches no document text), so an indexed, authorized source is never falsely reported
        'not indexed yet'. Same LAW-2 principals trim as retrieve()."""
        return self._qs.has_visible_content(access.user_oid, **self._tenant())

    def documents(self, access: AccessContext) -> list[dict]:
        """#939: the documents THIS caller can see in this store - names, not content.

        Trimmed by `access.user_oid`, which the store itself produced in `authorize`, rather
        than by anything a caller hands in - the same shape `retrieve` uses, and for the same
        reason: the identity that gates the answer must be the identity that gates the list.
        `**self._tenant()` so a store wrapping the edition's shared index reads its OWN ADR
        0012 partition (#439), exactly as `has_content` and `profile` do.
        """
        return self._qs.document_inventory(access.user_oid, **self._tenant())

    def retrieve(self, access: AccessContext, question: str, top_k: int = 5) -> list[Evidence]:
        # QueryService.retrieve re-applies the mandatory principals trim from user_oid,
        # so LAW 2 holds even though access already carries principals (belt + braces).
        chunks = self._qs.retrieve(access.user_oid, question, **self._tenant())[:top_k]
        return [
            Evidence(
                store_id=self._store_id,
                business_unit=self._bu,
                kind=CHUNK,
                content=c.text,
                provenance={"doc": c.doc_external_id, "title": c.title,
                            "uri": c.uri, "locator": c.locator},
                score=c.score,
            )
            for c in chunks
        ]
