"""Ports — the internal interfaces that keep us cloud-portable (SKILL.md LAW 7).

Core logic depends ONLY on these abstractions. Each cloud provides adapters
(see ../adapters/). NO cloud SDK call may appear outside an adapter — that is a
Gate item. Azure adapters come first (ADR 0001/0004); AWS/GCP are later adapters
with zero changes to core.

These are intentionally thin stubs for Phase 0 — they define the boundary, not
the implementation.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable

from dbsearch.core.models import (Chunk, CorpusStatus, DirectoryPrincipal, DocACL, Document,
                                  IndexStats, Principal, PrincipalDirectory, Segment)


class ConnectorPort(ABC):
    """A data source (SharePoint, Confluence, ...). Source-specific, NOT cloud-specific.
    Must be isolated, idempotent, resumable, permission-aware (LAW 3, CONNECTORS.md)."""

    @abstractmethod
    def authenticate(self, config: dict) -> object: ...

    @abstractmethod
    def list_changes(self, cursor: str | None) -> tuple[list[dict], str | None]:
        """Incremental sync via the source's delta token. cursor=None => full crawl."""

    @abstractmethod
    def fetch_content(self, item: dict) -> tuple[bytes, str]:  # (bytes, mime)
        ...

    @abstractmethod
    def fetch_acl(self, item: dict) -> list[Principal]:
        """REQUIRED. A connector that can't return ACLs is not shippable (LAW 2)."""

    @abstractmethod
    def to_documents(self, item: dict) -> list[Document]: ...

    def external_ids(self, item: dict) -> list[str]:
        """The stable source-side ids `item` will produce, WITHOUT fetching or building it.

        A resumed crawl (ADR 0016, #455) has to decide "is this one already indexed?" for
        every item in the batch, and it must decide it before paying for the item. Going
        through `to_documents` would defeat that: GraphSharePointConnector.to_documents calls
        fetch_acl, which is a Graph round-trip per item — so a resume of a 4884-document
        library would spend thousands of network calls re-deriving ids for documents it is
        about to skip.

        The default is correct for every connector and merely expensive; connectors that can
        answer from the item dict override it (the optional-capability pattern used by
        extract_segments / stats / list_doc_acls). Returning [] means "cannot say cheaply",
        which is treated as not-skippable — never as nothing-to-do."""
        return [d.external_id for d in self.to_documents(item)]

    def deletions(self, item: dict) -> list[str]:
        """#910: the external ids `item` DELETES at the source, or [] for an ordinary
        content item.

        A delta feed reports removals as well as additions (Graph marks them with a
        `deleted` facet), and a connector that swallows them leaves two lies behind: the
        node's corpus count can never go down, and the deleted document's chunks keep
        serving stale content under a stale ACL (LAW 2 freshness). The runner handles a
        deletion item entirely through this hook — no fetch, no Document — so `to_documents`
        never has to represent a tombstone.

        The default is the pre-#910 behaviour (no connector reported deletions), correct
        for full-crawl-only connectors; delta connectors override it."""
        return []


class QueuePort(ABC):
    """Durable queue between pipeline stages (LAW 4). Azure: Service Bus."""

    @abstractmethod
    def publish(self, topic: str, message: dict) -> None: ...

    @abstractmethod
    def consume(self, topic: str) -> Iterable[dict]: ...


