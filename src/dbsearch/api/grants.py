"""Sharing a document with a named person (ADR 0017, #538).

A grant mints a `grant:<id>` principal, puts it on the document's ACL once, and hands it to
the grantee's principal expansion for as long as the grant is live. Everything useful falls
out of that one shape:

  revoke   - drop the record; the principal stops being expanded and the read stops with it.
             The document's ACL is never rewritten, so there is no window in which a revoked
             reader still reads, and a dangling `grant:<id>` matches nobody forever.
  expiry   - evaluated during expansion, on every request. No sweeper, so no "the sweeper did
             not run and an expired grant still works" failure mode to reason about.
  LAW 2    - the principal-overlap test stays the ONE enforcement point. Sharing adds no
             second authorization path that could disagree with the first.

#575 closed the "not in this slice" durability gap ADR 0017 s4 named: `GrantRegistry` now
takes an optional write-through store (`dbsearch.server.grant_store`). The in-memory dict
below stays the ONLY thing per-request expansion reads - durability adds a store on the
write path and a one-time hydration at construction, never a read-time round trip, so
expiry evaluation and the LAW 2 single-enforcement-point property are unchanged by whether
a store is attached.

Revocation is NOT simply "unchanged", and an earlier version of this docstring wrongly
claimed it was (#575 review, Finding 3). With no store, revocation is exactly as
instantaneous and unconditional as it always was. With a store attached, revoke is
FAIL-CLOSED on that store: the delete is attempted there first, and memory is only cleared
once it succeeds. A store outage therefore makes revoke unavailable (it raises, and the
grant stays live in this process) rather than let a "revoked" grant quietly survive in
Postgres and get handed back out by the next restart's hydration - see `revoke`'s own
docstring for why that direction of failure was the one bug this design cannot tolerate.
`create` keeps the opposite, best-effort stance on purpose: losing durability on a create
is safe to shrug off, losing durability on a revoke is not.
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

PRINCIPAL_PREFIX = "grant:"

LINK_PRINCIPAL_PREFIX = "link:"
# #605 / ADR 0021: the prefix of a SYNTHETIC principal - `link:<share_id>`, the sentinel a link
# share grants its documents to, standing in for a grantee who signs in to nothing.
#
# DEFINED HERE, in the ACL layer, and re-exported by server/conversation_shares.py as
# `LINK_GRANTEE_PREFIX` rather than spelled out twice. One definition, and this is the layer
# that has to RECOGNISE the shape (see `GrantAwareIdentity.direct_principals`), while the share
# registry is only the layer that MINTS it - so the dependency runs server -> api, the direction
# every other module here already runs. A drifted copy would mean either a principal that
# matches nothing (a link that silently opens no documents) or, worse, a real oid treated as a
# synthetic one.


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Grant:
    grant_id: str
    doc_external_id: str
    tenant_id: str
    grantee_oid: str
    granted_by: str
    expires_at: "datetime | None"
    created_at: datetime = field(default_factory=_now)
    conv_id: "str | None" = None
    # #600: set = this grant exists BECAUSE one conversation was shared, so it is only an
    # authorization inside THAT conversation.
    #
    # #601 (ADR 0020) CORRECTED WHERE THAT IS ENFORCED, and the correction is the whole
    # point of the field now. This used to say the scoping lived in the doorway
    # (`_request_scope`, server/app.py) and that the ACL principal was "deliberately
    # conversation-blind", on the reasoning that one place should scope and one place
    # should authorize. That reasoning was wrong in a way that made the scoping vanish
    # entirely: the doorway is partition ROUTING, and `ReadScope.allows` returns True on
    # its partition-equality arm before it ever looks at the doorway. So in the ordinary
    # single-organization deployment - where grantor and grantee are in the SAME partition,
    # which is every self-host box and every single-org Entra tenant - the doorway was
    # never consulted and NOTHING scoped the grant. Reproduced end to end: a share of one
    # conversation handed the grantee /search, the "Your data" listing, the segment
    # previews, the original file bytes and /chat in an unrelated thread.
    #
    # So the rule now lives on the ACL side, where authorization is: `live_principals_for`
    # expands a conv-scoped grant's principal ONLY when its conversation is the active one,
    # and its default (no conversation active) drops it. That default is what makes every
    # read path that has no conversation concept safe by construction rather than by
    # remembering to ask. The doorway filter stays as the cross-partition backstop.

    @property
    def principal(self) -> str:
        return PRINCIPAL_PREFIX + self.grant_id

    def is_live(self, at: "datetime | None" = None) -> bool:
        return self.expires_at is None or (at or _now()) < self.expires_at

    def to_dict(self) -> dict:
        """LAW 1: who and when, never document content."""
        return {"grant_id": self.grant_id, "doc_external_id": self.doc_external_id,
                "grantee_oid": self.grantee_oid, "granted_by": self.granted_by,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "created_at": self.created_at.isoformat(),
                "conv_id": self.conv_id,
                "live": self.is_live()}


class GrantRegistry:
    def __init__(self, store=None) -> None:
        """`store` is optional (#575): unset, behaviour is exactly the ADR 0017 s4 in-memory
        registry - every existing caller (including `tests/selftest_538_document_grants.py`,
        which constructs `GrantRegistry()` with nothing) keeps working untouched. When set,
        hydration is best-effort: an unreachable store logs and starts empty rather than
        failing boot - the same stance `TokenVault` takes on its refresh-token store, because
        a store outage must cost durability, never the ability to serve a request."""
        self._by_id: dict[str, Grant] = {}
        self._lock = threading.Lock()
        self._store = store
        if store is not None:
            try:
                self._by_id = {g.grant_id: g for g in store.load_all()}
            except Exception:
                logging.getLogger("dbsearch").error(
                    "grant store unreadable - starting with no grants loaded")

    def create(self, doc_external_id: str, tenant_id: str, grantee_oid: str,
               granted_by: str, expires_in_days: "int | None" = None,
               conv_id: "str | None" = None) -> Grant:
        if not grantee_oid or not grantee_oid.strip():
            raise ValueError("a grant needs somebody to grant to")
        if grantee_oid == granted_by:
            raise ValueError("you already have access to this document")
        # #600 review Finding C: an empty or whitespace-only conv_id is not a real
        # conversation id - a route that forwards a blank body field (e.g. conv_id="")
        # unintentionally must not mint a grant that LOOKS conv-scoped but can never match
        # any real `active_conv_id` a request could carry (ChatRequest.conv_id is required
        # and _request_scope would have to see the exact same "" to open it), which would
        # be a share that silently never opens rather than an honest document share.
        conv_id = conv_id.strip() if conv_id and conv_id.strip() else None
        expires_at = (_now() + timedelta(days=expires_in_days)) if expires_in_days else None
        g = Grant(grant_id=uuid.uuid4().hex, doc_external_id=doc_external_id,
                  tenant_id=tenant_id, grantee_oid=grantee_oid, granted_by=granted_by,
                  expires_at=expires_at, conv_id=conv_id)
        with self._lock:
            self._by_id[g.grant_id] = g
        # Write-through, outside the lock (I/O should never hold up another thread's
        # expansion), and best-effort: a grant that outlives the process is the goal, but a
        # down store must not turn a working share into a 500 - same stance as TokenVault.
        # A grant born while the store is unreachable simply does not survive a restart; the
        # share itself works right now, which is what the caller asked for.
        if self._store is not None:
            try:
                self._store.save(g)
            except Exception:
                logging.getLogger("dbsearch").error(
                    "grant store unreachable - grant %s created in-process only", g.grant_id)
        return g

    def discard_unconfirmed(self, grant_id: str) -> None:
        """Compensates for a grant `create` made whose share never actually took effect -
        the ONLY caller today is `grant_document` (server/app.py), which creates the grant
        record and then adds its principal to the target document's ACL; if that ACL write
        touches zero chunks (document deleted between the ACL check and the write - a TOCTOU
        window, not the common case) the share never happened and the grant record must not
        outlive that request.

        #575 review, Finding A: this is deliberately NOT `revoke`. `revoke` reverses a share
        that DID take effect, so it fails closed on the store - a live authorization decision
        must never be silently un-done. A grant that never made it onto any document's ACL is
        not a live authorization decision at all; `grant:<id>` cannot match anything it was
        never added to (ADR 0017 s1), so there is nothing here for a store outage to put at
        risk. What IS at risk if this fails closed like `revoke` is the caller: the rollback
        runs from inside an already-failed request (the real error is "no such document"),
        and a `GrantStoreUnavailable` raised from a compensating cleanup would mask that
        honest 404 behind a 500 - and leave the dead grant sitting in `_by_id` for the rest
        of the process's life besides, since a masked rollback never runs the memory pop it
        was supposed to guarantee. So the in-process removal here is unconditional, and the
        store delete is best-effort (logged, never raised) - the same stance `create` takes,
        because this is closer to "undo a create" than "perform a revoke"."""
        with self._lock:
            self._by_id.pop(grant_id, None)
        if self._store is not None:
            try:
                self._store.delete(grant_id)
            except Exception:
                logging.getLogger("dbsearch").error(
                    "grant store unreachable - unconfirmed grant %s discarded in-process only",
                    grant_id)

    def revoke(self, grant_id: str, requester_oid: str) -> Grant:
        """Only the person who made the grant may take it back.

        This is the gap the #538 scoping called out in the api-key design: a token bound to a
        share principal left the SHARER failing every ownership check, so they could not
        revoke their own share. `granted_by` is kept distinct from `grantee_oid` precisely so
        that cannot happen here.

        #575 review, Finding 1 (CRITICAL, fixed here): this used to delete from memory FIRST
        and treat the store delete as best-effort, on the same "a down store must never turn
        a working action into a 500" reasoning `create` uses. That reasoning is right for
        `create` and WRONG for `revoke`, because the two fail in opposite directions:

          - `create`'s worst case on a store outage is a grant that works right now but does
            not survive a restart. Losing durability going FORWARD is an acceptable trade.
          - `revoke`'s worst case, under the old code, was a grant that LOOKS revoked (the
            API returns success, memory is clear) but whose row is still sitting in Postgres.
            The next restart's `load_all()` hydration would resurrect it - not for one
            window, but indefinitely, until someone happened to revoke the very same grant a
            second time. A resurrected TokenVault refresh token only re-establishes a user's
            OWN delegated credential, still filtered by a live LAW 2 group check on every
            request; a resurrected grant principal IS the authorization decision - `grant:
            <id>` exists specifically to hand someone access their real identity does not
            carry. Silently restoring a deliberately-cut-off reader's access is exactly what
            "never return a result a user isn't authorized to see" (CLAUDE.md) rules out.

        So the store delete now runs FIRST, and memory is only cleared once it succeeds (or
        there is no store to fail). On a store failure this method raises - deliberately
        unguarded, no try/except here - and the grant stays live in THIS process too, so
        behaviour is honest and consistent rather than silently wrong. The cost is that
        revoke becomes unavailable during a store outage instead of silently unsafe, which is
        the correct trade for an authorization operation."""
        with self._lock:
            g = self._by_id.get(grant_id)
            if g is None or g.granted_by != requester_oid:
                # One message for "no such grant" and "not yours": a 404 that distinguishes
                # them is an oracle for grants belonging to other people.
                raise KeyError(grant_id)
        # Ownership already verified above. The store call runs outside the lock (I/O should
        # not block another thread's expansion) but BEFORE any memory mutation, and it is
        # left to raise on failure - see the docstring above for why that is now the point.
        if self._store is not None:
            self._store.delete(grant_id)
        with self._lock:
            self._by_id.pop(grant_id, None)
        return g

    def drop_for_documents(self, doc_ids: "set[str]") -> int:
        """Task 6's retention sweep: when a document is deleted, its grants should not
        outlive it. Removes every grant (memory + store) whose `doc_external_id` is in
        `doc_ids`, returns the count.

        #575 review, Finding 2: this deliberately does NOT copy `revoke`'s fail-closed
        gating (store delete first, memory only cleared on success), even though it applies
        the same PER-ITEM ordering (store attempted before memory is touched) and the same
        "one failure must not abort the batch" behaviour `revoke` also needs. The two
        operations carry different risk if their store call fails - but NOT because a
        deleted document can never come back (#575 review, Finding C corrects an earlier
        version of this comment that argued exactly that; it does not hold: upload external
        ids are content-addressed, `upload-<slug>-<sha256 prefix>` - re-uploading the same
        file after deletion reuses the SAME doc_external_id, and a store row this loop failed
        to purge would still be sitting there for a later `GrantRegistry` to rehydrate).

        The real reason a leftover row here is safe is where the grant principal lives, not
        whether the document comes back. `grant:<id>` is written onto a document's
        `allowed_principals` exactly ONCE, at share time (`grant_document`, server/app.py).
        Re-ingesting a document - the ONLY way its doc_external_id becomes readable again -
        REPLACES that chunk's ACL with the uploader's own list wholesale
        (`pgvector.py`'s `upsert`: `allowed_principals=EXCLUDED.allowed_principals`; the
        local adapter's `upsert` replaces the same tuple outright), it does not merge into
        the old one. So even a rehydrated, still-"live" `grant:<id>` principal is on no
        document's ACL any more and cannot match anything (ADR 0017 s1) - the orphan is
        inert, not because the document stays gone, but because nothing puts that principal
        back once it has been forgotten here.

          - `revoke` cuts off access to a document whose ACL the grant principal is CURRENTLY
            on. A grant row that survives in the store because the delete failed is a live
            authorization decision waiting to be resurrected by the next restart - which is
            why `revoke` raises and leaves the grant expandable rather than pretend it
            succeeded.
          - This sweep runs over documents that are themselves being deleted, and the grant
            principals it is dropping are, by the paragraph above, never re-added to any ACL
            by a later re-ingest. A failed purge here leaves an inert row, not a live one.

        Given that, and because a sweep is unattended (nobody is watching a single failed
        item to retry it by hand), memory is cleared for every matching grant unconditionally
        - a stray store row left behind by a failed delete is cleaned up on the store's own
        terms (or the next sweep pass) without leaving a phantom, still-expandable grant in
        THIS process. One item's store failure is logged and skipped; the loop always
        finishes and every match is removed from memory, which is what the count returned
        here reflects.

        Residual cosmetic problem, left unfixed on purpose (do not build a fix for this - the
        reviewer asked only that it be recorded): if the store purge fails and the document
        is later re-ingested, `GET /documents/{id}/grants` (`list_document_grants`,
        server/app.py) would still list that grant as if the document were shared with its
        grantee, from whatever memory a subsequent hydration builds - even though the
        grantee's expansion no longer matches the document's (replaced) ACL and cannot read
        it. It reads wrong; it does not grant anything."""
        with self._lock:
            dead = [gid for gid, g in self._by_id.items() if g.doc_external_id in doc_ids]
        if self._store is not None:
            for gid in dead:
                try:
                    self._store.delete(gid)
                except Exception:
                    logging.getLogger("dbsearch").error(
                        "grant store unreachable - grant %s dropped in-process only", gid)
        with self._lock:
            for gid in dead:
                self._by_id.pop(gid, None)
        return len(dead)

    def drop_for_conversation(self, conv_id: str, grantee_oid: str,
                              granted_by: "str | None" = None) -> int:
        """#600: end one conversation share for one person - "she can no longer keep
        asking about that thread" - without touching any other grant naming her (a #582
        document share, or a different conversation share).

        This is `revoke`'s camp, not `drop_for_documents`'s, and deliberately so. A
        conv-scoped grant is a LIVE authorization decision exactly like a document grant is
        - it is the reason `_request_scope` (server/app.py) is willing to hand this person a
        doorway pair at all while that conversation is active - so undoing it carries the
        same risk `revoke`'s docstring spends most of its length on: a store delete that
        fails and is swallowed leaves the row sitting in Postgres, and the next restart's
        `load_all()` hydration resurrects it as a live, expandable grant - not for one
        window, but indefinitely, exactly the "silently restore a deliberately-cut-off
        reader's access" failure CLAUDE.md rules out.

        `drop_for_documents` gets to be best-effort because its rows are provably inert even
        if a purge fails: the document is being deleted too, and nothing re-adds a dropped
        grant's principal to any ACL afterward (see that method's docstring). Nothing
        equivalent holds here - the document a conv-scoped grant's principal sits on is NOT
        being deleted, so a row this loop failed to purge is not an orphan, it is a reader
        who still has the key. So the store delete runs per item, FIRST and unguarded, and
        memory is only cleared once it succeeds - the same ordering and the same reason
        `revoke` uses, and if the store is down this raises rather than pretend the
        conversation was un-shared.

        #600 review Finding D (sharpened by Finding G): unlike `revoke`, this does NOT check
        who is asking - there is no `requester_oid` parameter and no `granted_by` comparison.
        That is deliberate, not an oversight, but it is a REQUIREMENT ON THE CALLER, not a
        fact this codebase can yet claim: `revoke` is reached from a route where the caller
        names a `grant_id` they might not own, so ownership has to be checked HERE or
        nowhere. This method is meant to be reached only from a route that has ALREADY
        established the caller may act on `conv_id` - via whatever access check the
        conversation store ends up using - before calling this. No such route exists yet
        (#600 lands the mechanism only; wiring a route to `drop_for_conversation` is later
        work), so nothing today actually enforces that precondition. Whoever wires the first
        caller MUST verify the requester is allowed to end sharing for `conv_id` before
        calling this, exactly as `drop_for_documents`' caller (the retention sweep) is
        trusted to only ever pass `doc_ids` it already decided to delete - this method trusts
        `conv_id` and `grantee_oid` the same way, and that trust has no route backing it yet.

        #601 IMPORTANT-B: `granted_by` narrows the drop to ONE sharer's grants, and every
        caller that is acting on behalf of a particular sharer must pass it. `conv_id` is
        CLIENT-CHOSEN, so two people can have threads under the same id - and a caller who
        has proved she owns her own thread under that id has proved nothing about anybody
        else's grants under it. Without this, sharing a thread whose id collided with one
        somebody else had already shared destroyed that other person's grants while their
        share row stayed live, so their recipient kept a listed share backed by nothing and
        the sharer was never told.

        PASS IT. Both production callers do - `share_conversation` passes the caller,
        `revoke_conversation_share` passes the resolved share's `grantor_oid` - and no
        production caller leaves it None. Left None the drop covers EVERY sharer's grants for
        that (conversation, person), which is the defect this parameter exists to prevent, not
        a mode anything wants; the default survives only so registry-level tests can build the
        unscoped shape deliberately. An earlier version of this paragraph said the revoke
        route wanted the unscoped drop because it had already resolved a share row it owns.
        That was wrong - owning a share row proves who may END it, never whose grants sit
        under a client-chosen conv_id - and it stayed wrong here for a round after the share
        route was fixed, which is exactly long enough for the revoke route to be reproduced
        destroying another sharer's grants."""
        with self._lock:
            dead = [gid for gid, g in self._by_id.items()
                    if g.conv_id == conv_id and g.grantee_oid == grantee_oid
                    and (granted_by is None or g.granted_by == granted_by)]
        for gid in dead:               # store FIRST, unguarded - revoke's ordering, revoke's reason
            if self._store is not None:
                self._store.delete(gid)
            with self._lock:
                self._by_id.pop(gid, None)
        return len(dead)

    def live_principals_for(self, oid: str,
                            active_conv_id: "str | None" = None) -> list[str]:
        """The grant principals this person currently holds IN THIS CONTEXT. Expiry is
        applied HERE, per request, which is why no sweeper exists.

        #601 / ADR 0020: THIS is where a conversation share is scoped, and the default is
        FAIL-CLOSED. A grant with no `conv_id` is an ordinary #582 document share and
        always expands. A grant WITH a `conv_id` exists only because one conversation was
        shared, so it expands only while that conversation is the active one - and
        `active_conv_id=None`, which is what every read path with no conversation concept
        passes by simply not passing anything, drops it.

        The default is the safety property, not a convenience. This used to be
        conversation-blind, with the scoping done in `_request_scope`'s doorway instead;
        that placed the rule in partition ROUTING, which `ReadScope.allows` short-circuits
        whenever the two parties share a partition - so on any single-organization
        deployment the rule was not applied at all. Putting it here means a route that
        forgets to say which conversation is active gets LESS access rather than all of it,
        and every existing caller of this method became correct by not changing."""
        if not oid:
            return []
        at = _now()
        with self._lock:
            return sorted(g.principal for g in self._by_id.values()
                          if g.grantee_oid == oid and g.is_live(at)
                          and (g.conv_id is None or g.conv_id == active_conv_id))

    def live_grants_for(self, oid: str) -> list[Grant]:
        """The live grant RECORDS this person holds - `live_principals_for`'s sibling.

        #582 / ADR 0019 D3: the doorway needs each grant's PARTITION and DOCUMENT, where
        the ACL side needs only its principal. Same records, same lock, and deliberately
        the same per-request expiry evaluation: if these two views could disagree, a caller
        would end up holding a grant principal with no doorway (looks nowhere, reads
        nothing) or a doorway with no principal (looks, and the ACL refuses) - both silent
        failures of exactly the kind this card exists to remove."""
        if not oid:
            return []
        at = _now()
        with self._lock:
            return [g for g in self._by_id.values()
                    if g.grantee_oid == oid and g.is_live(at)]

    def list_for_document(self, doc_external_id: str) -> list[Grant]:
        with self._lock:
            return sorted((g for g in self._by_id.values()
                           if g.doc_external_id == doc_external_id),
                          key=lambda g: g.created_at)

    def principals_for_document(self, doc_external_id: str) -> list[str]:
        return [g.principal for g in self.list_for_document(doc_external_id)]


