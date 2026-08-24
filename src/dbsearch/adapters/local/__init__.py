"""In-memory adapters for local dev + tests (NOT for production).

These implement every port in dbsearch.ports.base with zero dependencies and no
cloud, so the whole spine runs and is testable on a laptop. The real Azure
adapters (adapters/azure/) implement the SAME ports — swapping them in is the
only change needed to go from this to a live data plane (LAW 7, ADR 0004).

Nothing here ever talks to the control plane; all state is in-process (LAW 1).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from collections import defaultdict
from typing import Iterable

from dbsearch.core.copy import NO_EVIDENCE_ANSWER
from dbsearch.core.models import (Chunk, CorpusStatus, DirectoryPrincipal, DocACL, IndexStats,
                                  Principal, PrincipalDirectory)
from dbsearch.ports.base import (
    EmbeddingPort,
    ExtractorPort,
    IdentityPort,
    IndexPort,
    LlmPort,
    ObjectStorePort,
    QueuePort,
    ReadScope,
    SecretsPort,
)

_TOKEN = re.compile(r"[a-z0-9]+")


def _stable_hash(token: str, dim: int) -> int:
    return int(hashlib.md5(token.encode()).hexdigest(), 16) % dim


class InMemoryQueue(QueuePort):
    """Durable-queue stand-in. publish() appends; consume() drains a topic."""

    def __init__(self) -> None:
        self._topics: dict[str, list[dict]] = defaultdict(list)

    def publish(self, topic: str, message: dict) -> None:
        self._topics[topic].append(message)

    def consume(self, topic: str) -> Iterable[dict]:
        batch, self._topics[topic] = self._topics[topic], []
        yield from batch


class InMemoryObjectStore(ObjectStorePort):
    """Blob stand-in. Holds bytes by key, all in-process (in the data plane)."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> str:
        self._blobs[key] = data if isinstance(data, bytes) else str(data).encode()
        return key

    def get(self, key: str) -> bytes:
        return self._blobs[key]

    def delete_prefix(self, prefix: str) -> int:
        # #576 review Finding 5: EQUALS prefix (a leaf blob) or starts with prefix + "/" (a
        # numbered family) - never a bare str.startswith, which would let doc_id="policy"
        # match doc_id="policy-2024"'s blob too. See ObjectStorePort.delete_prefix.
        boundary = prefix + "/"
        keys = [k for k in self._blobs if k == prefix or k.startswith(boundary)]
        for k in keys:
            del self._blobs[k]
        return len(keys)


