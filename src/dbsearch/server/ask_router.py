"""The ask seam: the caller's DOCUMENTS as a first-class store in a routed ask (#689, ADR 0025).

WHY THIS MODULE EXISTS, and it is not what the ADR assumed.

ADR 0025 reasons from "`/router/ask` already subsumes documents since #255, so delegating
`/chat/stream` to the router needs no separate document leg". That is true of the CANVAS and
false of the server. #255's bridge is client-side: `canvas.js` fires `askSharePoint(q)` at
`/search` on every ask and paints the result underneath the router's. Server-side the router
sees only what a manifest COMPOSED, and the edition's uploaded documents are not in anybody's
manifest. Delegate naively and `/ask` gains every database and loses the one plane it can
answer from today - the exact defect #689 reports, pointing the other way.

So the delegate overlays the caller's own document index as a store the router can route to,
for the duration of one ask. This is #255's own stated "real fix (b): compose doc sources as
first-class router stores", scoped to this seam: `/router/ask`, the canvas and every composed
manifest are untouched.

THREE PROPERTIES MAKE THE OVERLAY SAFE TO PUT IN FRONT OF A LIVE CATALOG:

- it never mutates the base catalog. The base is long-lived, shared workspace state that
  `/router/ask` and the canvas read concurrently; this wraps it and adds one node to what it
  REPORTS, so a per-request view can never leak into another request's routing.
- the doc node is visible to its owner alone (gate #1). `visible_stores` adds it only when the
  caller's own principal is in the expansion, so the overlay cannot widen visibility even by
  accident - and the store underneath is the same permission-trimmed `QueryService` core
  `/search` uses, so LAW 2 is enforced twice over.
- it is scoped to the caller's ADR 0012 partition, exactly as `/search` is. The IndexedStore
  takes the request's ReadScope, so an ask never reads another tenant's documents (#439).
"""
from __future__ import annotations

from collections import OrderedDict

from dbsearch.router.catalog import STORE, CatalogNode
from dbsearch.router.indexed_store import IndexedStore
from dbsearch.router.profiles import ensure_profile_vector

#: The overlay node's id. `documents` reads as itself in a citation, an origin line and the
#: Sources rail, which is where a user meets it.
DOCS_ID = "documents"
#: ...unless the caller already composed a store called that. Ids share ONE namespace (#114),
#: and a duplicate would shadow their own store in `get`.
DOCS_ID_ALT = "documents-yours"

#: What the router routes ON for this store, beyond the folded-in document titles
#: (`IndexedStore.profile` adds those). Deliberately generic: this store is "whatever this
#: person uploaded", and a narrower description would make the router prefer a database for
#: questions the documents actually answer.
DOCS_DESCRIPTION = ("Documents this person has uploaded to DBSearch or connected through a "
                    "document source: policies, reports, contracts, notes and other files.")
DOCS_TITLE = "Your documents"
#: #176: what a person reads under a citation from this store.
DOCS_ORIGIN = {"system": "Documents", "location": "indexed in DBSearch"}


class _OwnerRecordingQueryService:
    """Passthrough over a QueryService that remembers WHOSE documents this turn read.

    #576's retention sweep touches the accounts behind retrieved documents so an account
    whose documents are actively being read is never swept as silent. On the document path
    `QueryResult.retrieved_owners` carries that; the routed path maps chunks into `Evidence`,
    which has no room for an account id and must not grow one - an owner oid is server-
    internal (#549 is what happens when one reaches a browser), and `Evidence` is handed to
    synthesis, citations and footnotes.

    The fact is already in scope: `QueryService.retrieve` returns chunks carrying `owner_oid`
    and `IndexedStore` drops it on the way to `Evidence`. So observe it in passing, here, one
    instance per request, and let the delegate read `.owners` after the ask.

    Everything else delegates untouched - `content_titles` and `has_visible_content` are read
    by `IndexedStore.profile()` and by the has-documents check, and they must behave exactly
    as they do for any other caller."""

    def __init__(self, qs) -> None:
        self._qs = qs
        self.owners: set = set()

    def retrieve(self, user_oid: str, question: str, **kwargs):
        hits = self._qs.retrieve(user_oid, question, **kwargs)
        self.owners.update(h.owner_oid for h in hits if getattr(h, "owner_oid", None))
        return hits

    def __getattr__(self, name):
        return getattr(self._qs, name)


#: Embedding a profile costs a model call, and this profile is rebuilt every ask. Keyed on the
#: TEXT the vector is derived from (description + folded titles), so a new upload - which
#: changes the titles - correctly misses and re-embeds, and an ask that changed nothing does
#: not pay. Bounded: an unbounded cache keyed on user content is a memory leak with a slow fuse.
_VECTOR_CACHE: "OrderedDict[tuple, list]" = OrderedDict()
_VECTOR_CACHE_MAX = 128