class GrantAwareIdentity:
    """Wraps any IdentityPort so grants apply without either adapter knowing sharing exists.

    `direct_principals` is the un-granted set, and it is what the "may you share this?" check
    uses - so a share can never be re-shared (ADR 0017 s2).
    """

    def __init__(self, inner, registry: GrantRegistry) -> None:
        self._inner = inner
        self._grants = registry

    def __getattr__(self, name):          # set_user_groups, list_directory, knows_groups, ...
        return getattr(self._inner, name)

    def direct_principals(self, user_oid: str) -> list[str]:
        """The un-granted principal set - what "may you share this?" tests against, so a share
        can never be re-shared (ADR 0017 s2).

        A SYNTHETIC LINK PRINCIPAL NEVER REACHES THE DIRECTORY ADAPTER (#605 review, Finding 3).
        `link:<share_id>` names no person: there is no Entra user, no account row and nothing
        for a directory to resolve. Asking the adapter anyway is not merely wasteful, it is a
        live outage on the flagship deployment - `EntraIdentity.expand_groups` POSTs
        `/users/link:<share_id>/getMemberGroups` to Microsoft Graph and calls
        `raise_for_status()`, so on `SELFHOST_BACKEND=azure` EVERY visitor question would 500
        and the whole feature would be dead on arrival there. It fails closed, so it was never
        a disclosure; it was simply broken, and invisible to a memory-backed test rig whose
        `expand_groups` answers `[oid]` for any string.

        Short-circuited HERE rather than in the link routes, because this is the one seam every
        expansion passes through - `expand_groups`, `expand_groups_scoped` and therefore
        retrieval, the corpus denominator and the document listing all reach the adapter through
        this method, and a guard placed in the caller would have to be repeated in each of them
        and would eventually not be.

        The short-circuit can only ever NARROW. It returns the oid alone - exactly what a
        directory would return for somebody in no groups - so if a real oid ever began with
        `link:` (no id shape in this product does: Entra oids are GUIDs, local accounts are
        `acct_<hex>`) that person would lose their group memberships, never gain one. The grant
        principals that actually authorize a visitor are added by the callers below and are
        untouched by this."""
        if user_oid.startswith(LINK_PRINCIPAL_PREFIX):
            return [user_oid]
        return list(self._inner.expand_groups(user_oid))

    def expand_groups(self, user_oid: str) -> list[str]:
        """The caller's principals for a read with NO conversation active.

        #601 / ADR 0020: conv-scoped grant principals are NOT in here, because
        `live_principals_for`'s default drops them. That is what makes /search, the "Your
        data" listing, segment previews, downloads, suggestions, the router rail and the
        admin surfaces safe WITHOUT a single one of them being changed - a conversation
        share cannot reach a surface that has no conversation to name."""
        return self.direct_principals(user_oid) + self._grants.live_principals_for(user_oid)

    def expand_groups_scoped(self, user_oid: str,
                             active_conv_id: "str | None" = None) -> list[str]:
        """`expand_groups` for a read that HAS a conversation - the conversational rail
        (`QueryService.retrieve` and the corpus denominator that must agree with it).

        A separate method rather than an optional keyword on `expand_groups`, deliberately:
        `expand_groups` is the `IdentityPort` contract that every adapter implements, and
        widening it would make every adapter carry a parameter only this wrapper can honour.
        Callers reach this through `dbsearch.ports.base.expand_principals`, which falls back
        to plain `expand_groups` for an identity that has never heard of conversations - so
        the fallback direction is always the narrower one."""
        return (self.direct_principals(user_oid)
                + self._grants.live_principals_for(user_oid, active_conv_id))
