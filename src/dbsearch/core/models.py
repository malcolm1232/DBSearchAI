"""Core domain models — cloud-agnostic, source-agnostic (SKILL.md LAW 7).

Everything downstream of a connector speaks `Document` / `Chunk`, never the
specifics of SharePoint/Confluence/etc. If a change here forces a connector or
adapter to leak provider types upward, STOP — re-read SKILL.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Principal:
    """An Entra (or other IdP) identity allowed to read something.

    `kind` is "user" or "group". `oid` is the opaque object id — never a name,
    email, or anything human-readable that could leak via telemetry (LAW 1).
    """

    oid: str
    kind: str  # "user" | "group"

    def to_dict(self) -> dict:
        return {"oid": self.oid, "kind": self.kind}

    @staticmethod
    def from_dict(d: dict) -> "Principal":
        return Principal(oid=d["oid"], kind=d["kind"])


@dataclass
class Segment:
    """A parsed unit of a document plus an optional locator (slide/page/row/section).

    `locator` is metadata only — e.g. {"kind": "slide", "n": 7}. It is DATA-PLANE only
    (index + citations shown to an already-authorized user, like title/uri). It must never
    be added to a control-plane uplink or telemetry event (LAW 1). Empty locator = whole-doc.
    """

    text: str
    locator: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"text": self.text, "locator": self.locator}

    @staticmethod
    def from_dict(d: dict) -> "Segment":
        return Segment(text=d["text"], locator=d.get("locator", {}))


@dataclass
class Document:
    """A normalized item emitted by a connector, before parse/embed/index.

    `acl` is mandatory — content without an ACL is unsafe to index (LAW 2/3).
    Large bodies live in object storage; `content_ref` points at them rather
    than carrying bytes through the queue (LAW 4).
    """

    tenant_id: str
    source_id: str            # which connector instance produced this
    external_id: str          # stable id in the source system
    content_ref: str          # object-store key for the raw bytes (not the bytes)
    acl: list[Principal]      # allowed-principals — REQUIRED, default-deny
    title: str = ""           # source-side metadata (stays in the data plane)
    uri: str = ""
    content_hash: str = ""    # for idempotency: (tenant, external_id, hash) (LAW 3)
    source_meta: dict = field(default_factory=dict)
    owner_oid: str | None = None  # ADR 0012: who ingested it — attribution ONLY, never gates retrieval
    #: #775 / ADR 0027 rule 3: the RAW size of what the owner entrusted to us, carried into
    #: the index so quota accounting is a SUM over rows that already exist rather than a
    #: second ledger that drifts. 0 means "we hold no bytes of our own for this document" -
    #: a connector crawl reads the customer's tenant and stores nothing here - never "unknown".
    doc_bytes: int = 0

    def to_dict(self) -> dict:
        """JSON-safe form for the queue (so any QueuePort, incl. Service Bus, works)."""
        return {
            "tenant_id": self.tenant_id,
            "source_id": self.source_id,
            "external_id": self.external_id,
            "content_ref": self.content_ref,
            "acl": [p.to_dict() for p in self.acl],
            "title": self.title,
            "uri": self.uri,
            "content_hash": self.content_hash,
            "source_meta": self.source_meta,
            "owner_oid": self.owner_oid,
            "doc_bytes": self.doc_bytes,
        }

    @staticmethod
    def from_dict(d: dict) -> "Document":
        return Document(
            tenant_id=d["tenant_id"],
            source_id=d["source_id"],
            external_id=d["external_id"],
            content_ref=d["content_ref"],
            acl=[Principal.from_dict(p) for p in d["acl"]],
            title=d.get("title", ""),
            uri=d.get("uri", ""),
            content_hash=d.get("content_hash", ""),
            source_meta=d.get("source_meta", {}),
            owner_oid=d.get("owner_oid"),
            doc_bytes=d.get("doc_bytes", 0),
        )


@dataclass
class Chunk:
    """An indexed unit. `allowed_principals` is denormalized onto every chunk so
    security-trimming at query time is a single index filter, not a join (LAW 2).
    """

    tenant_id: str
    doc_external_id: str
    chunk_id: str
    text_ref: str             # reference, not text — keep payloads small
    allowed_principals: list[str]  # principal oids; intersect with the user's set
    embedding_ref: str = ""
    #: #834: the vector itself, riding the in-process pipeline queue instead of an emb/
    #: blob. TRANSIENT hand-off state: consumed once at upsert (pgvector's column, the
    #: in-memory cache, AI Search's payload) and never durably stored by us - emb/ blobs
    #: were 20% of prod's object store for a value used one stage later. embedding_ref
    #: stays as the legacy fallback so an old-shaped chunk still indexes.
    embedding: "list[float] | None" = None
    title: str = ""           # denormalized for display/citations (stays in data plane)
    uri: str = ""
    locator: dict = field(default_factory=dict)  # {"kind":"slide","n":7} — DATA-PLANE only (LAW 1)
    owner_oid: str | None = None  # ADR 0012: who ingested it — attribution ONLY, never gates retrieval
    #: #775: denormalized onto every chunk the way the ACL is, so usage is one grouped SUM
    #: and a document's bytes leave with its rows when it is deleted. EVERY chunk of one
    #: document carries the SAME value, so a sum must group by document and take the max -
    #: adding the column up directly would multiply a file by its chunk count.
    doc_bytes: int = 0

    def vector(self, store) -> "list[float]":
        """#834: the ONE place the indexing vector is resolved (three adapters call this -
        a copy per adapter is how rules drift). In-message `embedding` first; the legacy
        `embedding_ref` blob as fallback; neither -> refuse, because indexing a chunk
        without a vector produces a row retrieval can never rank - a silent data loss."""
        if self.embedding is not None:
            return self.embedding
        if self.embedding_ref:
            import json as _json
            return _json.loads(store.get(self.embedding_ref).decode())
        raise ValueError(
            f"chunk {self.chunk_id} carries neither an embedding nor an embedding_ref")

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "doc_external_id": self.doc_external_id,
            "chunk_id": self.chunk_id,
            "text_ref": self.text_ref,
            "allowed_principals": self.allowed_principals,
            "embedding_ref": self.embedding_ref,
            "embedding": self.embedding,
            "title": self.title,
            "uri": self.uri,
            "locator": self.locator,
            "owner_oid": self.owner_oid,
            "doc_bytes": self.doc_bytes,
        }

    @staticmethod
    def from_dict(d: dict) -> "Chunk":
        return Chunk(
            tenant_id=d["tenant_id"],
            doc_external_id=d["doc_external_id"],
            chunk_id=d["chunk_id"],
            text_ref=d["text_ref"],
            allowed_principals=d["allowed_principals"],
            embedding_ref=d.get("embedding_ref", ""),
            embedding=d.get("embedding"),
            title=d.get("title", ""),
            uri=d.get("uri", ""),
            locator=d.get("locator", {}),
            owner_oid=d.get("owner_oid"),
            doc_bytes=d.get("doc_bytes", 0),
        )


@dataclass(frozen=True)
class IndexStats:
    """Index health metadata (LAW 1: counts only, no content)."""

    chunk_count: int
    doc_count: int
    embedding_dim: int


@dataclass(frozen=True)
class CorpusStatus:
    """Does this tenant's document plane hold anything, and how much may THIS caller see.

    Two numbers rather than one, because the two failure modes need different words (#392).
    An empty index is a "connect a source" problem the user can act on; an index with
    documents none of which admit them is a permissions statement. Collapsing both into
    "I couldn't find anything you have access to" made an empty corpus read as a
    permissions failure, and the operator reasonably concluded the product was broken.

    Counts only - never titles, ids or content (LAW 1). `authorized_docs` is computed with
    the same mandatory ACL predicate as search(), so it can never overstate what the caller
    could actually retrieve (LAW 2)."""

    indexed: bool          # any document at all in this tenant partition
    authorized_docs: int   # documents whose ACL admits these principals


@dataclass(frozen=True)
class DocACL:
    """A document's ACL for the admin Permission Tester. Identifiers, never body text.

    `owner_oid` is who ingested the doc (ADR 0012 attribution). It exists here so the
    #90 supersede-by-uri loop can be owner-scoped (#791) — a WRITE/DELETE gate, which
    ADR 0012 permits; retrieval must still never be owner-gated."""

    doc_external_id: str
    title: str
    uri: str
    allowed_principals: list[str]
    owner_oid: "str | None" = None


@dataclass(frozen=True)
class PrincipalDirectory:
    """The identity directory the operator can audit: users→groups + all distinct groups."""

    users: dict          # {user_oid: [group_oids]}
    groups: list         # sorted distinct group oids


@dataclass(frozen=True)
class DirectoryPrincipal:
    """One pickable principal for an ACL (#258).

    Deliberately NOT PrincipalDirectory: that one maps user→groups and `identities()`
    derives member_count from it, so filling it for Entra would need a transitive
    getMemberGroups call PER USER (N+1 against Graph). Choosing an ACL entry only needs
    a name and an oid, so this is a flat, cheap listing.

    `name` is what a human picks by; `oid` is what actually lands in the ACL and is the
    only thing LAW 2 ever compares. A directory that cannot supply a name must not
    silently fall back to a blank one — an unlabelled GUID in a picker is no more usable
    than pasting the GUID by hand.
    """

    oid: str
    name: str
    # #881: "directoryRole" joined the set when #872 made roles expandable principals. It is a
    # LABEL for the picker, never a permission input - LAW 2 compares oids and nothing here.
    kind: str            # "group" | "user" | "directoryRole"