def _warm_profile_vector(profile, embedder) -> None:
    key = (profile.description, tuple(profile.topics or ()))
    hit = _VECTOR_CACHE.get(key)
    if hit is not None:
        profile.profile_vector = hit
        _VECTOR_CACHE.move_to_end(key)
        return
    vec = ensure_profile_vector(profile, embedder)
    _VECTOR_CACHE[key] = vec
    _VECTOR_CACHE.move_to_end(key)
    while len(_VECTOR_CACHE) > _VECTOR_CACHE_MAX:
        _VECTOR_CACHE.popitem(last=False)


def documents_node(edition, user_oid: str, scope, base_catalog=None) -> "CatalogNode | None":
    """The caller's document index as a STORE node, or None when they have no documents.

    None rather than an empty store, deliberately: a store that exists and answers nothing is
    a store the router can select over a database that WOULD have answered, and #808 is the
    card about how bad that is to be on the receiving end of. No documents, no node, and the
    ask routes exactly as `/router/ask` would.

    `scope` is the request's ReadScope and reaches `QueryService` verbatim, so this store
    reads the caller's own ADR 0012 partition (#439) - the same value `/search` passes."""
    qs = _OwnerRecordingQueryService(edition.query_service)
    if not qs.has_visible_content(user_oid, tenant_id=scope):
        return None
    store_id = DOCS_ID
    if base_catalog is not None:
        try:
            base_catalog.get(DOCS_ID)
            store_id = DOCS_ID_ALT       # their own store owns the name; stand aside
        except KeyError:
            pass
    store = IndexedStore(store_id, DOCS_ID, DOCS_TITLE, DOCS_DESCRIPTION,
                         qs, edition.identity, tenant_id=scope)
    profile = store.profile()
    profile.origin = dict(DOCS_ORIGIN)
    _warm_profile_vector(profile, edition.embedder)
    # `acl=[user_oid]` is belt to `visible_stores`' braces: the overlay decides visibility by
    # ownership, and the node also carries an ACL that says the same thing, so a future reader
    # who goes through the catalog rather than through this class gets the same answer.
    return CatalogNode(id=store_id, kind=STORE, parent_id=None, acl=[user_oid],
                       profile=profile, store=store)


class DocsOverlayCatalog:
    """A read-only view of a composed catalog PLUS the caller's documents node.

    Implements exactly the surface `RouterQueryService` and `decorate_ask_result` read:
    `stores`, `get`, `visible_stores`, `children`, `revision`. It is not a StoreCatalog and
    deliberately has no `register`/`remove` - the base is shared state and this must not be a
    door into mutating it.

    `doc_node` may be None (the caller has no documents), in which case every method is a
    straight passthrough and the routed ask is byte-identical to `/router/ask`."""

    def __init__(self, base, doc_node: "CatalogNode | None", owner: str) -> None:
        self._base = base
        self._node = doc_node
        self._owner = owner

    @property
    def revision(self):
        # The route cache keys on this. A tuple, so a change to EITHER half invalidates: the
        # base recomposing, or this caller's documents changing the profile they route on.
        if self._node is None:
            return self._base.revision
        return (self._base.revision, self._node.id,
                tuple(self._node.profile.topics or ()) if self._node.profile else ())

    def stores(self) -> list:
        base = self._base.stores()
        return base + [self._node] if self._node is not None else base

    def get(self, node_id: str):
        if self._node is not None and node_id == self._node.id:
            return self._node
        return self._base.get(node_id)

    def children(self, node_id: str) -> list:
        return self._base.children(node_id)

    def visible_stores(self, principals: list) -> list:
        """Gate #1, with the documents node added for its OWNER ONLY.

        Checked against the caller's expanded principals rather than trusting the constructor's
        `owner`, because that is the value every other visibility decision in this product is
        made against - a node visible on a different basis from every other node is how a
        visibility rule quietly stops being one rule."""
        base = self._base.visible_stores(principals)
        if self._node is not None and self._owner in set(principals):
            return base + [self._node]
        return base

    def always_consulted(self, principals: list) -> list:
        """#856: this node is ASKED on every turn, not ranked against the databases.

        The router looks for this method by name and a plain StoreCatalog does not have one,
        so `/router/ask`, the canvas and every composed manifest are untouched - the rule is
        the ask surface's, and it exists because of what the ask surface is FOR.

        Measured on prod: "How many days of annual leave do I get?" scored a composed HR
        folder at 0.1333 and this node at 0.0556, so the folder took the whole ask and
        answered from its abbreviated copy of the policy - dropping the "30 days after five
        years of service" clause the caller's own upload carries. The two are not competitors
        of the same kind. A database earns its place by looking relevant; a person's own files
        are worth reading every time, and that belongs in whether we ask rather than in the
        score. `DOCS_DESCRIPTION` says the same thing from the other side: it is generic on
        purpose, so making it win by description would make it lose everywhere else.

        Same visibility rule as `visible_stores`, read off the caller's principals for the
        same reason - a node consulted on a different basis from the one that makes it visible
        is a gate with two answers."""
        if self._node is not None and self._owner in set(principals):
            return [self._node]
        return []