class ObjectStorePort(ABC):
    """Raw-doc / artifact storage IN the customer tenant (LAW 1). Azure: Blob."""

    @abstractmethod
    def put(self, key: str, data: bytes) -> str: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    def delete_prefix(self, prefix: str) -> int:
        """Delete every key that EQUALS `prefix` or starts with `prefix + "/"`, return the
        count removed.

        #576's retention sweep calls this with `prefix` = a document's own key stem
        (`raw/{tenant}/{doc_id}`, `segments/{tenant}/{doc_id}`, `chunk/{tenant}/{doc_id}`,
        `emb/{tenant}/{doc_id}`) - `raw`/`segments` are a single LEAF blob (matched by the
        equality arm), `chunk`/`emb` are a numbered FAMILY of blobs (matched by the
        `prefix + "/"` arm, e.g. `chunk/{tenant}/{doc_id}/0`).

        #576 review Finding 5 (CRITICAL): a naive `str.startswith(prefix)` with no boundary
        has no such split and is a real cross-document deletion bug - `doc_id="policy"` is
        a Python-string-prefix of `doc_id="policy-2024"`, so sweeping the first account
        would silently delete the second account's raw blob too. The `prefix` OR
        `prefix + "/"` rule closes that: `"raw/t/policy-2024"` is neither equal to
        `"raw/t/policy"` nor does it start with `"raw/t/policy/"`, so it is never touched.

        Optional capability, same shape as `list_doc_acls`: implemented by the local
        adapters; a cloud adapter that has not wired it yet inherits this raise (LAW 7),
        and the sweep counts that as `blobs_unsupported` rather than silently leaving bytes
        behind."""
        raise NotImplementedError(f"{type(self).__name__}.delete_prefix() not implemented")

    def free_bytes(self) -> int:
        """Free space on the medium this store writes to (#831's disk-headroom guard).

        Optional capability, same shape as `delete_prefix`. NotImplementedError means this
        store cannot fill the application host's disk - the in-memory store does not
        persist, and a cloud blob store writes remotely - so the caller must treat it as
        "cannot measure, nothing to protect" and not enforce. The filesystem store is the
        one adapter whose writes land on the local disk, and it answers."""
        raise NotImplementedError(f"{type(self).__name__}.free_bytes() not implemented")


class UnsupportedMedia(Exception):
    """Mime type an extractor can't parse (caller -> 415)."""


class ParseProducedNoText(Exception):
    """Parse succeeded but yielded no usable text (caller -> 422)."""


class ItemUnreadable(Exception):
    """A listed item that is PERMANENTLY, PER-ITEM unfetchable (a 403/404 or equivalent -
    an individually-restricted or deleted file), never a transient or systemic failure.

    Distinct from UnsupportedMedia (a parse problem in bytes we hold): this is the source
    refusing to hand over the bytes at all. The ingest runner skips-and-counts it when
    strict=False - one connector/item failing never blocks the rest (LAW 3) - and the count
    surfaces as IngestResult.unreadable, because skipping QUIETLY reproduces the
    silently-partial store (#551): a store that looks full and is not.

    A TRANSIENT or SYSTEMIC failure (429 rate limit, 5xx, or any other unexpected status)
    must NOT raise this - it must fail the whole crawl instead (a plain exception, e.g.
    RuntimeError). Reason: a connector's sync cursor is typically computed during LISTING,
    before any item is fetched, so a fetch failure can never move the cursor back - an item
    skipped-and-counted here is already behind the advanced cursor and will never be
    re-listed by a future incremental crawl. Counting a retryable failure as "unreadable"
    therefore turns it into permanent, silent data loss. See gdrive.py's
    `_raise_for_fetch_failure` for the fully-worked example this rule was settled on."""


class ExtractorPort(ABC):
    """Text/OCR extraction. Azure: AI Document Intelligence.

    Implementations signal the two recoverable failures with UnsupportedMedia /
    ParseProducedNoText: part of the port contract, since callers (the ingest runner,
    the upload endpoint) branch on them."""

    @abstractmethod
    def extract(self, data: bytes, mime: str) -> str: ...

    def extract_segments(self, data: bytes, mime: str) -> list["Segment"]:
        """Structured extraction: text + per-unit locator (slide/page/row/section).

        Default wraps extract() into a single locator-less whole-doc segment, so existing
        adapters (PlainTextExtractor, Azure DocIntelligence) need no change — the codebase's
        optional-capability pattern (cf. stats/list_doc_acls/answer_stream). Rich adapters
        override for granularity. Locator is metadata only, never uplinked (LAW 1)."""
        text = self.extract(data, mime)
        return [Segment(text=text)] if text else []