class FilesystemObjectStore(ObjectStorePort):
    """Persistent object store on local disk — for the self-host edition (survives restarts).
    Keys may contain '/' (e.g. 'chunk/tenant/id/0'); they map to nested files under root."""

    def __init__(self, root: str) -> None:
        from pathlib import Path

        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, key: str):
        """Resolve `key` under root and refuse anything that would escape it - TWO
        independent checks, because either one alone has a gap the other closes.

        #576 review round 2, Finding A (CRITICAL): a caller-influenced key (a document
        `external_id`, ultimately - see `is_safe_external_id` for the accept-side guard at
        every ingestion entry point) must never let a `..` segment walk this store OUT of
        its own directory, or even just UP INTO a shared directory it does not own.
        `delete_prefix`'s Finding-7 rewrite traded a performance problem for exactly this:
        the reviewer's exact repro, `delete_prefix("raw/acct:victim/..")`, resolves to
        `<root>/raw` - and an `rmtree` there does not merely lose one document, it destroys
        EVERY account's raw blobs at once, while still technically resolving to a path
        UNDER root. A root-containment check alone would miss that (it never leaves root),
        which is why the first check here is at the PATH-COMPONENT level: no `.` or `..`
        segment is EVER legitimate in a blob key (every segment is server-controlled -
        `kind`, `tenant` - a validated `doc_id`, or a numeric chunk index), so any is
        refused outright before resolution even happens. Backslash is normalized to a
        forward slash first, so a Windows-style traversal string does not slip through a
        POSIX-only split.

        The second check - comparing the RESOLVED candidate against the RESOLVED root via
        `Path.resolve()`, never a string prefix check - catches what the component check
        cannot: an ABSOLUTE key (`self._root / "/etc/passwd"` silently DISCARDS `self._root`
        entirely - that is `pathlib`'s own documented join behaviour, not a traversal
        segment) or a symlink inside the tree that resolves elsewhere.

        A THIRD, narrower check (#576 review round 3, Finding E): an EMPTY key, or any key
        that resolves to the root itself rather than something under it, is refused too.
        `PurePosixPath` silently collapses a lone "." away entirely - it never survives
        into `.parts`, so it cannot be caught by the component check above - and an empty
        string joins onto root as a no-op (`self._root / "" == self._root`). Both are
        invisible to the first two checks (neither leaves root; "." never even reaches the
        component scan), and both mean `delete_prefix` would resolve to the STORE ROOT
        ITSELF - an `rmtree` there is not a bug that loses one document or one account, it
        deletes the entire store for every account at once. Unreachable today (both
        accept-side guards - `is_safe_external_id`, `has_traversal_segment` - already
        refuse an empty id, and no real id is ever a bare "."), but this function's own
        docstring promises "a future method gets both guards by construction": a latent
        whole-store delete is not something to leave for that future caller to discover.

        Every method that turns a key into a filesystem path - `put`, `get`,
        `delete_prefix` - goes through this ONE function, so a future method gets every
        guard by construction rather than by remembering to re-derive them."""
        from pathlib import Path, PurePosixPath

        if not key or not key.strip():
            raise ValueError(f"refused: empty key ({key!r})")

        parts = PurePosixPath(key.replace("\\", "/")).parts
        if any(p in (".", "..") for p in parts):
            raise ValueError(f"refused: key contains a '.' or '..' path segment ({key!r})")

        candidate = (self._root / key).resolve()
        if candidate == self._root:
            raise ValueError(f"refused: key resolves to the object store root itself ({key!r})")
        try:
            candidate.relative_to(self._root)
        except ValueError:
            raise ValueError(
                f"refused: key resolves outside the object store root ({key!r})") from None
        return candidate

    def put(self, key: str, data: bytes) -> str:
        p = self._safe_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else str(data).encode())
        return key

    def get(self, key: str) -> bytes:
        return self._safe_path(key).read_bytes()

    def delete_prefix(self, prefix: str) -> int:
        """Resolve `prefix` to its OWN path under root (via `_safe_path` - Finding A) and
        remove just that - a single file (`raw/{tenant}/{doc_id}`, `segments/{tenant}/
        {doc_id}`: leaf blobs) or a directory (that document's chunk/emb family) and
        everything under it.

        #576 review Finding 7: the first version of this walked the ENTIRE store
        (`self._root.rglob("*")`) on every call, called four times per document from a
        daemon-thread sweep - a deployment with 5000 documents did 20000 full-tree walks.
        Because keys map directly to nested paths (this adapter's whole design, see the
        class docstring), `prefix` names an exact path - no walk of anything OTHER than
        this one document's own (small) subtree is ever needed, and unlike the flat-dict
        InMemoryObjectStore, exact path resolution has no string-boundary ambiguity to
        guard against either (Finding 5 doesn't apply here: `root/raw/t/policy` and
        `root/raw/t/policy-2024` are simply different paths)."""
        target = self._safe_path(prefix)
        if target.is_dir():
            n = sum(1 for p in target.rglob("*") if p.is_file())
            import shutil

            shutil.rmtree(target, ignore_errors=True)
            return n
        if target.is_file():
            target.unlink()
            return 1
        return 0

    def free_bytes(self) -> int:
        """#831: this is the one store whose writes land on the local disk, so it is the
        one store that can answer how close that disk is to full."""
        import shutil

        return shutil.disk_usage(self._root).free


class PlainTextExtractor(ExtractorPort):
    """Decode bytes to text. Real OCR (Azure AI Document Intelligence) lands in Phase 3."""

    def extract(self, data: bytes, mime: str) -> str:
        return data.decode("utf-8", "ignore")


class HashingEmbedding(EmbeddingPort):
    """Deterministic bag-of-words embedding: hashed term frequencies, L2-normalized.
    No ML deps — good enough for lexical retrieval in tests. Real embeddings via
    Azure OpenAI behind the same port (LAW 9)."""

    def __init__(self, dim: int = 128) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in _TOKEN.findall(text.lower()):
                vec[_stable_hash(tok, self.dim)] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


def _chunk_index(chunk_id: str) -> int:
    """Numeric ordering key: `chunk_id` is `{external_id}#{n}` (runner.py) — sort by the
    integer `n`, not lexicographically, so chunk #2 precedes #10 (Fix D)."""
    try:
        return int(chunk_id.rsplit("#", 1)[-1])
    except ValueError:
        return 0