class EmbeddingPort(ABC):
    """Embeddings. Azure: Azure OpenAI. Pluggable / BYO model (LAW 9)."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class ReadScope:
    """WHERE a read may look (#582 / ADR 0019 D3): the caller's own partition, plus a
    doorway of explicitly-shared documents.

    `doorway` holds (tenant_id, doc_external_id) pairs derived SERVER-SIDE from the
    caller's live grants (`_request_scope`, server/app.py). Nothing a client sends ever
    reaches it - the same discipline the partition itself has carried since ADR 0012.

    This is partition ROUTING, not authorization. The ACL overlap remains the single
    enforcement point (ADR 0017 s1): a doorway says a document may be LOOKED at, and the
    ACL still decides whether it may be SEEN. `test_the_doorway_never_overrides_the_acl`
    pins exactly that, and it is the property that makes this safe rather than convenient.

    A doorway pair opens ONE document, never the partition it lives in. That is why this
    is a set of pairs rather than a set of partitions: a bug in the ACL test still cannot
    expose more than the documents somebody deliberately shared.

    `ReadScope(partition)` with an empty doorway is byte-for-byte the pre-#582 behaviour.

    `active_conv_id` (#601 / ADR 0020) is the OTHER half of "where this request may look":
    which conversation, if any, this read is happening inside. It rides here rather than
    being derived separately at each layer so the two halves travel together and cannot be
    computed from different inputs. Note carefully that it does NOT participate in
    `allows()` below - `allows` is partition routing, and the conversation rule is an
    AUTHORIZATION rule enforced on the ACL side (`GrantRegistry.live_principals_for`).
    Putting it in `allows` is exactly the mistake ADR 0020 corrects: `allows` returns True
    on its partition-equality arm before it ever reaches the doorway, so any rule expressed
    only there is silently skipped whenever grantor and grantee share a partition.
    """
    partition: str
    doorway: frozenset = field(default_factory=frozenset)
    active_conv_id: "str | None" = None

    def allows(self, tenant_id: str, doc_external_id: str) -> bool:
        """THE ROUTING predicate. Every in-process adapter calls this rather than
        reimplementing it, so two adapters cannot drift apart. pgvector expresses the same
        rule in SQL, and tests/selftest_582_doorway_parity.py pins that the two agree.

        Not an authorization test, and never sufficient on its own: the ACL overlap is the
        single enforcement point (ADR 0017 s1). The partition-equality arm short-circuits,
        which is correct for routing and is why no authorization rule may live here."""
        return tenant_id == self.partition or (tenant_id, doc_external_id) in self.doorway


def as_read_scope(value, default_partition: str = "") -> ReadScope:
    """Normalize a partition-or-scope into a `ReadScope`, for the PRODUCT layer only.

    A bare partition string is a complete and unambiguous scope: "my own partition, no
    doorway" - which is precisely the pre-#582 contract, and the right answer for every
    caller that has no sharing concept (the structured/router rail, admin metadata, the
    ingest paths). Widening a string into a scope loses nothing.

    `None` AND `""` ARE DIFFERENT ANSWERS AND THIS IS THE WHOLE POINT (#790).

      None  "no partition was supplied"      -> the caller's default. The single-tenant path.
      ""    "resolution ran and FAILED CLOSED" -> stays empty, and matches no chunk.

    Until #790 this read `value or default_partition`, and `""` is falsy, so the fail-closed
    value was rewritten into the deployment constant. The comment here swore it never did that.
    It was reachable: `resolve_tenant` returns `""` for every NON-OPERATOR api key, any signed-in
    user can mint one at POST /developer/keys, and GraphQL passes that string straight down while
    REST passes a `ReadScope` object the isinstance arm returns untouched. Same identity, same
    key, same question: REST retrieved nothing and GraphQL retrieved the home partition's
    documents. The ACL overlap still ran, so this was not an unauthenticated dump - it was
    defence in depth collapsing from two predicates to one, plus a wrong-corpus correctness bug,
    in the one property this repo claims to enforce everywhere.

    `value if value is not None` is the fix and `value or ...` is not; they differ on exactly the
    string that matters. A caller that genuinely wants the default must pass `None`, which is
    what omitting an argument already means everywhere else in this codebase.

    Deliberately NOT used inside `IndexPort` implementations. The index is where LAW 2 and
    the partition predicate are enforced, and a caller that reaches it without an explicit
    scope must still fail loudly rather than be quietly given one.
    """
    if isinstance(value, ReadScope):
        return value
    return ReadScope(partition=default_partition if value is None else value)


def expand_principals(identity, user_oid: str, scope: "ReadScope | None" = None) -> list[str]:
    """THE one place a scoped read expands its principals (#601 / ADR 0020).

    Principal expansion is authorization, and a conversation share is an authorization that
    only holds inside one conversation - so the conversation has to reach expansion, not
    only the partition router. This is the single seam that carries it, so that "which
    conversation is active" is asked once per read path rather than re-derived per layer.

    Fails to the NARROW side twice over. An identity that has never heard of conversations
    (every plain `IdentityPort` adapter) is called through its ordinary `expand_groups`,
    which for a grant-aware identity already drops every conv-scoped principal. And a caller
    that passes no scope, or a scope with no active conversation, gets the same. Widening
    requires naming a conversation explicitly; nothing here can widen by omission.
    """
    fn = getattr(identity, "expand_groups_scoped", None)
    if callable(fn):
        return list(fn(user_oid, getattr(scope, "active_conv_id", None)))
    return list(identity.expand_groups(user_oid))


class IndexPort(ABC):
    """Hybrid vector+keyword index with security trimming (LAW 2). Azure: AI Search."""

    @abstractmethod
    def upsert(self, chunks: list[Chunk]) -> None: ...

    @abstractmethod
    def search(self, query_embedding: list[float], principals: list[str], top_k: int,
               scope: "ReadScope") -> list[dict]:
        """`principals` is a MANDATORY filter applied here — callers cannot omit it.

        `scope` is a SECOND mandatory filter (ADR 0012, widened by ADR 0019): the
        partition key of the document plane, plus the caller's doorway of explicitly
        shared documents. Required, not optional-with-default, so a caller that omits it
        fails loudly rather than silently querying across tenants. It is built from
        server-supplied verified values only (the session's Entra tid or the deployment
        constant, and the caller's own live grant records) — never user data.

        #582: this was a bare `tenant_id: str` until ADR 0019. A chunk qualifies when
        `scope.allows(chunk.tenant_id, chunk.doc_external_id)` — own partition, or a
        document deliberately shared with this caller. The ACL overlap below is unchanged
        and still decides what may actually be returned."""

    @abstractmethod
    def delete(self, tenant_id: str, doc_external_id: str) -> None: ...

    # --- admin read surface (Phase 2). Optional capability: in-memory implements it;
    # cloud/pg adapters inherit this default until they become a tested path (LAW 7). ---
    def stats(self, tenant_id: str) -> IndexStats:
        raise NotImplementedError(f"{type(self).__name__}.stats() not implemented")

    def corpus_status(self, scope: "ReadScope", principals: list[str]) -> "CorpusStatus":
        """Is there anything indexed here, and how much of it may `principals` see (#392).

        Same optional-capability shape as stats(): implemented by the in-memory and
        pgvector adapters, NotImplementedError elsewhere until each becomes a tested path
        (LAW 7). Callers must treat that as "unknown" and say nothing, never as "empty" -
        guessing empty would put a false "no documents indexed" in front of a user whose
        corpus is fine.

        `principals` is mandatory for the same reason it is on search(): a count that
        ignored the ACL would advertise documents the caller cannot retrieve (LAW 2)."""
        raise NotImplementedError(f"{type(self).__name__}.corpus_status() not implemented")

    def list_doc_acls(self, scope: "ReadScope") -> list[DocACL]:
        raise NotImplementedError(f"{type(self).__name__}.list_doc_acls() not implemented")

    def docs_owned_by(self, tenant_id: str, owner_oid: str) -> list[str]:
        """Doc external ids this account ingested INTO this partition (#576's retention
        sweep - "whose documents are these" for an account that has gone silent).
        `owner_oid` is attribution metadata (ADR 0012), never an ACL - this reads it back
        for exactly one purpose: deciding what a silent workspace's sweep should delete.
        Optional capability, same shape as `list_doc_acls`: implemented by the in-memory
        and pgvector adapters, NotImplementedError elsewhere until wired (LAW 7). The
        sweep treats that as "cannot enumerate" and skips the partition rather than
        guessing."""
        raise NotImplementedError(f"{type(self).__name__}.docs_owned_by() not implemented")

    def usage_bytes(self, tenant_id: str, owner_oid: str) -> int:
        """Raw bytes this account has entrusted to this partition (#775, ADR 0027 rule 3).

        Only explicitly UPLOADED content has a size here. Connector content is read from the
        customer's own tenant and never held by us, so it contributes nothing and must never
        be metered - that is rule 1 of the same ADR, and the reason a quota can exist at all
        without touching the in-tenant thesis.

        Optional capability, same shape as `docs_owned_by`. NotImplementedError means this
        deployment cannot meter, and the caller must treat that as "no quota here" rather
        than "zero used": a self-hosted box holds its own storage and is free forever
        (ADR 0027 rule 6), so refusing its uploads would be enforcing a bill nobody sends.
        """
        raise NotImplementedError(f"{type(self).__name__}.usage_bytes() not implemented")

    def list_doc_segments(self, scope: "ReadScope", doc_external_id: str, principals: list[str]) -> list[dict]:
        """Admin 'verify data' read: this doc's chunks as [{chunk_id, locator, preview}].
        `principals` is a MANDATORY filter, same as search() (LAW 2) — a chunk is only
        returned if its allowed_principals intersects the caller's principal set, so an
        unauthorized caller gets an empty list, never a peek at content it can't see.
        In-memory implements it; cloud/pg adapters until wired inherit this raise (LAW 7)."""
        raise NotImplementedError(f"{type(self).__name__}.list_doc_segments() not implemented")


class IdentityPort(ABC):
    """Identity + transitive group expansion (LAW 2). Azure: Entra ID via Graph."""

    @abstractmethod
    def expand_groups(self, user_oid: str) -> list[str]:
        """Return the user's oid + all (transitive) group oids."""

    def list_principals(self) -> PrincipalDirectory:
        """Admin directory view (Phase 2). In-memory implements it; Entra raises until wired."""
        raise NotImplementedError(f"{type(self).__name__}.list_principals() not implemented")

    def list_directory(self) -> "list[DirectoryPrincipal]":
        """Named principals an operator can pick when writing an ACL (#258).

        RAISES rather than returning [] when a backend cannot enumerate the directory.
        An empty list is indistinguishable from "this tenant has no groups", which would
        render an empty picker that looks authoritative and quietly pushes the operator
        back to pasting raw GUIDs — the same class of confident-but-false signal as #255.
        Callers must surface the unavailability, not paper over it."""
        raise NotImplementedError(f"{type(self).__name__}.list_directory() not implemented")


class LlmPort(ABC):
    """Answer generation. Azure: Azure OpenAI. Pluggable / BYO (LAW 9).
    MUST only ever receive post-trim content (LAW 2, PERMISSIONS.md)."""

    #: Does every prompt to this adapter stay inside the customer tenant? Gates the ONE
    #: sanctioned values-in-a-prompt exception (#462, ADR 0015 amendment: the literal-
    #: resolution rung). Class-level, default CLOSED: an adapter must claim in-tenancy
    #: explicitly, and a subclass of an in-tenant adapter that talks to a third-party
    #: endpoint (GroqLlm under LlamaLlm) must override it back to False.
    in_tenant: bool = False

    @abstractmethod
    def answer(self, question: str, context_chunks: list[str]) -> dict:
        """Returns {'answer': str, 'citations': [...]} — citations from trimmed results only."""

    def answer_stream(self, question: str, context_chunks: list[str]):
        """Yield the answer token-by-token (#50). Default: non-streaming adapters yield the
        whole answer once, so a caller can always iterate. Only ever receives post-trim
        content (LAW 2)."""
        yield self.answer(question, context_chunks)["answer"]

    def condense_question(self, question: str, history: list[dict]) -> str:
        """Rewrite a follow-up into a standalone question using prior turns (Phase 2.5).

        `history` is a list of primitive dicts ``{"question", "answer"}`` (most recent
        last) — deliberately NOT a query-layer type, so this module imports nothing from
        `dbsearch.query`. Default is a no-op (return `question` unchanged) so existing
        adapters keep working; quality adapters override. Only ever receives this user's
        own post-trim content (LAW 1/2)."""
        return question

    def decompose_question(self, question: str) -> list[str]:
        """Split a COMPOUND question into standalone sub-questions, one per data domain (#215).

        Each part must stand alone — pronouns resolved, and the shared entity/grain carried
        into every part ("per product SKU", "by region"), because that grain is the JOIN KEY
        the synthesizer needs to line up results from different stores. Losing it produces
        halves that cannot be joined: "how much revenue do they bring" becomes total company
        revenue instead of revenue per product.

        Default `[]` means "no opinion" — `router.decompose.llm_decomposer` then falls back to
        the deterministic split, so adapters that don't override keep working. Sees only the
        question, never any content (LAW 1/2)."""
        return []

    def plan_subquestions(self, brief: str, sections: list[str]) -> list[str]:
        """Decompose an opportunity brief into one retrieval sub-question per proposal
        section. Default = a deterministic heuristic (one query per section). Quality
        adapters (Azure OpenAI / LLaMA) override to genuinely decompose the brief.
        Receives only the brief + section titles — no document content."""
        return [f"{s} for: {brief}" for s in sections]

    def draft_section(self, title: str, brief: str, context_chunks: list[str]) -> str:
        """Write one proposal section's prose from already-trimmed context. Returns PROSE
        ONLY — citation assembly stays in the orchestration layer (like answer()), never
        the model. Default = extractive (stitch the trimmed chunk texts). Quality adapters
        override for fluent generation. `context_chunks` MUST be post-trim text (LAW 1/2)."""
        if not context_chunks:
            return "No authorized source material found for this section."
        joined = " ".join(" ".join(c.split()) for c in context_chunks)
        snippet = joined[:500] + ("…" if len(joined) > 500 else "")
        return f"Drawing on {len(context_chunks)} retrieved source(s): {snippet}"

    def draft_section_stream(self, title: str, brief: str, context_chunks: list[str]):
        """Streaming twin of draft_section (#61): yield the section prose token-by-token. Default
        (non-streaming adapters) yields the whole section once, so callers can always iterate.
        `context_chunks` MUST be post-trim text (LAW 1/2)."""
        yield self.draft_section(title, brief, context_chunks)

    # --- conversational proposal draft (#57): the GATHER phase uses these on a CHEAP model ---
    def elicit_requirements(self, history: list[dict]) -> str:
        """Given the gather conversation so far (``[{"question","answer"}]``, most recent last),
        return the assistant's next message — a clarifying question that pins down the proposal's
        client, need, and scope. Receives only the user's own typed messages (no document
        content). Default = a single generic prompt; quality adapters override to ask sharp,
        context-aware questions."""
        if not history:
            return "Tell me about the proposal you'd like to draft — who is the client and what do they need?"
        return "Got it. What else should the proposal cover: scope, constraints, timeline, or budget?"

    def summarize_requirements(self, history: list[dict]) -> str:
        """Condense the gather conversation into a short bullet list of confirmed requirements,
        shown back to the user for sign-off before drafting. Receives only the user's own typed
        messages. Default = echo the user's messages as bullets; quality adapters synthesise."""
        asks = [h.get("question", "").strip() for h in history if h.get("question", "").strip()]
        return "\n".join(f"- {a}" for a in asks) or "- (no requirements captured yet)"


class SecretsPort(ABC):
    """Secrets. Azure: Key Vault.

    #319 (ADR 0010): the port gained a WRITE surface so a self-serve user can supply their
    own database credential without it ever entering a manifest. The asymmetry is deliberate
    and is the whole security property: a value goes IN once and never comes back out over
    an API. `describe_secret` is the only read-side affordance, and it returns existence plus
    a short hint, never the value.
    """

    @abstractmethod
    def get_secret(self, name: str) -> str: ...

    @abstractmethod
    def put_secret(self, name: str, value: str) -> None: ...

    @abstractmethod
    def delete_secret(self, name: str) -> None: ...

    @abstractmethod
    def describe_secret(self, name: str) -> "dict | None":
        """`{"exists": True, "hint": "<= last 4 chars"}`, or None when absent."""