class InMemoryIndex(IndexPort):
    """Hybrid-search stand-in with MANDATORY security trimming (LAW 2).

    The `principals` filter is applied INSIDE search() — a caller cannot retrieve a
    chunk it isn't authorized for, by construction. Embeddings are loaded from the
    object store by reference at upsert (mirrors sending the vector to AI Search)."""

    def __init__(self, store: ObjectStorePort) -> None:
        self._store = store
        self._items: dict[tuple[str, str], tuple[Chunk, list[float]]] = {}
        # #454: ingest moved off the request thread, so a background crawl now upserts and
        # deletes WHILE a user's question iterates this dict — `RuntimeError: dictionary
        # changed size during iteration`, raised precisely when someone asks a question while
        # their library is still indexing, which is the entire point of the feature. Every
        # read below iterates a SNAPSHOT taken under this lock and then scores outside it:
        # scoring is O(chunks x dims) (#533) and must never hold a writer off.
        self._lock = threading.RLock()

    def _snapshot(self) -> list[tuple[tuple[str, str], tuple[Chunk, list[float]]]]:
        with self._lock:
            return list(self._items.items())

    def upsert(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            vec = c.vector(self._store)
            # idempotent by (tenant, chunk_id) — re-indexing replaces, never duplicates (LAW 3)
            with self._lock:
                self._items[(c.tenant_id, c.chunk_id)] = (c, vec)

    def chunk_count_for(self, tenant_id: str, external_id: str) -> int:
        return sum(1 for (c, _) in (v for _k, v in self._snapshot())
                   if c.tenant_id == tenant_id and c.doc_external_id == external_id)

    def search(self, query_embedding: list[float], principals: list[str], top_k: int,
               scope: ReadScope) -> list[dict]:
        pset = set(principals)
        scored: list[tuple[float, Chunk]] = []
        for chunk, vec in (v for _k, v in self._snapshot()):
            if not scope.allows(chunk.tenant_id, chunk.doc_external_id):
                continue  # ADR 0012 + 0019: wrong partition and not shared -> never returned
            if not (set(chunk.allowed_principals) & pset):
                continue  # LAW 2: not authorized -> never returned, full stop
            score = sum(a * b for a, b in zip(query_embedding, vec))
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda sc: -sc[0])
        return [
            {
                "doc_external_id": c.doc_external_id,
                "chunk_id": c.chunk_id,
                "text_ref": c.text_ref,
                "title": c.title,
                "uri": c.uri,
                "locator": c.locator,
                "score": round(score, 4),
                "allowed_principals": c.allowed_principals,
                "owner_oid": c.owner_oid,   # #576 Finding 2: retrieval-based activity touch
            }
            for score, c in scored[:top_k]
        ]

    def lexical_search(self, terms: list, principals: list, top_k: int,
                       scope: ReadScope) -> list[dict]:
        """#498: the KEYWORD candidate source, beside the vector one. Scores every
        authorized chunk by question-term hits - distinct terms matched first, total
        occurrences second - which is exactly where embeddings are blind: a needle of
        raw JSON has no meaning-shape, but "Loo Say Hoo" is an exact token match.

        Same mandatory trims as search(), by construction: wrong tenant never returned
        (ADR 0012), unauthorized never returned (LAW 2). Same hit shape, so the caller
        unions the two pools without translation."""
        wanted = [t for t in (str(t).casefold() for t in terms) if t]
        if not wanted:
            return []
        scored: list[tuple[tuple, Chunk]] = []
        for chunk, _vec in (v for _k, v in self._snapshot()):
            if not scope.allows(chunk.tenant_id, chunk.doc_external_id):
                continue  # ADR 0012 + 0019
            if not (set(chunk.allowed_principals) & set(principals)):
                continue  # LAW 2
            text = self._store.get(chunk.text_ref).decode().casefold()
            hits = {t: text.count(t) for t in wanted if t in text}
            if hits:
                scored.append(((len(hits), sum(hits.values())), chunk))
        scored.sort(key=lambda sc: sc[0], reverse=True)
        return [
            {
                "doc_external_id": c.doc_external_id,
                "chunk_id": c.chunk_id,
                "text_ref": c.text_ref,
                "title": c.title,
                "uri": c.uri,
                "locator": c.locator,
                "score": float(distinct + total / 100.0),
                "allowed_principals": c.allowed_principals,
                "owner_oid": c.owner_oid,   # #576 Finding 2: retrieval-based activity touch
            }
            for (distinct, total), c in scored[:top_k]
        ]

    def distinct_titles(self, tenant_id: str) -> list[str]:
        """#306: distinct doc titles in the index — a CONTENT signal for a document store's routing
        profile (otherwise just the store id), so 'how is epictus' can rank the store holding
        'Epictus Discourses'. Order-stable + deduped. Tenant-scoped (ADR 0012): titles are
        content metadata and must not leak across the partition."""
        seen: set[str] = set()
        out: list[str] = []
        for chunk, _ in (v for _k, v in self._snapshot()):
            if chunk.tenant_id != tenant_id:
                continue
            t = (chunk.title or "").strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def has_authorized(self, principals: list[str], scope: ReadScope) -> bool:
        """#304: does ANY indexed chunk's ACL intersect these principals? An existence check for
        health/exercise — score-free (search() only returns score>0 hits, so a neutral probe finds
        nothing), and LAW-2 exact: the same allowed_principals intersection search() enforces.
        Tenant-scoped like search() (ADR 0012) — existence must not leak across tenants."""
        pset = set(principals)
        return any(scope.allows(chunk.tenant_id, chunk.doc_external_id)
                   and (set(chunk.allowed_principals) & pset)
                   for chunk, _ in (v for _k, v in self._snapshot()))

    def delete(self, tenant_id: str, doc_external_id: str) -> None:
        with self._lock:
            for key in [k for k, (c, _) in self._items.items()
                        if c.doc_external_id == doc_external_id and k[0] == tenant_id]:
                del self._items[key]

    def stats(self, tenant_id: str) -> IndexStats:
        chunks = [c for (t, _cid), (c, _v) in self._snapshot() if t == tenant_id]
        docs = {c.doc_external_id for c in chunks}
        tenant_vecs = [v for (t, _cid), (_c, v) in self._snapshot() if t == tenant_id]
        dim = len(tenant_vecs[0]) if tenant_vecs else 0
        return IndexStats(chunk_count=len(chunks), doc_count=len(docs), embedding_dim=dim)

    def corpus_status(self, scope: ReadScope, principals: list[str]) -> CorpusStatus:
        """#392. Mirrors the pgvector adapter: same partition filter, same ACL predicate
        (set intersection here, && there), so the two backends cannot disagree about
        whether a user has anything to search."""
        allowed = set(principals or [])
        total: set[str] = set()
        mine: set[str] = set()
        for (t, _cid), (c, _v) in self._snapshot():
            if not scope.allows(t, c.doc_external_id):
                continue
            total.add(c.doc_external_id)
            if allowed & set(c.allowed_principals or []):
                mine.add(c.doc_external_id)
        return CorpusStatus(indexed=bool(total), authorized_docs=len(mine))

    def add_doc_principals(self, tenant_id: str, doc_external_id: str,
                           principals: list[str]) -> int:
        """#538/ADR 0017: put extra principals on every chunk of one document.

        The grant operation the sharing design needed and the storage layer never had - the
        upsert path could always REPLACE an acl, but nothing could ADD to one. Returns the
        number of chunks touched so a caller can tell "granted" from "no such document";
        silently succeeding on a document that does not exist is how a share that grants
        nothing gets reported as a share."""
        extra = [p for p in principals if p]
        if not extra:
            return 0
        n = 0
        for (t, _cid), (c, _v) in self._snapshot():
            if t != tenant_id or c.doc_external_id != doc_external_id:
                continue
            merged = list(c.allowed_principals or [])
            merged += [p for p in extra if p not in merged]
            c.allowed_principals = merged
            n += 1
        return n

    def docs_owned_by(self, tenant_id: str, owner_oid: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for (t, _cid), (c, _v) in self._snapshot():
            if t != tenant_id or c.owner_oid != owner_oid:
                continue
            if c.doc_external_id not in seen:
                seen.add(c.doc_external_id)
                out.append(c.doc_external_id)
        return out

    def usage_bytes(self, tenant_id: str, owner_oid: str,
                    exclude_uri: "str | None" = None,
                    exclude_doc_id: "str | None" = None) -> int:
        """#775: how many raw bytes this owner has entrusted to this partition.

        Grouped by DOCUMENT, because `doc_bytes` is denormalized onto every chunk: adding
        the column up directly would multiply each file by its chunk count and bill a
        300-page upload as if it were hundreds of files.

        #844: the exclusions answer "how much will this account hold once the write in
        flight has landed", which is the only number a quota check should compare against.
        A replacement upload REMOVES the version it supersedes, so counting both the old
        and the new bytes refused uploads that in fact fit.
        """
        per_doc: dict[str, int] = {}
        for (t, _cid), (c, _v) in self._snapshot():
            if t != tenant_id or c.owner_oid != owner_oid:
                continue
            if exclude_uri is not None and getattr(c, "uri", None) == exclude_uri:
                continue
            if exclude_doc_id is not None and c.doc_external_id == exclude_doc_id:
                continue
            per_doc[c.doc_external_id] = max(per_doc.get(c.doc_external_id, 0),
                                             int(c.doc_bytes or 0))
        return sum(per_doc.values())

    def list_doc_acls(self, scope: ReadScope) -> list[DocACL]:
        rows: dict[str, DocACL] = {}   # one row per doc (chunks of a doc share acl/title/uri)
        for (t, _cid), (c, _v) in self._snapshot():
            if not scope.allows(t, c.doc_external_id):
                continue
            rows[c.doc_external_id] = DocACL(
                doc_external_id=c.doc_external_id, title=c.title, uri=c.uri,
                allowed_principals=list(c.allowed_principals), owner_oid=c.owner_oid,
            )
        return list(rows.values())

    def list_doc_segments(self, scope: ReadScope, doc_external_id: str, principals: list[str]) -> list[dict]:
        pset = set(principals)
        rows: list[dict] = []
        for (t, _cid), (c, _v) in self._snapshot():
            if c.doc_external_id != doc_external_id or not scope.allows(t, c.doc_external_id):
                continue
            if not (set(c.allowed_principals) & pset):
                continue  # LAW 2: not authorized -> never returned, full stop (mirrors search())
            preview = self._store.get(c.text_ref).decode()[:200]
            rows.append({"chunk_id": c.chunk_id, "locator": c.locator, "preview": preview})
        rows.sort(key=lambda r: _chunk_index(r["chunk_id"]))  # numeric, so #2 precedes #10
        return rows


class InMemoryIdentity(IdentityPort):
    """Maps a user to their transitive group oids (LAW 2). Real impl: Entra via Graph."""

    def __init__(self, user_groups: dict[str, list[str]]) -> None:
        self._user_groups = user_groups
        self._principal_names: dict[str, str] = {}
        self._principal_kinds: dict[str, str] = {}

    def expand_groups(self, user_oid: str) -> list[str]:
        return [user_oid, *self._user_groups.get(user_oid, [])]

    def knows_groups(self, user_oid: str) -> bool:
        """Have we ever RESOLVED this user's memberships? (#266)

        expand_groups() returns [oid] both for "belongs to no groups" and for "never looked",
        so callers cannot tell a resolved-empty from an unresolved user. A lazy resolver needs
        that distinction in both directions: without it, it either never fires (and group ACLs
        stay invisible after a restart) or fires on every single request (a Graph round trip
        per query for anyone genuinely in no groups)."""
        return user_oid in self._user_groups

    def set_user_groups(self, user_oid: str, groups: list[str]) -> None:
        """Register a (real, signed-in) user's transitive Entra group oids at runtime (#171),
        so LAW 2 trims to their actual memberships on the self-host/dev backend."""
        self._user_groups[user_oid] = list(groups)

    def list_principals(self) -> PrincipalDirectory:
        groups = sorted({g for gs in self._user_groups.values() for g in gs})
        return PrincipalDirectory(users=dict(self._user_groups), groups=groups)

    def set_principal_name(self, oid: str, name: str) -> None:
        """Register a human label for an oid (#258). On the self-host/dev backend the
        directory is whatever has signed in or been composed, so names arrive piecemeal
        (a sign-in knows its own name and its groups' names)."""
        self._principal_names[oid] = name

    def set_principal_kind(self, oid: str, kind: str) -> None:
        """Register what KIND of directory object an oid is (#881): user, group, or
        directoryRole. Arrives on the same Graph lookup as the name (fetch_principal_facts)
        and is stored beside it rather than derived, because nothing about a GUID says which
        it is - which is how a role ended up offered in the ACL picker as a "group"."""
        self._principal_kinds[oid] = kind

    def list_directory(self) -> list[DirectoryPrincipal]:
        """Only principals we can actually NAME are pickable — see IdentityPort.list_directory.
        Unnamed oids stay absent rather than appearing as bare GUIDs the operator can't verify."""
        out: list[DirectoryPrincipal] = []
        for g in sorted({g for gs in self._user_groups.values() for g in gs}):
            if self._principal_names.get(g):
                # #881: "group" is the FALLBACK, not the answer. Before this, every non-user
                # principal was flatly declared a group, so #872's directory roles arrived in
                # the picker as "Global Administrator (group)" - a label that misdescribes who
                # a store is being shared with, at the moment the operator decides to share it.
                # Anything whose kind was never learned (composed rigs, dev seeds) keeps the
                # old label rather than becoming "unknown".
                out.append(DirectoryPrincipal(oid=g, name=self._principal_names[g],
                                              kind=self._principal_kinds.get(g, "group")))
        for u in sorted(self._user_groups):
            if self._principal_names.get(u):
                out.append(DirectoryPrincipal(oid=u, name=self._principal_names[u], kind="user"))
        return out


class ExtractiveLlm(LlmPort):
    """Deterministic 'answer' from the supplied (already permission-trimmed) context.
    No external call. Real impl: Azure OpenAI behind the same port (LAW 9). It only
    ever sees post-trim text (LAW 2); citations are assembled by the QueryService."""

    #: Context entries headed by one of these are DIRECTIVES to the model, not evidence
    #: (#206 [coverage], #227 [query], #449 [style]). Kept in sync with
    #: synthesizer._INSTRUCTION_MARKERS by selftest_570; not imported, because an adapter
    #: importing the router would invert the dependency this package exists to keep one-way.
    _DIRECTIVE_HEADS = ("[coverage]", "[query]", "[style]")

    def answer(self, question: str, context_chunks: list[str]) -> dict:
        # #570: a real model OBEYS a directive chunk; this one quotes whatever it is given, so
        # the whole "[style] Answer in plain prose ..." paragraph was rendering to the user as
        # the answer. Invisible on Anthropic/Groq, total on the no-key path - which is the
        # self-host edition and every demo rig without credentials.
        #
        # Matched on the LABEL only. Evidence is routinely bracketed too ("[hr-wiki · hr] ..."),
        # and dropping those would empty the answer instead of cleaning it: the opposite
        # failure, and the worse one.
        context_chunks = [c for c in context_chunks
                          if not c.lstrip().lower().startswith(self._DIRECTIVE_HEADS)]
        if not context_chunks:
            return {"answer": NO_EVIDENCE_ANSWER, "citations": []}
        joined = " ".join(" ".join(c.split()) for c in context_chunks)
        snippet = joined[:400] + ("…" if len(joined) > 400 else "")
        answer = f"Based on {len(context_chunks)} retrieved source(s): {snippet}"
        return {"answer": answer, "citations": []}

    def condense_question(self, question: str, history: list[dict]) -> str:
        # Deterministic dev/test rewrite: fold the most recent question in front of the
        # follow-up so retrieval embeds the prior topic too. Real models do this properly.
        if not history:
            return question
        prev = history[-1].get("question", "").strip()
        return f"{prev} {question}" if prev else question


class EnvSecrets(SecretsPort):
    """Secrets from environment for local dev. Real impl: Azure Key Vault."""

    def get_secret(self, name: str) -> str:
        return os.environ.get(name, "")

    def put_secret(self, name: str, value: str) -> None:
        raise NotImplementedError(
            "EnvSecrets is operator configuration and is read-only: it reads the server's "
            "own environment, so a self-serve write has nowhere to go. Configure "
            "DBSEARCH_SECRET_KEY and use EncryptedFileSecrets for user-supplied credentials.")

    def delete_secret(self, name: str) -> None:
        raise NotImplementedError(
            "EnvSecrets is operator configuration and is read-only; unset the environment "
            "variable on the server instead.")

    def describe_secret(self, name: str) -> "dict | None":
        raw = os.environ.get(name, "")
        if not raw:
            return None
        return {"exists": True, "hint": raw[-4:] if len(raw) > 4 else ""}


from dbsearch.adapters.local.secrets import EncryptedFileSecrets  # noqa: E402

__all__ = [
    "InMemoryQueue",
    "InMemoryObjectStore",
    "FilesystemObjectStore",
    "PlainTextExtractor",
    "LocalRichExtractor",
    "UnsupportedMedia",
    "ParseProducedNoText",
    "HashingEmbedding",
    "InMemoryIndex",
    "InMemoryIdentity",
    "ExtractiveLlm",
    "EnvSecrets",
    "EncryptedFileSecrets",
    "Principal",
]

from dbsearch.adapters.local.rich_extractor import (  # noqa: E402
    LocalRichExtractor, UnsupportedMedia, ParseProducedNoText,
)
